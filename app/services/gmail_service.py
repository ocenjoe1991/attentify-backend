import base64
import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.models.message import Message, ChatEntry 
from app.services.deleted_gmail_service import is_deleted_gmail_message
from app.services.processed_gmail_service import claim_gmail_message, release_gmail_message_claim
from app.services.gmail_attachment_service import extract_gmail_attachments
from bson import ObjectId
import logging
import requests

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TICKET_GENERATION_POLICY = "verified-order-reference-v1"
ticket_logger = logging.getLogger("gmail.ticketing")
ORDER_MENTION_PATTERN = re.compile(r"\border\b", re.IGNORECASE)
ORDER_REFERENCE_PATTERN = re.compile(
    r"(?<![\w#])#(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{3,}\b"
)
UNHASHED_ORDER_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,6}\d{3,}[A-Z0-9-]*|\d{3,}[A-Z]{2,6}[A-Z0-9-]*)\b"
)


class _VisibleEmailTextExtractor(HTMLParser):
    _ignored_tags = {"head", "style", "script", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.ignored_depth:
            self.ignored_depth += 1
            return
        if tag.lower() in self._ignored_tags:
            self.ignored_depth = 1
            return
        attributes = {name.lower(): (value or "").lower() for name, value in attrs}
        if attributes.get("aria-hidden") == "true" or "display:none" in attributes.get("style", "").replace(" ", ""):
            self.ignored_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _visible_email_text(content: str) -> str:
    extractor = _VisibleEmailTextExtractor()
    try:
        extractor.feed(content or "")
        extractor.close()
        return re.sub(r"\s+", " ", "".join(extractor.parts)).strip()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content or "")).strip()


def _order_reference_values(text: str) -> set[str]:
    """Return normalized order-like references found in visible email text."""
    matches = [
        *ORDER_REFERENCE_PATTERN.findall(text),
        *UNHASHED_ORDER_REFERENCE_PATTERN.findall(text),
    ]
    return {
        match.lstrip("#").upper()
        for match in matches
        if match.lstrip("#")
    }


async def should_generate_ticket_number(db, company_id, subject: str, content: str) -> bool:
    """Generate tickets for order requests, or for verified order references only."""
    text = f"{unescape(subject or '')} {_visible_email_text(content)}"
    if ORDER_MENTION_PATTERN.search(text):
        ticket_logger.info(
            "Ticket decision policy=%s company_id=%s eligible=true reason=order_keyword",
            TICKET_GENERATION_POLICY,
            company_id,
        )
        return True

    order_references = _order_reference_values(text)
    if not order_references:
        ticket_logger.info(
            "Ticket decision policy=%s company_id=%s eligible=false reason=no_order_keyword_or_reference",
            TICKET_GENERATION_POLICY,
            company_id,
        )
        return False

    company_object_id = company_id if isinstance(company_id, ObjectId) else ObjectId(company_id)
    matching_references = [
        {"name": {"$regex": f"^#?{re.escape(reference)}$", "$options": "i"}}
        for reference in order_references
    ]
    matching_order = await db["orders"].find_one(
        {"company_id": company_object_id, "$or": matching_references},
        {"_id": 1},
    )
    eligible = matching_order is not None
    ticket_logger.info(
        "Ticket decision policy=%s company_id=%s eligible=%s reason=%s reference_count=%d",
        TICKET_GENERATION_POLICY,
        company_id,
        str(eligible).lower(),
        "verified_order_reference" if eligible else "unmatched_order_reference",
        len(order_references),
    )
    return eligible


async def next_ticket_number(db, company_id) -> str:
    company_object_id = company_id if isinstance(company_id, ObjectId) else ObjectId(company_id)
    now = datetime.now(timezone.utc)
    prefix = f"CA-{now.strftime('%Y-%m-%d')}-"
    max_sequence = 0

    cursor = db["messages"].find(
        {"company_id": company_object_id, "ticket": {"$regex": f"^{re.escape(prefix)}"}},
        {"ticket": 1},
    )
    async for message in cursor:
        try:
            max_sequence = max(max_sequence, int(str(message["ticket"]).removeprefix(prefix)))
        except (KeyError, ValueError):
            continue

    return f"{prefix}{max_sequence + 1:04d}"

