from fastapi import APIRouter, Request, HTTPException, Response, Depends, Body
from fastapi.responses import RedirectResponse
from typing import List, Optional, Dict, Any
import logging
import httpx
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from app.core.security import get_current_user
from app.core.audit import record_audit_log
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
import os
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import pubsub_v1
from app.core.config import settings
import asyncio
import json
import base64
import urllib.parse
import re
from app.db.mongodb import get_database
from app.services.gmail_service import fetch_and_save_gmail, get_gmail_service
from app.services.deleted_gmail_service import is_deleted_gmail_message
from app.services.processed_gmail_service import claim_gmail_message, release_gmail_message_claim
from app.services.gmail_attachment_service import extract_gmail_attachments
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
from app.utils.datetime_utils import to_utc_iso
import re

logger = logging.getLogger("attentify.gmail")

router = APIRouter()

GMAIL_REAUTH_REQUIRED_MESSAGE = (
    "Google refresh token is invalid or revoked. Reconnect this Gmail account."
)


def _gmail_header(headers: list[dict], name: str) -> str:
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()),
        "",
    )


def _account_store_ids(account: dict) -> list[ObjectId]:
    values = account.get("store_ids")
    if not values and account.get("store_id"):
        values = [account.get("store_id")]
    result = []
    for value in values or []:
        if isinstance(value, ObjectId):
            result.append(value)
        elif ObjectId.is_valid(str(value)):
            result.append(ObjectId(str(value)))
    return result


async def _load_store_scope(db: AsyncIOMotorDatabase, company_id: ObjectId, store_ids: list[ObjectId]) -> list[dict]:
    if not store_ids:
        return []
    stores = await db["shopify_cred"].find({
        "_id": {"$in": store_ids},
        "company_id": company_id,
        "status": {"$ne": "disconnected"},
    }).to_list(length=100)
    by_id = {store["_id"]: store for store in stores}
    return [by_id[store_id] for store_id in store_ids if store_id in by_id]


def _message_match_conditions(account_id: ObjectId, account_email: str | None) -> list[dict]:
    conditions = [{"gmail_account_id": account_id}]
    if account_email:
        email_pattern = re.escape(account_email)
        conditions.extend([
            {"inbox_email": account_email},
            {"messages": {"$elemMatch": {"metadata.to": {"$regex": email_pattern, "$options": "i"}}}},
        ])
    return conditions


async def mark_gmail_account_disconnected(db, account: dict, error_message: str = GMAIL_REAUTH_REQUIRED_MESSAGE):
    await db["gmail_accounts"].update_one(
        {"_id": account["_id"]},
        {
            "$set": {
                "status": "disconnected",
                "last_error": error_message,
                "last_error_at": datetime.now(timezone.utc),
            },
            "$unset": {"watch_expiration": ""},
        },
    )

