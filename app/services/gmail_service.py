import base64
from datetime import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from app.models.message import Message, ChatEntry 
from bson import ObjectId
import logging
import requests

async def fetch_and_save_gmail(account: dict, db, user_id: str, company_id: str):
    creds = Credentials(
        token=account["access_token"],
        refresh_token=account["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )

    # Token expiration check
    expires_at = account.get("expires_at")
    
    token_expired = False
    if expires_at:
        try:
            expires_at_dt = (
                datetime.fromisoformat(expires_at)
                if isinstance(expires_at, str) else expires_at
            )
            if expires_at_dt.tzinfo:
                expires_at_dt = expires_at_dt.astimezone(tz=None).replace(tzinfo=None)
            token_expired = datetime.utcnow() >= expires_at_dt
        except Exception as e:
            logging.warning(f"Could not parse expires_at: {expires_at} ({e})")
            token_expired = creds.expired
    else:
        token_expired = creds.expired

    if token_expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logging.error(f"Failed to refresh token for {account['email']}: {e}")
            return f"Token refresh failed for {account['email']}"

    try:
        token_info = requests.get(
            f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={creds.token}"
        ).json()
        if "https://www.googleapis.com/auth/gmail.readonly" not in token_info.get("scope", ""):
            return f"Insufficient permissions: 'gmail.readonly' not in token scopes for {account['email']}"
    except Exception as e:
        logging.warning(f"Token scope check failed: {e}")

    try:
        service = build("gmail", "v1", credentials=creds)
        result = service.users().messages().list(
            userId="me",
            maxResults=10
        ).execute()

        messages = result.get("messages", [])
        stored_count = 0

        for msg in messages:
            gmail_id = msg["id"]
            full_msg = service.users().messages().get(
                userId="me", id=gmail_id, format="full"
            ).execute()
            thread_id = full_msg.get("threadId", gmail_id)
            payload = full_msg.get("payload", {})
            headers = payload.get("headers", [])

            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            sender = next((h["value"] for h in headers if h["name"] == "From"), "")
            to = next((h["value"] for h in headers if h["name"] == "To"), "")
            date = next((h["value"] for h in headers if h["name"] == "Date"), "")

            try:
                timestamp = datetime.strptime(date[:25], "%a, %d %b %Y %H:%M:%S")
            except Exception:
                timestamp = datetime.utcnow()

            # Extract plain text and HTML body
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
                message_type="html" if html_body else "text",
                metadata={
                    "gmail_id": gmail_id,
                    "from": sender,
                    "to": to,
                    "subject": subject,
                    "date": date
                }
            )

            # Find existing thread (conversation) in 'messages' collection by thread_id
            existing_thread = await db["messages"].find_one({"user_id": ObjectId(user_id), "thread_id": thread_id, "channel": "email"})

            # Avoid duplicate insert of the same gmail_id in a thread
            if existing_thread:
                if any(m.get("metadata", {}).get("gmail_id") == gmail_id for m in existing_thread.get("messages", [])):
                    continue
                await db["messages"].update_one(
                    {"_id": existing_thread["_id"]},
                    {
                        "$push": {"messages": chat_entry.dict()},
                        "$set": {
                            "last_updated": timestamp,
                            "title": subject,
                            "participants": list(set(existing_thread.get("participants", []) + [sender, to]))
                        }
                    }
                )
            else:
                message_doc = {
                    "user_id": ObjectId(user_id),
                    "company_id": ObjectId(company_id),
                    "thread_id": thread_id,
                    "participants": list(set([sender, to])),
                    "channel": "email",
                    "status": "Open",
                    "title": subject,
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
            stored_count += 1

        return f"Fetched and stored {stored_count} new messages (grouped by thread) for {account['email']}"

    except Exception as e:
        logging.exception(f"Error fetching emails for {account['email']}: {str(e)}")
        return f"Failed to fetch emails for {account['email']} due to an error."
    
async def fetch_all_gmail_accounts(db, user_id: str, company_id: str):
    cursor = db["gmail_accounts"].find({"user_id": ObjectId(user_id)})
    results = []
    async for cred in cursor:
        try:
            token_data = {
                "email": cred["email"],  # <-- include email here
                "access_token": cred["access_token"],
                "refresh_token": cred["refresh_token"],
                "client_id": cred["client_id"],
                "client_secret": cred["client_secret"],
                "expires_at": cred.get("expires_at")
            }

            result = await fetch_and_save_gmail(token_data, db, user_id, company_id)  # now only 2 args
            results.append({cred["email"]: result})
        except Exception as e:
            results.append({cred["email"]: f"Error: {str(e)}"})

    return results

def get_gmail_service(user_credentials: dict):
    """
    Returns an authenticated Gmail API service for the given user's credentials.
    user_credentials: dict with keys such as token, refresh_token, client_id, client_secret, token_uri, scopes
    """
    creds = Credentials(
        token=user_credentials['access_token'],
        refresh_token=user_credentials.get('refresh_token'),
        token_uri=user_credentials.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=user_credentials['client_id'],
        client_secret=user_credentials['client_secret'],
        scopes=user_credentials.get('scopes', ['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/gmail.readonly']),
    )

    request = Request()

   # Refresh the credentials
    try:
        creds.refresh(request)
        print("Credentials refreshed successfully!")
        print(f"New access token: {creds.token}")
    except Exception as e:
        print(f"Error refreshing credentials: {e}")

    service = build('gmail', 'v1', credentials=creds)
    return service