def _gmail_header(headers: list[dict], name: str) -> str:
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()),
        "",
    )


def _parse_expires_at(value):
    if not value:
        return None
    if isinstance(value, datetime):
        expires_at = value
    else:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        return expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc)


async def _save_refreshed_token(db, account: dict, creds: Credentials) -> None:
    update_data = {
        "access_token": creds.token,
        "status": "connected",
    }
    if creds.expiry:
        expires_at = creds.expiry
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        update_data["expires_at"] = expires_at

    await db["gmail_accounts"].update_one(
        {"_id": account["account_id"]},
        {"$set": update_data, "$unset": {"last_error": ""}},
    )


async def _refresh_gmail_token(db, account: dict, creds: Credentials):
    try:
        creds.refresh(Request())
        await _save_refreshed_token(db, account, creds)
        return None
    except Exception as e:
        error_text = str(e)
        logging.error(f"Failed to refresh token for {account['email']}: {e}")
        if "invalid_grant" in error_text:
            await db["gmail_accounts"].update_one(
                {"_id": account["account_id"]},
                {
                    "$set": {
                        "status": "disconnected",
                        "last_error": "Google refresh token is invalid or revoked. Reconnect this Gmail account.",
                        "last_error_at": datetime.now(timezone.utc),
                    }
                },
            )
            return {
                "email": account["email"],
                "status": "failed",
                "reason": "invalid_grant",
                "message": "Google refresh token is invalid or revoked. Reconnect this Gmail account.",
            }
        return {
            "email": account["email"],
            "status": "failed",
            "reason": "token_refresh_failed",
            "message": f"Token refresh failed for {account['email']}",
        }


def _fetch_token_info(access_token: str) -> dict:
    return requests.get(
        f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={access_token}",
        timeout=10,
    ).json()


def _stored_scopes(account: dict) -> set[str]:
    scopes = set()
    scope_value = account.get("scope")
    if isinstance(scope_value, str):
        scopes.update(scope_value.split())
    scopes_value = account.get("scopes")
    if isinstance(scopes_value, str):
        scopes.update(scopes_value.split())
    elif isinstance(scopes_value, list):
        scopes.update(str(scope) for scope in scopes_value)
    return scopes


def _message_timestamp_bounds(messages):
    timestamps = []
    for item in messages:
        if not isinstance(item, dict) or not item.get("timestamp"):
            continue
        timestamp = item["timestamp"]
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            else:
                timestamp = timestamp.astimezone(timezone.utc)
        timestamps.append(timestamp)
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


