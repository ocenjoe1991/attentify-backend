from fastapi import APIRouter, Request, HTTPException, Response, Depends, Body
from fastapi.responses import RedirectResponse
from typing import List, Optional, Dict, Any
import httpx
from urllib.parse import urlencode
from datetime import datetime, timedelta
from app.core.security import get_current_user
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.cloud import pubsub_v1
from app.core.config import settings
import asyncio
import json
import base64
import urllib.parse
from app.db.mongodb import get_database
from app.services.gmail_service import get_gmail_service
from google.oauth2 import service_account
from email.utils import parsedate_to_datetime
from app.models.gmail import (
    GmailAccountCreate,
    GmailAccountUpdate,
    GmailAccountInDB
)

from app.models.message import Message, ChatEntry 
from app.utils.logger import logger
from app.main import sio
import re

router = APIRouter()

def gmail_account_helper(account: dict) -> dict:
    return {
        "id": str(account["_id"]),
        "user_id": str(account["user_id"]),
        "email": account["email"],
        "access_token": account["access_token"],
        "refresh_token": account["refresh_token"],
        "token_type": account.get("token_type", "Bearer"),
        "expires_at": account["expires_at"],
        "client_id": account["client_id"],
        "client_secret": account["client_secret"],
        "status": account.get("status", "connected"),
        "scope": account.get("scope"),
        "token_issued_at": account.get("token_issued_at"),
        "is_primary": account.get("is_primary", False),
        "provider": account.get("provider", "google"),
        "history_id":  account.get("history_id", ""),
        "store": account.get("store", "")
    }