def gmail_account_helper(account: dict) -> dict:
    store_ids = _account_store_ids(account)
    return {
        "id": str(account["_id"]),
        "user_id": str(account["user_id"]),
        "email": account["email"],
        "access_token": account["access_token"],
        "refresh_token": account["refresh_token"],
        "token_type": account.get("token_type", "Bearer"),
        "expires_at": to_utc_iso(account.get("expires_at")),
        "client_id": account["client_id"],
        "client_secret": account["client_secret"],
        "status": account.get("status", "connected"),
        "scope": account.get("scope"),
        "token_issued_at": to_utc_iso(account.get("token_issued_at")),
        "is_primary": account.get("is_primary", False),
        "provider": account.get("provider", "google"),
        "history_id":  account.get("history_id", ""),
        "store": account.get("store", ""),
        "store_ids": [str(store_id) for store_id in store_ids],
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
    
    # Check membership
    membership = await db["memberships"].find_one(
        {"user_id": current_user["_id"], "company_id": ObjectId(company_id)}
    )

    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")
    
    role = membership.get("role")

    if role in ("company_owner", "store_owner", "agent"):
        accounts_cursor = db.gmail_accounts.find({"company_id": ObjectId(company_id)})
    elif role == "readonly":
        accounts_cursor = db.gmail_accounts.find({"company_id": ObjectId(company_id)})
    else:
        accounts_cursor = db.gmail_accounts.find({"user_id": current_user["_id"]})
    
    accounts = []
    async for account in accounts_cursor:
        owner = await db.users.find_one({"_id": account["user_id"]})
        if not owner:
            continue
        account_data = gmail_account_helper(account)
        account_data["owner_email"] = owner.get("email", "unknown")
        account_data["owner_name"] = f"{owner.get('first_name', 'unknown')} {owner.get('last_name', 'unknown')}"
        scoped_stores = await _load_store_scope(db, ObjectId(company_id), _account_store_ids(account))
        account_data["stores"] = [
            {"id": str(store["_id"]), "shop": store.get("shop", "")}
            for store in scoped_stores
        ]
        account_data["store"] = account_data["stores"][0] if len(account_data["stores"]) == 1 else None
        accounts.append(account_data)

    # Fetch company stores so an inbox can be scoped to any connected store in the company.
    stores_cursor = db.shopify_cred.find({
        "company_id": ObjectId(company_id),
        "status": {"$ne": "disconnected"},
    })
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

    try:
        account_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Gmail account id")

    account = await db["gmail_accounts"].find_one({"_id": account_id})
    if not account:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    
    update_query = {}
    message_update_query = None

    if field in {"store_id", "store_ids"}:
        raw_values = value if isinstance(value, list) else ([value] if value else [])
        store_ids = []
        for item in raw_values:
            if not item:
                continue
            try:
                store_ids.append(ObjectId(item))
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid store_id format")

        stores = await _load_store_scope(db, account.get("company_id"), store_ids)
        if len(stores) != len(store_ids):
            raise HTTPException(status_code=404, detail="One or more Shopify stores were not found")

        if not store_ids:
            update_query = {"$unset": {"store_id": "", "store_ids": ""}}
            message_update_query = {
                "$set": {
                    "gmail_account_id": account_id,
                    "inbox_email": account.get("email"),
                },
                "$unset": {
                    "default_store_id": "",
                    "default_store_shop": "",
                    "order_matching_store_ids": "",
                    "order_matching_store_shops": "",
                },
            }
        else:
            set_payload = {
                "store_ids": store_ids,
                "store_id": store_ids[0] if len(store_ids) == 1 else None,
            }
            unset_payload = {} if len(store_ids) == 1 else {"store_id": ""}
            update_query = {"$set": {k: v for k, v in set_payload.items() if v is not None}}
            if unset_payload:
                update_query["$unset"] = unset_payload

            message_set = {
                "gmail_account_id": account_id,
                "inbox_email": account.get("email"),
                "order_matching_store_ids": store_ids,
                "order_matching_store_shops": [store.get("shop") for store in stores],
            }
            if len(stores) == 1:
                message_set["default_store_id"] = stores[0]["_id"]
                message_set["default_store_shop"] = stores[0].get("shop")
                message_update_query = {"$set": message_set}
            else:
                message_update_query = {
                    "$set": message_set,
                    "$unset": {
                        "default_store_id": "",
                        "default_store_shop": "",
                    },
                }
    else:
        update_query = {"$set": {field: value}}

    result = await db["gmail_accounts"].update_one(
        {"_id": account_id},
        update_query
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    if field in {"store_id", "store_ids"} and message_update_query:
        message_match_conditions = _message_match_conditions(account_id, account.get("email"))
        await db["messages"].update_many(
            {
                "company_id": account.get("company_id"),
                "$or": message_match_conditions,
            },
            message_update_query,
        )

    return {"message": f"{field} updated"}


@router.delete("/{account_id}", status_code=204)
async def delete_gmail_account(
    account_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = request.app.state.db
    account = await db.gmail_accounts.find_one({"_id": ObjectId(account_id)})
    if not account:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": account.get("company_id"),
        "status": "active",
    })
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")

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
        logger.warning("Failed to stop watch for %s: %s", account['email'], e)

    # Step 2: Delete from DB
    result = await db.gmail_accounts.delete_one({"_id": ObjectId(account_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    await record_audit_log(
        db,
        company_id=account.get("company_id"),
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action="Removed Gmail account",
        entity_type="gmail_account",
        entity_id=ObjectId(account_id),
        details={"email": account.get("email", "")},
    )

    return None

# Environment variables for Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPES = [
    "email",
    "profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
REQUIRED_GMAIL_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
}
GOOGLE_SCOPE = " ".join(GMAIL_SCOPES)


def has_required_gmail_scopes(scope_value: str) -> bool:
    granted_scopes = set((scope_value or "").split())
    return REQUIRED_GMAIL_SCOPES.issubset(granted_scopes)


def get_pubsub_credentials():
    service_account_info = json.loads(settings.SERVICE_ACCOUNT_JSON)
    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/pubsub"],
    )


def ensure_gmail_pubsub_resources(credentials):
    publisher = pubsub_v1.PublisherClient(credentials=credentials)
    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    topic_path = publisher.topic_path(settings.PUBSUB_PROJECT, settings.PUBSUB_TOPIC)
    subscription_path = subscriber.subscription_path(
        settings.PUBSUB_PROJECT,
        settings.PUBSUB_SUBSCRIPTION,
    )
    push_endpoint = f"{settings.BACKEND_URL}/api/v1/gmail/pubsub/push"

    try:
        publisher.get_topic(request={"topic": topic_path})
    except Exception:
        publisher.create_topic(request={"name": topic_path})

    try:
        subscription = subscriber.get_subscription(request={"subscription": subscription_path})
        existing_endpoint = subscription.push_config.push_endpoint
        if existing_endpoint != push_endpoint:
            subscriber.modify_push_config(
                request={
                    "subscription": subscription_path,
                    "push_config": {"push_endpoint": push_endpoint},
                }
            )
    except Exception:
        subscriber.create_subscription(
            request={
                "name": subscription_path,
                "topic": topic_path,
                "push_config": {"push_endpoint": push_endpoint},
            }
        )

    return topic_path, subscription_path

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

    # Pack user_id and company_id into state.
    state = json.dumps({"user_id": user_id, "company_id": company_id})
    auth_params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(auth_params)}"

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

    granted_scope = token_data.get("scope", "")
    if not has_required_gmail_scopes(granted_scope):
        raise HTTPException(
            status_code=400,
            detail=(
                "Gmail permission was not granted. Please reconnect Gmail and approve "
                "Gmail read/send access. Granted scopes: "
                f"{granted_scope or 'none'}"
            ),
        )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in or 3600)

    db = request.app.state.db

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

    existing = await db.gmail_accounts.find_one({"email": email, "user_id": user_id})
    if not refresh_token and existing:
        refresh_token = existing.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Google did not return a refresh token. Please remove access for Attentify in your Google account and connect Gmail again.",
        )

    # Build credentials
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    )

    loop = asyncio.get_running_loop()

    try:
        pubsub_credentials = get_pubsub_credentials()
        topic_path, subscription_path = await loop.run_in_executor(
            None,
            ensure_gmail_pubsub_resources,
            pubsub_credentials,
        )
    except Exception:
        logger.error("Failed to prepare Gmail Pub/Sub resources", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Failed to prepare Gmail Pub/Sub resources",
        )

    # Call Gmail API in threadpool (avoid blocking)
    def watch_gmail():
        gmail = build("gmail", "v1", credentials=creds)
        watch_request = {
            "labelIds": ["INBOX"],
            "topicName": topic_path,
        }
        return gmail.users().watch(userId="me", body=watch_request).execute()

    try:
        watch_response = await loop.run_in_executor(None, watch_gmail)
    except HttpError as e:
        logger.error("Failed to start Gmail watch for OAuth callback", exc_info=True)
        if getattr(e, "resp", None) and e.resp.status == 403:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google rejected the Gmail watch request because the connected "
                    "account does not have Gmail API permission. Remove Attentify "
                    "from Google third-party access and connect Gmail again."
                ),
            )
        if getattr(e, "resp", None) and e.resp.status == 400:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google rejected the Gmail watch topic. Set PUBSUB_PROJECT to "
                    "the same Google Cloud project ID as the OAuth client and make "
                    "sure the Pub/Sub topic exists in that project."
                ),
            )
        raise HTTPException(status_code=502, detail="Failed to start Gmail watch")
    logger.debug("Gmail watch response: %s", watch_response)

    history_id = watch_response["historyId"]

    # Save/update Gmail account
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
        "scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ],
        "scope": token_data.get("scope"),
        "token_issued_at": datetime.now(timezone.utc),
        "provider": "google",
        "history_id": history_id,
        "watch_expiration": watch_response.get("expiration"),
        "last_watch_renewed_at": datetime.now(timezone.utc),
        "subscription": subscription_path,
    }

    if existing:
        await db.gmail_accounts.update_one({"_id": existing["_id"]}, {"$set": account_data})
        account_id = existing["_id"]
    else:
        result = await db.gmail_accounts.insert_one(account_data)
        account_id = result.inserted_id

    actor = await db["users"].find_one({"_id": user_id})
    membership = await db["memberships"].find_one({
        "user_id": user_id,
        "company_id": company_id,
        "status": "active",
    })
    await record_audit_log(
        db,
        company_id=company_id,
        actor=actor,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action="Connected Gmail account",
        entity_type="gmail_account",
        entity_id=account_id,
        details={"email": email},
    )

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
        # Decode Pub/Sub base64 payload. Pub/Sub data can arrive without padding.
        encoded_data = message["data"]
        encoded_data += "=" * (-len(encoded_data) % 4)
        data = json.loads(base64.urlsafe_b64decode(encoded_data).decode("utf-8"))
    except Exception:
        logger.error("Failed to decode Pub/Sub data", exc_info=True)
        return Response(status_code=400)

    email_address = data.get("emailAddress")
    history_id = data.get("historyId")

    if not email_address or not history_id:
        logger.warning("Pub/Sub payload missing emailAddress or historyId: %s", data)
        return Response(status_code=200)

    logger.info("Gmail change detected", extra={"email": email_address, "historyId": history_id})

    account = await db["gmail_accounts"].find_one({"email": email_address})
    if not account:
        logger.info("No account found for %s", email_address)
        return Response(status_code=200)

    user_id = account["user_id"]
    company_id = account["company_id"]
    user_object_id = user_id if isinstance(user_id, ObjectId) else ObjectId(user_id)
    company_object_id = company_id if isinstance(company_id, ObjectId) else ObjectId(company_id)

    try:
        service = get_gmail_service(account)
    except RefreshError:
        logger.warning(
            "Gmail credentials need reconnect for %s; acknowledging Pub/Sub push.",
            email_address,
            exc_info=True,
        )
        await mark_gmail_account_disconnected(db, account)
        return Response(status_code=200)
    except Exception:
        logger.error("Failed to initialize Gmail API service for %s", email_address, exc_info=True)
        return Response(status_code=500)

    last_history_id = account.get("history_id")
    if not last_history_id:
        logger.info("No stored historyId for %s. Setting Gmail baseline to %s.", email_address, history_id)
        await fetch_and_save_gmail(
            {
                "account_id": account["_id"],
                "email": account["email"],
                "access_token": account["access_token"],
                "refresh_token": account["refresh_token"],
                "client_id": account["client_id"],
                "client_secret": account["client_secret"],
                "expires_at": account.get("expires_at"),
                "history_id": account.get("history_id"),
                "scope": account.get("scope"),
                "scopes": account.get("scopes"),
                "store_ids": _account_store_ids(account),
                "store_shops": [
                    store.get("shop")
                    for store in await _load_store_scope(
                        db,
                        company_object_id,
                        _account_store_ids(account),
                    )
                ],
            },
            db,
            str(user_object_id),
            str(company_object_id),
            include_unread_backfill=True,
        )
        await db["gmail_accounts"].update_one(
            {"_id": account["_id"]},
            {"$set": {"history_id": history_id, "status": "connected"}},
        )
        return Response(status_code=200)
    else:
        try:
            history = []
            latest_history_id = history_id
            page_token = None
            while True:
                results = service.users().history().list(
                    userId="me",
                    startHistoryId=str(last_history_id),
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                ).execute()
                latest_history_id = results.get("historyId") or latest_history_id
                history.extend(results.get("history", []))
                page_token = results.get("nextPageToken")
                if not page_token:
                    break
        except RefreshError:
            logger.warning(
                "Gmail credentials need reconnect while fetching history for %s; acknowledging Pub/Sub push.",
                email_address,
                exc_info=True,
            )
            await mark_gmail_account_disconnected(db, account)
            return Response(status_code=200)
        except HttpError as e:
            status_code = getattr(getattr(e, "resp", None), "status", None)
            if status_code in (400, 404):
                logger.warning(
                    "Gmail historyId expired for %s. Recovering unread messages and resetting baseline to %s.",
                    email_address,
                    history_id,
                )
                await fetch_and_save_gmail(
                    {
                        "account_id": account["_id"],
                        "email": account["email"],
                        "access_token": account["access_token"],
                        "refresh_token": account["refresh_token"],
                        "client_id": account["client_id"],
                        "client_secret": account["client_secret"],
                        "expires_at": account.get("expires_at"),
                        "history_id": account.get("history_id"),
                        "scope": account.get("scope"),
                        "scopes": account.get("scopes"),
                        "store_ids": _account_store_ids(account),
                        "store_shops": [
                            store.get("shop")
                            for store in await _load_store_scope(
                                db,
                                company_object_id,
                                _account_store_ids(account),
                            )
                        ],
                    },
                    db,
                    str(user_object_id),
                    str(company_object_id),
                    include_unread_backfill=True,
                )
                await db["gmail_accounts"].update_one(
                    {"_id": account["_id"]},
                    {"$set": {"history_id": history_id, "status": "connected"}},
                )
                return Response(status_code=200)
            logger.error("Failed fetching Gmail history for %s", email_address, exc_info=True)
            return Response(status_code=500)
        except Exception:
            logger.error("Failed fetching Gmail history for %s", email_address, exc_info=True)
            return Response(status_code=500)

    for record in history:
        if "messagesAdded" not in record:
            continue

        for added in record["messagesAdded"]:
            gmail_id = added["message"]["id"]
            if await is_deleted_gmail_message(
                db,
                company_id=company_object_id,
                user_id=user_object_id,
                gmail_id=gmail_id,
            ):
                logger.debug("Deleted Gmail %s ignored for %s", gmail_id, email_address)
                continue

            if not await claim_gmail_message(
                db,
                company_id=company_object_id,
                user_id=user_object_id,
                gmail_id=gmail_id,
            ):
                logger.debug("Already processed Gmail %s ignored for %s", gmail_id, email_address)
                continue

            try:
                full_msg = service.users().messages().get(
                    userId="me",
                    id=gmail_id,
                    format="full"
                ).execute()
            except RefreshError:
                await release_gmail_message_claim(
                    db,
                    company_id=company_object_id,
                    user_id=user_object_id,
                    gmail_id=gmail_id,
                )
                logger.warning(
                    "Gmail credentials need reconnect while fetching message %s for %s; acknowledging Pub/Sub push.",
                    gmail_id,
                    email_address,
                    exc_info=True,
                )
                await mark_gmail_account_disconnected(db, account)
                return Response(status_code=200)
            except Exception:
                await release_gmail_message_claim(
                    db,
                    company_id=company_object_id,
                    user_id=user_object_id,
                    gmail_id=gmail_id,
                )
                logger.error("Failed fetching Gmail message %s", gmail_id, exc_info=True)
                continue

            labels = full_msg.get("labelIds", [])
            if "INBOX" not in labels:
                await release_gmail_message_claim(
                    db,
                    company_id=company_object_id,
                    user_id=user_object_id,
                    gmail_id=gmail_id,
                )
                continue
            thread_id = full_msg.get("threadId", gmail_id)
            payload = full_msg.get("payload", {}) or {}
            headers = payload.get("headers", [])

            subject = _gmail_header(headers, "Subject")
            sender = _gmail_header(headers, "From")
            to = _gmail_header(headers, "To")
            date = _gmail_header(headers, "Date")
            rfc_message_id = _gmail_header(headers, "Message-ID")
            in_reply_to = _gmail_header(headers, "In-Reply-To")
            references = _gmail_header(headers, "References")

            try:
                timestamp = parsedate_to_datetime(date)
                if timestamp is None:
                    raise ValueError("Unable to parse date header")
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                else:
                    timestamp = timestamp.astimezone(timezone.utc)
            except Exception:
                timestamp = datetime.now(timezone.utc)

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
                    "date": date,
                    "rfc_message_id": rfc_message_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "attachments": extract_gmail_attachments(
                        payload,
                        gmail_message_id=gmail_id,
                        account_email=account.get("email"),
                    ),
                }
            )

            scoped_stores = await _load_store_scope(
                db,
                account.get("company_id"),
                _account_store_ids(account),
            )
            message_context = {
                "gmail_account_id": account.get("_id"),
                "inbox_email": account.get("email"),
                "order_matching_store_ids": [store["_id"] for store in scoped_stores],
                "order_matching_store_shops": [store.get("shop") for store in scoped_stores],
            }
            if len(scoped_stores) == 1:
                message_context["default_store_id"] = scoped_stores[0]["_id"]
                message_context["default_store_shop"] = scoped_stores[0].get("shop")

            existing_thread = await db["messages"].find_one(
                {"user_id": user_object_id, "thread_id": thread_id, "channel": "email"}
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
                            "participants": participants,
                            **{k: v for k, v in message_context.items() if v},
                        }
                    }
                )
            else:
                shopify_order_match = re.search(r"#([A-Z]{2}\d+)", subject or content or "")
                shopify_order = shopify_order_match.group(1) if shopify_order_match else None

                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                count_today = await db["messages"].count_documents({
                    "company_id": company_object_id,
                    "started_at": {"$gte": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)}
                })
                ticket_number = f"CA-{today}-{count_today + 1:04d}"

                logger.info(f"Creating new ticket {ticket_number} for order {shopify_order}")

                message_doc = {
                    "user_id": user_object_id,
                    "company_id": company_object_id,
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
                message_doc.update({k: v for k, v in message_context.items() if v})
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
        {"$set": {"history_id": latest_history_id, "status": "connected"}}
    )

    logger.info("Processed Gmail Pub/Sub for %s up to historyId=%s", email_address, latest_history_id)
    return Response(status_code=200)