async def fetch_and_save_gmail(
    account: dict,
    db,
    user_id: str,
    company_id: str,
    update_existing: bool = False,
    force_full_sync: bool = False,
    include_unread_backfill: bool = False,
):
    creds = Credentials(
        token=account["access_token"],
        refresh_token=account["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )

    try:
        expires_at = _parse_expires_at(account.get("expires_at"))
        if expires_at:
            creds.expiry = expires_at.replace(tzinfo=None)
            token_expired = datetime.now(timezone.utc) >= expires_at
        else:
            token_expired = True
    except Exception as e:
        logging.warning(f"Could not parse expires_at: {account.get('expires_at')} ({e})")
        token_expired = True

    if token_expired and creds.refresh_token:
        refresh_error = await _refresh_gmail_token(db, account, creds)
        if refresh_error:
            return refresh_error

    try:
        token_info = _fetch_token_info(creds.token)
        if token_info.get("error") and creds.refresh_token:
            refresh_error = await _refresh_gmail_token(db, account, creds)
            if refresh_error:
                return refresh_error
            token_info = _fetch_token_info(creds.token)
        if token_info.get("error"):
            return {
                "email": account["email"],
                "status": "failed",
                "reason": "tokeninfo_failed",
                "message": f"Google token check failed for {account['email']}: {token_info.get('error_description') or token_info.get('error')}",
            }
        tokeninfo_scopes = set((token_info.get("scope") or "").split())
        if GMAIL_READONLY_SCOPE not in tokeninfo_scopes:
            if GMAIL_READONLY_SCOPE in _stored_scopes(account):
                logging.warning(
                    "Tokeninfo scope for %s does not include gmail.readonly, but stored OAuth scope does. "
                    "Continuing and letting Gmail API validate access. tokeninfo_scope=%s",
                    account["email"],
                    token_info.get("scope", ""),
                )
            else:
                logging.warning(
                    "Stored OAuth scope for %s does not include gmail.readonly. stored_scope=%s stored_scopes=%s tokeninfo_scope=%s",
                    account["email"],
                    account.get("scope", ""),
                    account.get("scopes", ""),
                    token_info.get("scope", ""),
                )
                await db["gmail_accounts"].update_one(
                    {"_id": account["account_id"]},
                    {
                        "$set": {
                            "last_error": (
                                "Gmail read permission is missing. Reconnect this Gmail "
                                "account and approve Gmail read/send access."
                            ),
                            "last_error_at": datetime.now(timezone.utc),
                        },
                    },
                )
                return {
                    "email": account["email"],
                    "status": "failed",
                    "reason": "insufficient_permissions",
                    "message": (
                        f"Insufficient permissions: 'gmail.readonly' not in token scopes for {account['email']}. "
                        "Reconnect this Gmail account and approve Gmail read/send access."
                    ),
                }
    except Exception as e:
        logging.warning(f"Token scope check failed: {e}")

    try:
        service = build("gmail", "v1", credentials=creds)
        messages = []
        seen_gmail_ids = set()
        latest_history_id = None
        sync_mode = "full" if force_full_sync else "history"

        def add_message_ref(message_ref):
            gmail_id = message_ref.get("id")
            if gmail_id and gmail_id not in seen_gmail_ids:
                messages.append(message_ref)
                seen_gmail_ids.add(gmail_id)

        if force_full_sync:
            page_token = None
            while True:
                request = service.users().messages().list(
                    userId="me",
                    maxResults=100,
                    pageToken=page_token,
                )
                result = request.execute()
                for message_ref in result.get("messages", []):
                    add_message_ref(message_ref)
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        else:
            last_history_id = account.get("history_id")
            if not last_history_id:
                profile = service.users().getProfile(userId="me").execute()
                latest_history_id = profile.get("historyId")
                if latest_history_id:
                    await db["gmail_accounts"].update_one(
                        {"_id": account["account_id"]},
                        {"$set": {"history_id": latest_history_id, "status": "connected"}},
                    )
                sync_mode = "baseline"

            else:
                page_token = None
                try:
                    while True:
                        result = service.users().history().list(
                            userId="me",
                            startHistoryId=str(last_history_id),
                            historyTypes=["messageAdded"],
                            pageToken=page_token,
                        ).execute()
                        latest_history_id = result.get("historyId") or latest_history_id
                        for record in result.get("history", []):
                            for added in record.get("messagesAdded", []):
                                add_message_ref(added.get("message", {}))
                        page_token = result.get("nextPageToken")
                        if not page_token:
                            break
                except HttpError as e:
                    status = getattr(getattr(e, "resp", None), "status", None)
                    if status in (400, 404):
                        profile = service.users().getProfile(userId="me").execute()
                        latest_history_id = profile.get("historyId")
                        if latest_history_id:
                            await db["gmail_accounts"].update_one(
                                {"_id": account["account_id"]},
                                {"$set": {"history_id": latest_history_id, "status": "connected"}},
                            )
                        sync_mode = "baseline_reset"
                    else:
                        raise

            if include_unread_backfill:
                page_token = None
                while True:
                    result = service.users().messages().list(
                        userId="me",
                        labelIds=["INBOX", "UNREAD"],
                        maxResults=100,
                        pageToken=page_token,
                    ).execute()
                    for message_ref in result.get("messages", []):
                        add_message_ref(message_ref)
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break

        stored_count = 0
        updated_count = 0

        for msg in messages:
            gmail_id = msg["id"]
            if await is_deleted_gmail_message(
                db,
                company_id=company_id,
                user_id=user_id,
                gmail_id=gmail_id,
            ):
                continue

            if not await claim_gmail_message(
                db,
                company_id=company_id,
                user_id=user_id,
                gmail_id=gmail_id,
            ):
                continue

            try:
                full_msg = service.users().messages().get(
                    userId="me", id=gmail_id, format="full"
                ).execute()
            except Exception:
                await release_gmail_message_claim(
                    db,
                    company_id=company_id,
                    user_id=user_id,
                    gmail_id=gmail_id,
                )
                raise
            thread_id = full_msg.get("threadId", gmail_id)
            payload = full_msg.get("payload", {})
            headers = payload.get("headers", [])

            subject = _gmail_header(headers, "Subject")
            sender = _gmail_header(headers, "From")
            to = _gmail_header(headers, "To")
            reply_to = _gmail_header(headers, "Reply-To")
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
            received_at = datetime.now(timezone.utc)
            list_updated_at = timestamp if force_full_sync else received_at

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
                    "reply_to": reply_to,
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

            message_context = {
                "gmail_account_id": account.get("account_id"),
                "inbox_email": account.get("email"),
                "order_matching_store_ids": account.get("store_ids"),
                "order_matching_store_shops": account.get("store_shops"),
            }
            if len(account.get("store_ids") or []) == 1:
                message_context["default_store_id"] = account["store_ids"][0]
                message_context["default_store_shop"] = (account.get("store_shops") or [""])[0]

            # Find existing thread (conversation) in 'messages' collection by thread_id
            existing_thread = await db["messages"].find_one({"user_id": ObjectId(user_id), "thread_id": thread_id, "channel": "email"})

            # Avoid duplicate insert of the same gmail_id in a thread
            if existing_thread:
                existing_messages = existing_thread.get("messages", [])
                duplicate_index = next(
                    (
                        index
                        for index, item in enumerate(existing_messages)
                        if item.get("metadata", {}).get("gmail_id") == gmail_id
                    ),
                    None,
                )
                if duplicate_index is not None:
                    if not update_existing:
                        continue
                    existing_messages[duplicate_index] = chat_entry.dict()
                    started_at, last_updated = _message_timestamp_bounds(existing_messages)
                    await db["messages"].update_one(
                        {"_id": existing_thread["_id"]},
                        {
                            "$set": {
                                f"messages.{duplicate_index}": chat_entry.dict(),
                                "started_at": started_at or existing_thread.get("started_at", timestamp),
                                "last_updated": last_updated or timestamp,
                                "title": existing_thread.get("title", subject or ""),
                                "status": "Open",
                                "archived": False,
                                "trashed": False,
                            }
                        },
                    )
                    updated_count += 1
                    continue
                next_messages = existing_messages + [chat_entry.dict()]
                started_at, last_updated = _message_timestamp_bounds(next_messages)
                await db["messages"].update_one(
                    {"_id": existing_thread["_id"]},
                        {
                            "$push": {"messages": chat_entry.dict()},
                            "$set": {
                                "started_at": started_at or existing_thread.get("started_at", timestamp),
                                "last_updated": list_updated_at,
                                "participants": list(set(existing_thread.get("participants", []) + [sender, to])),
                                "status": "Open",
                                "archived": False,
                                "trashed": False,
                                "read_by": existing_thread.get("read_by", []),
                                **{k: v for k, v in message_context.items() if v},
                            }
                        }
                )
            else:
                ticket_eligible = await should_generate_ticket_number(
                    db, company_id, subject, content
                )
                ticket_number = await next_ticket_number(db, company_id) if ticket_eligible else ""

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
                    "read_by": [],
                    "last_updated": list_updated_at,
                    "started_at": timestamp,
                    "ai_summary": None,
                    "tags": [],
                    "resolved_by_ai": False,
                    "ticket_generation_policy": TICKET_GENERATION_POLICY,
                    "ticket_generation_eligible": ticket_eligible,
                }
                if ticket_number:
                    message_doc["ticket"] = ticket_number
                message_doc.update({k: v for k, v in message_context.items() if v})
                await db["messages"].insert_one(message_doc)
                # Trigger AI analysis in background for new email messages
                asyncio.create_task(_auto_analyze_message(db, message_doc["_id"]))
            stored_count += 1

        try:
            if not latest_history_id:
                profile = service.users().getProfile(userId="me").execute()
                latest_history_id = profile.get("historyId")
            history_id = latest_history_id
            if history_id:
                await db["gmail_accounts"].update_one(
                    {"_id": account["account_id"]},
                    {"$set": {"history_id": history_id, "status": "connected"}},
                )
        except Exception as e:
            logging.warning(f"Could not update Gmail history_id after sync for {account['email']}: {e}")

        return {
            "email": account["email"],
            "status": "ok",
            "stored_count": stored_count,
            "updated_count": updated_count,
            "fetched_count": len(messages),
            "sync_mode": f"{sync_mode}+unread" if include_unread_backfill and not force_full_sync else sync_mode,
            "message": f"Fetched {len(messages)} messages, stored {stored_count} new messages, and updated {updated_count} existing messages for {account['email']}",
        }

    except Exception as e:
        logging.exception(f"Error fetching emails for {account['email']}: {str(e)}")
        return {
            "email": account["email"],
            "status": "failed",
            "reason": "fetch_failed",
            "message": f"Failed to fetch emails for {account['email']} due to an error.",
        }
    
async def fetch_all_gmail_accounts(db, user_id: str, company_id: str):
    cursor = db["gmail_accounts"].find({"user_id": ObjectId(user_id)})
    results = []
    async for cred in cursor:
        try:
            token_data = {
                "account_id": cred["_id"],
                "email": cred["email"],  # <-- include email here
                "access_token": cred["access_token"],
                "refresh_token": cred["refresh_token"],
                "client_id": cred["client_id"],
                "client_secret": cred["client_secret"],
                "expires_at": cred.get("expires_at"),
                "history_id": cred.get("history_id"),
                "scope": cred.get("scope"),
                "scopes": cred.get("scopes"),
                "store_ids": cred.get("store_ids") or ([cred.get("store_id")] if cred.get("store_id") else []),
            }
            if token_data["store_ids"]:
                stores = await db["shopify_cred"].find({
                    "_id": {"$in": token_data["store_ids"]},
                    "company_id": cred.get("company_id"),
                    "status": {"$ne": "disconnected"},
                }).to_list(length=100)
                by_id = {store["_id"]: store for store in stores}
                valid_store_ids = [store_id for store_id in token_data["store_ids"] if store_id in by_id]
                token_data["store_ids"] = valid_store_ids
                token_data["store_shops"] = [by_id[store_id].get("shop") for store_id in valid_store_ids]

            account_company_id = str(cred.get("company_id") or company_id)
            result = await fetch_and_save_gmail(
                token_data,
                db,
                user_id,
                account_company_id,
                include_unread_backfill=True,
            )
            results.append(result)
        except Exception as e:
            results.append({
                "email": cred.get("email", "unknown Gmail account"),
                "status": "failed",
                "reason": "unexpected_error",
                "message": f"Error: {str(e)}",
            })

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
        logging.info("Credentials refreshed successfully for %s", user_credentials.get("email", "unknown Gmail account"))
    except RefreshError:
        logging.exception("Error refreshing Gmail credentials for %s", user_credentials.get("email", "unknown Gmail account"))
        raise
    except Exception:
        logging.exception("Error refreshing Gmail credentials for %s", user_credentials.get("email", "unknown Gmail account"))
        raise

    service = build('gmail', 'v1', credentials=creds)
    return service


async def _auto_analyze_message(db, message_id):
    """Background task: auto-analyze a new email message with Gemini and save order_info."""
    import logging
    _logger = logging.getLogger("attentify.gmail.auto_analyze")
    try:
        from app.services.ai_service import analyze_emails_with_ai
        from app.api.v1.message import cacheable_order_info, clean_json_response

        doc = await db["messages"].find_one({"_id": message_id})
        if not doc or doc.get("order_info"):
            return  # Already analyzed or message deleted

        result = await analyze_emails_with_ai(doc)
        if isinstance(result, dict) and result.get("error"):
            error_code = result.get("error", "UNKNOWN")
            error_reason = result.get("reason", "")
            error_model = result.get("model", "")
            _logger.warning(
                "[AUTO-ANALYZE FAIL] msg=%s code=%s reason=%s model=%s",
                str(message_id), error_code, error_reason[:200], error_model
            )
            return

        response = getattr(result, 'content', str(result))
        order_info = clean_json_response(response)

        await db["messages"].update_one(
            {"_id": message_id},
            {"$set": {"order_info": cacheable_order_info(order_info)}}
        )
        _logger.info("Auto-analyze saved for %s: order_id=%s", str(message_id), order_info.get("order_id", ""))
    except Exception as e:
        _logger.warning("Auto-analyze error for %s: %s", str(message_id), str(e)[:300])