@router.post("/", response_model=GmailAccountInDB)
async def create_gmail_account(account: GmailAccountCreate, request: Request):
    db = request.app.state.db
    existing = await db.gmail_accounts.find_one({"email": account.email})
    if existing:
        raise HTTPException(status_code=400, detail="Gmail account already registered")

    # Ensure user_id is a valid ObjectId
    try:
        account_dict = account.dict()
        account_dict["user_id"] = ObjectId(account.user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await db.gmail_accounts.insert_one(account_dict)
    account_dict["id"] = str(result.inserted_id)
    return gmail_account_helper(account_dict)

@router.get("/company_accounts/{company_id}")
async def list_gmail_accounts(
    company_id: str, 
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")
    
    # ✅ check membership
    membership = await db["memberships"].find_one(
        {"user_id": current_user["_id"], "company_id": ObjectId(company_id)}
    )

    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")
    
    role = membership.get("role")

    if role == "company_owner":
        accounts_cursor = db.gmail_accounts.find({"company_id": ObjectId(company_id)})
    elif role == "store_owner":
        accounts_cursor = db.gmail_accounts.find({"user_id": current_user["_id"]})
    else:  # agent and fallback
        accounts_cursor = db.gmail_accounts.find({"company_id": ObjectId(company_id)})
    
    accounts = []
    async for account in accounts_cursor:
        owner = await db.users.find_one({"_id": account["user_id"]})
        if not owner:
            continue
        account_data = gmail_account_helper(account)
        account_data["owner_email"] = owner.get("email", "unknown")
        account_data["owner_name"] = f"{owner.get('first_name', 'unknown')} {owner.get('last_name', 'unknown')}"
        if account.get("store_id"):
            store = await db.shopify_cred.find_one({"_id": account["store_id"]})
            if store:
                print(store)
                account_data["store"] = {
                    "id": str(store["_id"]),
                    "shop": store.get("shop", "")
                }
        accounts.append(account_data)

    # ✅ fetch stores properly
    stores_cursor = db.shopify_cred.find({"user_id": current_user["_id"]})
    stores = []

    async for store in stores_cursor:
        stores.append({
            "id": str(store["_id"]),
            "shop": store.get("shop", "")
        })

    return {
        "accounts": accounts,
        "stores": stores
    }

@router.get("/{account_id}", response_model=GmailAccountInDB)
async def get_gmail_account(account_id: str, request: Request):
    db = request.app.state.db
    account = await db.gmail_accounts.find_one({"_id": ObjectId(account_id)})
    if not account:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    return gmail_account_helper(account)

@router.put("/{account_id}", response_model=GmailAccountInDB)
async def update_gmail_account(account_id: str, update: GmailAccountUpdate, request: Request):
    db = request.app.state.db
    update_data = {k: v for k, v in update.dict().items() if v is not None}

    if "user_id" in update_data:
        try:
            update_data["user_id"] = ObjectId(update_data["user_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user_id format")

    if not update_data:
        raise HTTPException(status_code=400, detail="No data provided for update")

    result = await db.gmail_accounts.update_one({"_id": ObjectId(account_id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    account = await db.gmail_accounts.find_one({"_id": ObjectId(account_id)})
    return gmail_account_helper(account)

@router.put("/{id}/store")
async def update_gmail_account(
    id: str,
    body: dict = Body(...), 
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    field = body.get("field")
    value = body.get("value")

    if not field:
        raise HTTPException(status_code=400, detail="Field is required")

    if field == "_id":
        raise HTTPException(status_code=400, detail="Cannot update _id field")
    
    update_query = {}

    if field == "store_id":
        if value == "":  # Remove store_id if empty string
            update_query = {"$unset": {field: ""}}
        else:
            try:
                value = ObjectId(value)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid store_id format")
            update_query = {"$set": {field: value}}
    else:
        update_query = {"$set": {field: value}}

    result = await db["gmail_accounts"].update_one(
        {"_id": ObjectId(id)},
        update_query
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    return {"message": f"{field} updated"}


@router.delete("/{account_id}", status_code=204)
async def delete_gmail_account(account_id: str, request: Request):
    db = request.app.state.db
    account = await db.gmail_accounts.find_one({"_id": ObjectId(account_id)})
    if not account:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    # Step 1: Stop Gmail Watch for this user
    try:
        creds = Credentials(
            token=account['access_token'],
            refresh_token=account.get('refresh_token'),
            token_uri=account.get('token_uri', 'https://oauth2.googleapis.com/token'),
            client_id=account['client_id'],
            client_secret=account['client_secret'],
            scopes=account.get('scopes', ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/gmail.readonly']),
        )
        service = build('gmail', 'v1', credentials=creds)
        service.users().stop(userId="me").execute()
    except Exception as e:
        # Don't block delete if Gmail stop fails
        print(f"Failed to stop watch for {account['email']}: {e}")

    # Step 2: Delete from DB
    result = await db.gmail_accounts.delete_one({"_id": ObjectId(account_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    return None

# Environment variables for Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email"

#/api/v1/gmail/oauth/login
@router.get("/oauth/login")
async def google_oauth_login(user_id: str, company_id: str):
    """
    Starts the OAuth flow by redirecting to Google with the user's ID and company ID in state
    """
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company_id format")

    # Pack user_id and company_id into state (JSON then urlencode)
    state = json.dumps({"user_id": user_id, "company_id": company_id})
    state_encoded = urllib.parse.quote(state)

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=email%20profile%20https://www.googleapis.com/auth/gmail.readonly%20https://www.googleapis.com/auth/gmail.send%20https://www.googleapis.com/auth/userinfo.email"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={state_encoded}"
    )

    return RedirectResponse(url=auth_url)

#/api/v1/gmail/oauth/callback
@router.get("/oauth/callback")
async def google_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    if not state:
        raise HTTPException(status_code=400, detail="Missing state parameter")

    try:
        # Decode and parse state
        decoded_state = urllib.parse.unquote(state)
        state_data = json.loads(decoded_state)

        user_id = state_data.get("user_id")
        company_id = state_data.get("company_id")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid state: {str(e)}")

    if not user_id or not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Missing or invalid user_id")
    
    if not company_id or not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Missing or invalid company_id")

    user_id = ObjectId(user_id)
    company_id = ObjectId(company_id)

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            token_data = token_resp.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid token response from Google")

        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "Failed to get tokens"))

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")

    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access token")

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in or 3600)

    # Get user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        userinfo = userinfo_resp.json()
        email = userinfo.get("email")

    if not email:
        raise HTTPException(status_code=400, detail="Failed to retrieve user email")

    # Build credentials
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )

    # Call Gmail API in threadpool (avoid blocking)
    def watch_gmail():
        gmail = build("gmail", "v1", credentials=creds)
        watch_request = {
            "labelIds": ["INBOX"],
            "topicName": f"projects/{settings.PUBSUB_PROJECT}/topics/{settings.PUBSUB_TOPIC}",
        }
        return gmail.users().watch(userId="me", body=watch_request).execute()

    loop = asyncio.get_running_loop()
    watch_response = await loop.run_in_executor(None, watch_gmail)
    print(watch_response)

    history_id = watch_response["historyId"]

    # ✅ Ensure Pub/Sub subscription exists
    service_account_info = json.loads(settings.SERVICE_ACCOUNT_JSON)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/pubsub"]
    )

    def ensure_subscription():
        subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
        topic_path = subscriber.topic_path(settings.PUBSUB_PROJECT, settings.PUBSUB_TOPIC)
        subscription_path = subscriber.subscription_path(settings.PUBSUB_PROJECT, settings.PUBSUB_SUBSCRIPTION)

        try:
            subscriber.get_subscription(request={"subscription": subscription_path})
        except Exception:
            subscriber.create_subscription(
                request={
                    "name": subscription_path, 
                    "topic": topic_path,
                    "push_config": {
                        "push_endpoint": f"{settings.BACKEND_URL}/api/v1/gmail/pubsub/push"
                    }
                }
            )
        return subscription_path

    subscription_path = await loop.run_in_executor(None, ensure_subscription)

    # Save/update Gmail account
    db = request.app.state.db
    existing = await db.gmail_accounts.find_one({"email": email, "user_id": user_id})

    account_data = {
        "email": email,
        "user_id": user_id,
        "company_id": company_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "status": "connected",
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "token_issued_at": datetime.utcnow(),
        "provider": "google",
        "history_id": history_id,
        "subscription": subscription_path,  # ✅ store subscription
    }

    if existing:
        await db.gmail_accounts.update_one({"_id": existing["_id"]}, {"$set": account_data})
    else:
        await db.gmail_accounts.insert_one(account_data)

    return RedirectResponse(url=f"{FRONTEND_URL}/accounts/gmail")

#/api/v1/gmail/pubsub/push
@router.post("/pubsub/push") 
async def pubsub_push(request: Request, db=Depends(get_database)):
    try:
        body = await request.json()
    except Exception as e:
        logger.error("Invalid JSON payload from Pub/Sub", exc_info=True)
        return Response(status_code=400)

    message = body.get("message")
    if not message or "data" not in message:
        logger.warning("Pub/Sub message missing 'data' field")
        return Response(status_code=400)

    try:
        # Decode Pub/Sub base64 payload
        data = json.loads(base64.urlsafe_b64decode(message["data"]).decode("utf-8"))
    except Exception:
        logger.error("Failed to decode Pub/Sub data", exc_info=True)
        return Response(status_code=400)

    email_address = data.get("emailAddress")
    history_id = data.get("historyId")

    if not email_address or not history_id:
        logger.warning("Pub/Sub payload missing emailAddress or historyId: %s", data)
        return Response(status_code=200)

    logger.info("📩 Gmail change detected", extra={"email": email_address, "historyId": history_id})

    account = await db["gmail_accounts"].find_one({"email": email_address})
    if not account:
        logger.info("No account found for %s", email_address)
        return Response(status_code=200)

    user_id = account["user_id"]
    company_id = account["company_id"]

    try:
        service = get_gmail_service(account)
    except Exception:
        logger.error("Failed to initialize Gmail API service for %s", email_address, exc_info=True)
        return Response(status_code=500)

    last_history_id = account.get("history_id")
    if not last_history_id:
        logger.debug("No stored historyId for %s. Skipping history fetch.", email_address)
    else:
        try:
            results = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id
            ).execute()

            history = results.get("history", [])
        except Exception:
            logger.error("Failed fetching Gmail history for %s", email_address, exc_info=True)
            return Response(status_code=500)

        for record in history:
            if "messagesAdded" not in record:
                continue

            for added in record["messagesAdded"]:
                gmail_id = added["message"]["id"]

                try:
                    full_msg = service.users().messages().get(
                        userId="me",
                        id=gmail_id,
                        format="full" 
                    ).execute()
                except Exception:
                    logger.error("Failed fetching Gmail message %s", gmail_id, exc_info=True)
                    continue
                
                labels = full_msg.get("labelIds", [])
                if "INBOX" not in labels:
                    continue
                thread_id = full_msg.get("threadId", gmail_id)
                payload = full_msg.get("payload", {}) or {}
                headers = payload.get("headers", [])

                subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
                sender = next((h["value"] for h in headers if h["name"] == "From"), "")
                to = next((h["value"] for h in headers if h["name"] == "To"), "")
                date = next((h["value"] for h in headers if h["name"] == "Date"), "")

                try:
                    timestamp = parsedate_to_datetime(date)
                except Exception:
                    timestamp = datetime.utcnow()

                # --- Extract bodies ---
                text_body, html_body = "", ""

                def extract_bodies(payload):
                    nonlocal text_body, html_body
                    if "parts" in payload:
                        for part in payload["parts"]:
                            mime_type = part.get("mimeType")
                            data = part["body"].get("data", "")
                            if data:
                                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                                if mime_type == "text/plain" and not text_body:
                                    text_body = decoded
                                elif mime_type == "text/html" and not html_body:
                                    html_body = decoded
                            if "parts" in part:
                                extract_bodies(part)
                    else:
                        data = payload.get("body", {}).get("data", "")
                        if data:
                            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                            mime_type = payload.get("mimeType")
                            if mime_type == "text/plain":
                                text_body = decoded
                            elif mime_type == "text/html":
                                html_body = decoded

                extract_bodies(payload)
                content = html_body if html_body else text_body

                chat_entry = ChatEntry(
                    sender=sender,
                    recipient=to,
                    content=content,
                    title=subject,
                    timestamp=timestamp,
                    channel="email",
                    message_type="html",
                    metadata={
                        "gmail_id": gmail_id,
                        "from": sender,
                        "to": to,
                        "subject": subject,
                        "date": date
                    }
                )

                existing_thread = await db["messages"].find_one(
                    {"user_id": ObjectId(user_id), "thread_id": thread_id, "channel": "email"}
                )

                if existing_thread:
                    if any(m.get("metadata", {}).get("gmail_id") == gmail_id for m in existing_thread.get("messages", [])):
                        logger.debug("Duplicate Gmail %s ignored for thread %s", gmail_id, thread_id)
                        continue

                    participants = existing_thread.get("participants", [])
                    for p in [sender, to]:
                        if p not in participants:
                            participants.append(p)

                    await db["messages"].update_one(
                        {"_id": existing_thread["_id"]},
                        {
                            "$push": {"messages": chat_entry.dict()},
                            "$set": {
                                "last_updated": timestamp,
                                "title": subject,
                                "participants": participants
                            }
                        }
                    )
                else:

                    shopify_order_match = re.search(r"#([A-Z]{2}\d+)", subject or content or "")
                    shopify_order = shopify_order_match.group(1) if shopify_order_match else None
                    
                    # Generate new ticket number
                    today = datetime.utcnow().strftime("%Y-%m-%d")
                    count_today = await db["messages"].count_documents({
                        "company_id": ObjectId(company_id),
                        "started_at": {"$gte": datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)}
                    })
                    ticket_number = f"CA-{today}-{count_today + 1:04d}"

                    logger.info(f"Creating new ticket {ticket_number} for order {shopify_order}")
                    
                    message_doc = {
                        "user_id": ObjectId(user_id),
                        "company_id": ObjectId(company_id),
                        "thread_id": thread_id,
                        "participants": list(set([sender, to])),
                        "channel": "email",
                        "status": "Open",
                        "title": subject,
                        "ticket": ticket_number,
                        "client": sender,
                        "agent": to,
                        "messages": [chat_entry.dict()],
                        "last_updated": timestamp,
                        "started_at": timestamp,
                        "ai_summary": None,
                        "tags": [],
                        "resolved_by_ai": False
                    }
                    await db["messages"].insert_one(message_doc)

                await sio.emit(
                    "gmail_update",
                    {
                        "user_id": str(user_id),
                        "company_id": str(company_id),
                        "email": email_address,
                        "message": f"New messages pushed for {email_address}"
                    }
                )
                
    await db["gmail_accounts"].update_one(
        {"_id": account["_id"]},
        {"$set": {"history_id": history_id}}
    )

    logger.info("✅ Processed Gmail Pub/Sub for %s up to historyId=%s", email_address, history_id)
    return Response(status_code=200)



