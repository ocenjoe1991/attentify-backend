# app/routes/message.py

from fastapi import APIRouter, HTTPException, Depends, Body, Query, Request, Response
import os
import httpx
import hashlib
from app.services.gmail_service import fetch_all_gmail_accounts, get_gmail_service
from app.services.deleted_gmail_service import record_deleted_gmail_messages
from app.services.gmail_attachment_service import (
    decode_gmail_attachment_data,
    extract_gmail_attachments,
)
from app.db.mongodb import get_database
from app.models.message import Message, ChatEntry 
from typing import List
import re
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from app.services.ai_service import analyze_emails_with_ai
from app.services.shopify_service import (
    _to_datetime,
    fetch_order_from_shop,
    fetch_order_updated_at_from_shop,
    upsert_orders,
)
import json
from bson import ObjectId
import base64
from email.utils import formatdate, format_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from html import unescape
from html.parser import HTMLParser
import mimetypes
from urllib.parse import quote
from datetime import datetime, timezone
from email.utils import parseaddr
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from app.core.security import get_current_user
from app.utils.logger import logger
from app.core.permissions import (
    OWNER_ROLES,
    PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL,
    can_permanently_delete_ticket,
    has_owner_approval_bypass,
)
from app.core.audit import record_audit_log
from app.utils.datetime_utils import to_utc_iso
from app.main import sio
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from math import ceil

router = APIRouter()

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")

TICKET_STATUSES = {
    "Open",
    "In Progress",
    "Pending",
    "Resolved",
    "Escalated",
    "Awaiting Approval",
    "Canceled",
}


def _gmail_header(headers: list[dict], name: str) -> str:
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()),
        "",
    )


def _normalize_rfc_message_id(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("<") and value.endswith(">"):
        return value
    if "@" not in value:
        return ""
    return f"<{value}>"


def _build_references(existing_references: str | None, message_id: str) -> str:
    parts = (existing_references or "").strip()
    if message_id and message_id not in parts:
        parts = f"{parts} {message_id}".strip()
    return parts


def _reply_recipient(client_message: dict, fallback_client: str | None) -> str:
    metadata = client_message.get("metadata") or {}
    candidates = [
        metadata.get("reply_to"),
        metadata.get("from"),
        fallback_client,
    ]
    for candidate in candidates:
        _, email_addr = parseaddr(candidate or "")
        if email_addr:
            return email_addr
    return fallback_client or ""


def apply_current_user_read_state(doc: dict, user_id: ObjectId) -> None:
    if "read_by" not in doc:
        # Tickets created before read tracking was added remain read on rollout.
        doc["is_read_by_current_user"] = True
    else:
        doc["is_read_by_current_user"] = not unviewed_customer_entries(doc, user_id)
    doc.pop("read_by", None)


class _EmailTextExtractor(HTMLParser):
    _ignored_tags = {"head", "style", "script", "noscript", "template", "svg"}
    _block_tags = {"address", "article", "br", "div", "footer", "header", "li", "p", "section", "table", "td", "th", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._block_tags:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if not self.ignored_depth and tag.lower() in self._block_tags:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._block_tags:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _html_to_plain_text(html: str) -> str:
    extractor = _EmailTextExtractor()
    try:
        extractor.feed(unescape(html or ""))
        extractor.close()
        text = "".join(extractor.parts)
    except Exception:
        text = re.sub(r"(?is)<(style|script|head|noscript|template|svg)\b.*?</\1>", " ", html or "")
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _message_entry_timestamp(entry: dict) -> datetime:
    value = entry.get("timestamp")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def customer_message_entries(doc: dict) -> list[dict]:
    client_email = parseaddr(doc.get("client") or "")[1].lower()
    entries = doc.get("messages") or []
    if not client_email:
        return sorted(entries, key=_message_entry_timestamp)

    customer_entries = []
    for entry in entries:
        sender = (entry.get("metadata") or {}).get("from") or entry.get("sender") or ""
        if parseaddr(sender)[1].lower() == client_email:
            customer_entries.append(entry)
    return sorted(customer_entries, key=_message_entry_timestamp)


def current_user_read_entry(doc: dict, user_id: ObjectId) -> dict | None:
    return next(
        (
            entry
            for entry in doc.get("read_by") or []
            if isinstance(entry, dict) and str(entry.get("user_id")) == str(user_id)
        ),
        None,
    )


def unviewed_customer_entries(doc: dict, user_id: ObjectId) -> list[dict]:
    entries = customer_message_entries(doc)
    if not entries or "read_by" not in doc:
        return []

    read_entry = current_user_read_entry(doc, user_id)
    if not read_entry:
        return entries

    last_viewed_id = read_entry.get("last_viewed_gmail_id")
    # Legacy read records did not have an individual email cursor.
    if not last_viewed_id:
        return []

    for index, entry in enumerate(entries):
        gmail_id = (entry.get("metadata") or {}).get("gmail_id")
        if gmail_id == last_viewed_id:
            return entries[index + 1:]
    return []


def _preview_content(content: str, message_type: str | None) -> str:
    if message_type == "html":
        # Gmail commonly puts quoted history in blockquotes. This affects only
        # the Inbox snippet; the original message remains unchanged for detail view.
        content = re.sub(r"(?is)<blockquote\b.*?</blockquote\s*>", " ", content or "")
        content = _html_to_plain_text(content)

    quote_boundary = re.search(
        r"(?is)\s+(?:On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b.{0,180}?\bwrote:|-----Original Message-----|From:\s+\S+)",
        content or "",
    )
    return content[:quote_boundary.start()] if quote_boundary else content


def message_preview(entry: dict | None, limit: int = 180) -> str:
    if not entry:
        return ""
    content = _preview_content(entry.get("content") or "", entry.get("message_type"))

    preview = re.sub(r"\s+", " ", content).strip()
    if len(preview) > limit:
        return f"{preview[:limit - 3].rstrip()}..."
    return preview


def latest_message_preview_entry_for_user(doc: dict, user_id: ObjectId) -> dict | None:
    unviewed_entries = unviewed_customer_entries(doc, user_id)
    if unviewed_entries:
        return unviewed_entries[0]
    entries = customer_message_entries(doc)
    return entries[-1] if entries else None


def latest_message_preview_details(doc: dict, user_id: ObjectId) -> dict:
    entry = latest_message_preview_entry_for_user(doc, user_id)
    metadata = (entry or {}).get("metadata") or {}
    return {
        "latest_message_preview": message_preview(entry),
        "latest_message_preview_from": metadata.get("from") or (entry or {}).get("sender") or "",
        "latest_message_preview_at": to_utc_iso((entry or {}).get("timestamp")),
    }


def latest_message_preview_for_user(doc: dict, user_id: ObjectId) -> str:
    return latest_message_preview_details(doc, user_id)["latest_message_preview"]


def _reply_body_part(html: str) -> MIMEMultipart:
    alternative = MIMEMultipart("alternative")
    plain_text = _html_to_plain_text(html)
    alternative.attach(MIMEText(plain_text or " ", "plain", "utf-8"))
    alternative.attach(MIMEText(html or "", "html", "utf-8"))
    return alternative


def _attachment_key(filename: str, size: int) -> str:
    return f"{filename.strip().lower()}:{int(size or 0)}"


def _sent_attachment_keys(message: dict) -> tuple[set[str], set[str]]:
    hashes: set[str] = set()
    keys: set[str] = set()
    for entry in message.get("messages", []):
        if entry.get("sender") == message.get("client"):
            continue
        for attachment in (entry.get("metadata") or {}).get("attachments") or []:
            if attachment.get("content_hash"):
                hashes.add(attachment["content_hash"])
            if attachment.get("filename"):
                keys.add(_attachment_key(attachment.get("filename", ""), attachment.get("size", 0)))
    return hashes, keys


def _dedupe_reply_attachments(message: dict, attachments: list[dict]) -> tuple[list[dict], list[dict]]:
    sent_hashes, sent_keys = _sent_attachment_keys(message)
    next_attachments = []
    skipped = []
    for attachment in attachments:
        content_hash = attachment.get("content_hash")
        key = _attachment_key(attachment.get("filename", ""), attachment.get("size", 0))
        if (content_hash and content_hash in sent_hashes) or key in sent_keys:
            skipped.append(attachment)
            continue
        next_attachments.append(attachment)
    return next_attachments, skipped

LEGACY_STATUS_MAP = {
    "Assigned": "Open",
    "Closed": "Resolved",
    "Cancelled": "Canceled",
    "open": "Open",
    "closed": "Resolved",
    "pending": "Pending",
}

ACTIVE_STATUSES = {
    "Open",
    "In Progress",
    "Pending",
    "Escalated",
    "Awaiting Approval",
}

ACTIVE_QUERY_STATUSES = ACTIVE_STATUSES | {"Assigned"}

ARCHIVED_STATUSES = {
    "Resolved",
    "Canceled",
}

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_SIZE = 20 * 1024 * 1024
BLOCKED_ATTACHMENT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".exe",
    ".js",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".vbs",
}


def sanitize_attachment_filename(filename: str | None) -> str:
    safe = os.path.basename(filename or "").strip()
    return safe or "attachment"


def validate_attachment(filename: str, content: bytes) -> None:
    extension = os.path.splitext(filename)[1].lower()
    if extension in BLOCKED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Attachment type is not allowed: {filename}")
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(status_code=400, detail=f"Attachment is larger than 10MB: {filename}")


def attachment_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"

def normalize_doc_dates(doc: dict) -> dict:
    if "started_at" in doc:
        doc["started_at"] = to_utc_iso(doc.get("started_at"))
    if "last_updated" in doc:
        doc["last_updated"] = to_utc_iso(doc.get("last_updated"))
    if "created_at" in doc:
        doc["created_at"] = to_utc_iso(doc.get("created_at"))
    if "messages" in doc and isinstance(doc["messages"], list):
        for item in doc["messages"]:
            if isinstance(item, dict) and "timestamp" in item:
                item["timestamp"] = to_utc_iso(item.get("timestamp"))
    return doc


def serialize_for_json(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return to_utc_iso(value)
    if isinstance(value, list):
        return [serialize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_for_json(item) for key, item in value.items()}
    return value


def normalize_status(status: str) -> str:
    return LEGACY_STATUS_MAP.get(status, status)

@router.post("/fetch-all")
async def fetch_all(body: dict, db=Depends(get_database), current_user: dict = Depends(get_current_user)):
    company_id = body.get("company_id", "")
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")
    
    result = await fetch_all_gmail_accounts(db, user_id=str(current_user["_id"]), company_id= company_id)
    failures = [item for item in result if item.get("status") == "failed"]
    if failures:
        raise HTTPException(status_code=424, detail=failures)
    return {"result": result}

def extract_name(email_str: str) -> str:
    match = re.match(r"^(.*?)\s*<", email_str)
    return match.group(1).strip() if match else email_str

async def get_user_display_name(user: dict) -> str:
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return name or user.get("email", "Unknown user")

async def record_ticket_audit_log(
    db: AsyncIOMotorDatabase,
    message: dict,
    current_user: dict,
    membership: dict,
    action: str,
    details: dict | None = None,
) -> None:
    ticket = message.get("ticket") or str(message.get("_id"))
    await record_audit_log(
        db,
        company_id=message["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action=action,
        entity_type="ticket",
        entity_id=message["_id"],
        ticket=ticket,
        customer=message.get("client", ""),
        details=details,
    )

async def _batch_get_users(db, user_ids: list) -> dict:
    """Fetch multiple users in a single query and return a lookup dict keyed by ObjectId string."""
    if not user_ids:
        return {}
    # Normalize all IDs to ObjectId
    oids = []
    for uid in user_ids:
        try:
            oids.append(uid if isinstance(uid, ObjectId) else ObjectId(uid))
        except Exception:
            continue
    if not oids:
        return {}
    users = {}
    cursor = db["users"].find({"_id": {"$in": oids}})
    async for u in cursor:
        users[str(u["_id"])] = {
            "id": str(u["_id"]),
            "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
            "email": u.get("email", ""),
        }
    return users


@router.get("/", response_model=List[dict])
async def get_messages(db=Depends(get_database), current_user: dict = Depends(get_current_user)):
    cursor = db["messages"].find({"user_id": current_user["_id"]}).sort("last_updated", DESCENDING)
    messages = []
    # Collect assigned member IDs for batch query
    assigned_ids = set()
    async for doc in cursor:
        messages.append(doc)
        aid = doc.get("assigned_member_id")
        if aid:
            assigned_ids.add(str(aid))

    # Batch fetch all assigned users
    user_map = await _batch_get_users(db, list(assigned_ids))

    result = []
    for doc in messages:
        apply_current_user_read_state(doc, current_user["_id"])
        doc["_id"] = str(doc["_id"])
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])
        doc["client"] = extract_name(doc.get("client", ""))
        normalize_doc_dates(doc)

        # Map assigned member from batch lookup
        aid = doc.get("assigned_member_id")
        doc["assigned_to"] = user_map.get(str(aid)) if aid else None
        doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        result.append(doc)

    return result

@router.get("/company_messages", response_model=dict)
async def get_company_messages(
    company_id: str = Query(..., description="ID of the company"),
    search: str = Query("", description="Search by client, title, or ticket"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Page size"),
    view_mode: str = Query("inbox", description="inbox, archived, or trashed"),
    assigned_filter: str = Query("all", description="all, assigned, or unassigned"),
    status_filter: str = Query("all", description="Message status or all"),
    order_filter: str = Query("all", description="all, order, other, or needs_review"),
    store_id: str = Query("", description="Default Shopify store ID or unassigned"),
    sort_by: str = Query("started_at", description="title, ticket, started_at, created_at or last_updated"),
    sort_order: str = Query("desc", description="asc or desc"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user)
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")

    # Verify membership
    membership = await db["memberships"].find_one(
        {"user_id": current_user["_id"], "company_id": ObjectId(company_id)}
    )
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")

    role = membership.get("role")

    # Base query depending on role
    query = {"company_id": ObjectId(company_id)}
    if role == "agent":
        # Agents may access only tickets explicitly assigned to themselves.
        query["assigned_member_id"] = current_user["_id"]
    elif role not in ["company_owner", "store_owner", "agent", "readonly"]:
        query["user_id"] = current_user["_id"]

    if view_mode == "inbox":
        query["trashed"] = {"$ne": True}
        query["archived"] = {"$ne": True}
        query["status"] = {"$in": list(ACTIVE_QUERY_STATUSES)}
    elif view_mode == "archived":
        query["trashed"] = {"$ne": True}
        query["$and"] = query.get("$and", [])
        query["$and"].append({
            "$or": [
                {"archived": True},
                {"status": {"$in": list(ARCHIVED_STATUSES | {"Cancelled", "Closed"})}},
            ]
        })
    elif view_mode == "trashed":
        query["trashed"] = True
    else:
        raise HTTPException(status_code=400, detail="Invalid view mode")

    # Apply search filter (case-insensitive)
    if search.strip():
        search_regex = {"$regex": search.strip(), "$options": "i"}
        search_or = [
            {"title": search_regex},
            {"client": search_regex},
            {"ticket": search_regex},
        ]
        if "$or" in query:
            existing_or = query.pop("$or")
            query["$and"] = query.get("$and", [])
            query["$and"].append({"$or": existing_or})
            query["$and"].append({"$or": search_or})
        else:
            query["$or"] = search_or

    if assigned_filter == "assigned":
        if role != "agent":
            query["assigned_member_id"] = {"$exists": True, "$ne": None}
    elif assigned_filter == "unassigned":
        query["$and"] = query.get("$and", [])
        query["$and"].append({
            "$or": [
                {"assigned_member_id": {"$exists": False}},
                {"assigned_member_id": None},
                {"assigned_member_id": ""},
            ]
        })

    if order_filter == "order":
        query["order_match_status"] = "matched"
    elif order_filter == "other":
        query["order_match_status"] = {"$in": ["unmatched", "not_order"]}
    elif order_filter == "needs_review":
        query["$and"] = query.get("$and", [])
        query["$and"].append({
            "$or": [
                {"order_match_status": {"$exists": False}},
                {"order_match_status": {"$in": ["unknown", "possible"]}},
            ]
        })
    elif order_filter != "all":
        raise HTTPException(status_code=400, detail="Invalid order filter")

    if store_id:
        if store_id == "unassigned":
            query["$and"] = query.get("$and", [])
            query["$and"].append({
                "$or": [
                    {"default_store_id": {"$exists": False}},
                    {"default_store_id": None},
                    {"default_store_id": ""},
                ]
            })
        elif ObjectId.is_valid(store_id):
            query["default_store_id"] = ObjectId(store_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid store filter")

    if status_filter != "all":
        status_filter = normalize_status(status_filter)
        if status_filter not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status filter")
        if view_mode == "inbox" and status_filter not in ACTIVE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status for inbox")
        if view_mode == "archived" and status_filter not in ARCHIVED_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status for archive")
        query["status"] = status_filter

    # Support 'created_at' as alias for 'started_at' (messages use started_at as ticket date)
    sort_fields = {
        "title": "title",
        "ticket": "ticket",
        "started_at": "started_at",
        "created_at": "started_at",
        "last_updated": "last_updated",
    }
    if sort_by not in sort_fields:
        raise HTTPException(status_code=400, detail="Invalid sort field")
    sort_field = sort_fields[sort_by]
    sort_direction = ASCENDING if sort_order == "asc" else DESCENDING

    # Count total documents for pagination
    total_count = await db["messages"].count_documents(query)
    totalPages = ceil(total_count / size)

    # Pagination
    skip = (page - 1) * size

    sort_value = (
        {"$toLower": {"$ifNull": [f"${sort_field}", ""]}}
        if sort_field in {"title", "ticket"}
        else {
            "$ifNull": [
                f"${sort_field}",
                {"$ifNull": ["$last_updated", "$started_at"]},
            ]
        }
    )

    pipeline = [
        {"$match": query},
        {"$addFields": {"_sort_value": sort_value}},
        {"$sort": {"_sort_value": sort_direction, "_id": sort_direction}},
        {"$skip": skip},
        {"$limit": size},
    ]

    messages = []
    assigned_ids = set()
    async for doc in db["messages"].aggregate(pipeline):
        # Build the per-user preview before the response serializer removes read_by.
        doc.update(latest_message_preview_details(doc, current_user["_id"]))
        apply_current_user_read_state(doc, current_user["_id"])
        doc["_id"] = str(doc["_id"])
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])
        if doc.get("default_store_id"):
            doc["default_store_id"] = str(doc["default_store_id"])
        if doc.get("order_matching_store_ids"):
            doc["order_matching_store_ids"] = [str(store_id) for store_id in doc["order_matching_store_ids"]]
        if doc.get("gmail_account_id"):
            doc["gmail_account_id"] = str(doc["gmail_account_id"])
        doc["status"] = normalize_status(doc.get("status", "Open"))
        doc["order_match_status"] = doc.get("order_match_status", "unknown")
        doc.pop("_sort_date", None)
        doc["client"] = extract_name(doc.get("client", ""))

        # Collect assigned_member_id BEFORE popping
        aid = doc.get("assigned_member_id")
        if aid:
            assigned_ids.add(str(aid))
            doc["_assigned_member_id"] = str(aid)  # temp field for later lookup

        first_attachment = None
        for entry in doc.get("messages", []) or []:
            for attachment in (entry.get("metadata") or {}).get("attachments") or []:
                if attachment.get("gmail_message_id") and attachment.get("attachment_id"):
                    first_attachment = {
                        "filename": attachment.get("filename"),
                        "mime_type": attachment.get("mime_type"),
                        "size": attachment.get("size"),
                        "gmail_message_id": attachment.get("gmail_message_id"),
                        "attachment_id": attachment.get("attachment_id"),
                    }
                    break
            if first_attachment:
                break
        doc["has_attachments"] = bool(first_attachment)
        if first_attachment:
            doc["first_attachment"] = first_attachment

        doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        doc.pop("comments", None)
        normalize_doc_dates(doc)
        messages.append(doc)

    # Batch fetch all assigned users
    user_map = await _batch_get_users(db, list(assigned_ids))

    for doc in messages:
        doc["assigned_to"] = user_map.get(doc.pop("_assigned_member_id", "")) or None

    return {
        "messages": messages,
        "totalPages": totalPages
    }

@router.get("/{id}", response_model=dict)
async def get_message(
    id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    doc = await ensure_message_access(id, db, current_user, action="read")
    apply_current_user_read_state(doc, current_user["_id"])

    # Convert ObjectIds to strings
    doc["_id"] = str(doc["_id"])
    doc["user_id"] = str(doc["user_id"])
    doc["company_id"] = str(doc["company_id"])
    if doc.get("default_store_id"):
        doc["default_store_id"] = str(doc["default_store_id"])
    if doc.get("order_matching_store_ids"):
        doc["order_matching_store_ids"] = [str(store_id) for store_id in doc["order_matching_store_ids"]]
    if doc.get("gmail_account_id"):
        doc["gmail_account_id"] = str(doc["gmail_account_id"])
    if "assigned_member_id" in doc and doc["assigned_member_id"]:
        doc["assigned_member_id"] = str(doc["assigned_member_id"])
    doc["status"] = normalize_status(doc.get("status", "Open"))
    normalize_doc_dates(doc)

    # Properly await comment serialization
    comments = []
    for c in doc.get("comments", []):
        comments.append(await serialize_comment(c, db))
    doc["comments"] = comments

    return doc


@router.post("/{id}/read", response_model=dict)
async def mark_message_read(
    id: str,
    payload: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    doc = await ensure_message_access(id, db, current_user, action="read")
    gmail_message_id = str(payload.get("gmail_message_id") or "").strip()
    if not gmail_message_id:
        raise HTTPException(status_code=400, detail="gmail_message_id is required")

    customer_entries = customer_message_entries(doc)
    target_index = next(
        (
            index
            for index, entry in enumerate(customer_entries)
            if (entry.get("metadata") or {}).get("gmail_id") == gmail_message_id
        ),
        None,
    )
    if target_index is None:
        raise HTTPException(status_code=400, detail="Message is not a customer email in this ticket")

    existing_read_entry = current_user_read_entry(doc, current_user["_id"])
    existing_index = -1
    if existing_read_entry and existing_read_entry.get("last_viewed_gmail_id"):
        existing_index = next(
            (
                index
                for index, entry in enumerate(customer_entries)
                if (entry.get("metadata") or {}).get("gmail_id") == existing_read_entry["last_viewed_gmail_id"]
            ),
            -1,
        )

    read_at = datetime.now(timezone.utc)
    if target_index > existing_index:
        read_state = {
            "user_id": current_user["_id"],
            "last_viewed_gmail_id": gmail_message_id,
            "read_at": read_at,
        }
        if existing_read_entry:
            await db["messages"].update_one(
                {"_id": doc["_id"], "read_by.user_id": current_user["_id"]},
                {"$set": {"read_by.$": read_state}},
            )
        else:
            await db["messages"].update_one(
                {"_id": doc["_id"], "read_by.user_id": {"$ne": current_user["_id"]}},
                {"$push": {"read_by": read_state}},
            )
        doc["read_by"] = [
            entry
            for entry in doc.get("read_by") or []
            if not (isinstance(entry, dict) and str(entry.get("user_id")) == str(current_user["_id"]))
        ] + [read_state]

    return {
        "id": str(doc["_id"]),
        "is_read_by_current_user": not unviewed_customer_entries(doc, current_user["_id"]),
        **latest_message_preview_details(doc, current_user["_id"]),
        "read_at": to_utc_iso(read_at),
    }


@router.get("/{id}/order-precheck", response_model=dict)
async def precheck_message_orders(
    id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    message_doc = await ensure_message_access(id, db, current_user, action="read")
    client_str = message_doc.get("client") or ""
    email_match = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', client_str)
    client_email = email_match[0] if email_match else ""
    if not client_email or mentions_order_number(message_doc):
        return {"checked": False, "no_orders": False}

    if message_requires_store_scope(message_doc):
        order_info = await mark_message_needs_store_scope(db, message_doc)
        return {
            "checked": True,
            "no_orders": False,
            "store_required": True,
            "order_info": serialize_for_json(order_info),
        }

    order_count = await db["orders"].count_documents(scoped_order_query(message_doc, {
        "company_id": message_doc["company_id"],
        "customer.email": {"$regex": f"^{re.escape(client_email)}$", "$options": "i"},
    }))
    if order_count > 0:
        return {"checked": True, "no_orders": False, "order_count": order_count}

    order_info = {
        "order_id": "",
        "type": "",
        "status": 0,
        "msg": "No orders for this customer",
        "no_orders": True,
    }
    await db["messages"].update_one(
        {"_id": message_doc["_id"]},
        {"$set": {
            "order_info": cacheable_order_info(order_info, source="no_orders"),
            "order_match_status": "not_order",
        }},
    )
    await update_message_analysis_state(db, message_doc["_id"], state="success", source="no_orders")
    return {"checked": True, "no_orders": True, "order_info": serialize_for_json(order_info)}

@router.put("/{id}", response_model=dict)
async def update_message(
    id: str,
    payload: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    message = await ensure_message_access(id, db, current_user, action="update")
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    safe_payload = {k: v for k, v in payload.items() if k != "_id"}
    if safe_payload.get("order_info.confirmed") is True:
        order_id = safe_payload.get("order_info.order_id", "")
        order_info = dict(message.get("order_info") or {})
        if order_id:
            order_info["order_id"] = order_id
        order_info["confirmed"] = True
        order_name = str(order_info.get("order_id", ""))
        order_name = order_name if order_name.startswith("#") else f"#{order_name}"
        db_order, _ = await find_order_for_message(db, message, order_name)
        if db_order:
            order_info["shopify_order"] = await build_order_snapshot(db, db_order)
            safe_payload["matched_order_id"] = str(db_order.get("order_id", ""))
            safe_payload["matched_order_name"] = db_order.get("name", order_info.get("order_id", ""))
            safe_payload.update(await matched_store_fields(db, message, db_order))

        safe_payload.pop("order_info.order_id", None)
        safe_payload.pop("order_info.confirmed", None)
        safe_payload["order_info"] = cacheable_order_info(
            order_info,
            source="confirmed",
            keep_shopify_order=True,
        )
        safe_payload["order_match_status"] = "matched"
    if "status" in safe_payload:
        safe_payload["status"] = normalize_status(safe_payload["status"])
        if safe_payload["status"] not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        if (
            safe_payload["status"] == "Resolved"
            and membership.get("role") not in OWNER_ROLES
            and not has_owner_approval_bypass(membership, PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL)
        ):
            safe_payload["status"] = "Awaiting Approval"
    safe_payload["last_updated"] = datetime.now(timezone.utc)
    updated_message = await db["messages"].find_one_and_update(
        {"_id": ObjectId(id)},
        {"$set": safe_payload},
        return_document=ReturnDocument.AFTER,
    )
    return {
        "message": "Message updated",
        "order_info": serialize_for_json((updated_message or {}).get("order_info")),
    }

async def ensure_message_access(
    message_id: str,
    db: AsyncIOMotorDatabase,
    current_user: dict,
    action: str = "read",
) -> dict:
    if not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    message = await db["messages"].find_one({"_id": ObjectId(message_id)})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")

    role = membership.get("role")
    if action != "read" and role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only users cannot modify messages")
    if role == "agent":
        assigned = message.get("assigned_member_id")
        if not assigned or str(assigned) != str(current_user["_id"]):
            raise HTTPException(status_code=403, detail="Message is not assigned to this agent")
    if role not in ["company_owner", "store_owner", "agent", "readonly"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    return message

@router.delete("/{message_id}", response_model=dict)
async def delete_message(
    message_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    message = await ensure_message_access(message_id, db, current_user)
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    role = membership.get("role") if membership else None
    if not can_permanently_delete_ticket(membership):
        raise HTTPException(status_code=403, detail="Permanent delete is not enabled for this account")

    if not message.get("trashed"):
        raise HTTPException(status_code=400, detail="Only trashed messages can be permanently deleted")

    deleted_gmail_count = await record_deleted_gmail_messages(db, message, current_user)

    result = await db["messages"].delete_one({"_id": message["_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    await record_ticket_audit_log(
        db,
        message,
        current_user,
        membership,
        "Permanently deleted ticket",
        {"deleted_gmail_count": deleted_gmail_count},
    )

    return {"message": "Message permanently deleted"}

async def serialize_comment(comment: dict, db) -> dict:
    user = await db["users"].find_one({"_id": comment["user_id"]})
    return {
        "id": str(comment["_id"]),
        "user_id": str(comment["user_id"]),  # raw user reference
        "user": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() if user else None,
        "content": comment["content"],
        "status": comment.get("status"),
        "edited": comment.get("edited"),
        "created_at": to_utc_iso(comment.get("created_at")),
        "updated_at": to_utc_iso(comment.get("updated_at")),
    }

@router.post("/add_comment/{message_id}", response_model=dict)
async def add_comment(
    message_id: str,
    payload: dict = Body(...),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    message = await ensure_message_access(message_id, db, user, action="update")

    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Comment content is required")
    status = payload.get("status", "Pending")
    membership = await db["memberships"].find_one({
        "user_id": user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    can_resolve_without_owner = has_owner_approval_bypass(
        membership,
        PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL,
    )
    if status == "Resolved" and not can_resolve_without_owner:
        status = "Awaiting Approval"
    elif status == "Awaiting Approval" and can_resolve_without_owner:
        status = "Resolved"
    if status not in {"Pending", "Resolved", "Awaiting Approval"}:
        raise HTTPException(status_code=400, detail="Invalid comment status")

    # Build new comment object
    new_comment = {
        "_id": ObjectId(),  # unique ID for comment
        "user_id": ObjectId(user["_id"]),
        "content": content,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "status": status
    }

    # Push comment into the message's comments array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$push": {"comments": new_comment}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"message": "Comment added", "comment": await serialize_comment(new_comment, db)}

@router.put("/edit_comment/{message_id}/{comment_id}", response_model=dict)
async def edit_comment(
    message_id: str,
    comment_id: str,
    content: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")
    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Comment content is required")
    message = await ensure_message_access(message_id, db, user, action="update")
    membership = await db["memberships"].find_one({
        "user_id": user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    role = membership.get("role") if membership else None
    existing_comment = next(
        (c for c in message.get("comments", []) if c.get("_id") == ObjectId(comment_id)),
        None,
    )
    if not existing_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if existing_comment.get("user_id") != user["_id"] and role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only the author or an owner can edit this comment")
    
    # Find and update comment inside array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id), "comments._id": ObjectId(comment_id)},
        {
            "$set": {
                "comments.$.content": content,
                "comments.$.edited": True,
                "comments.$.updated_at": datetime.now(timezone.utc)
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    # Get updated comment
    message = await db["messages"].find_one({"_id": ObjectId(message_id)})
    updated_comment = next((c for c in message["comments"] if c["_id"] == ObjectId(comment_id)), None)
    await record_ticket_audit_log(
        db,
        message,
        user,
        membership,
        "Edited comment",
        {"comment_id": comment_id},
    )

    return {"message": "Comment updated", "comment": await serialize_comment(updated_comment, db)}

@router.put("/approve_comment/{message_id}/{comment_id}", response_model=dict)
async def approve_comment(
    message_id: str,
    comment_id: str,
    status: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")
    message = await ensure_message_access(message_id, db, user, action="update")
    membership = await db["memberships"].find_one({
        "user_id": user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    if not membership or membership.get("role") not in OWNER_ROLES:
        if not has_owner_approval_bypass(membership, PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL):
            raise HTTPException(status_code=403, detail="Only owners or permitted users can approve resolution comments")
    if status not in {"Pending", "Resolved", "Awaiting Approval"}:
        raise HTTPException(status_code=400, detail="Invalid comment status")
    
    # Find and update comment inside array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id), "comments._id": ObjectId(comment_id)},
        {
            "$set": {
                "comments.$.status": status,
                "comments.$.updated_at": datetime.now(timezone.utc)
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    # Get updated comment
    message = await db["messages"].find_one({"_id": ObjectId(message_id)})
    updated_comment = next((c for c in message["comments"] if c["_id"] == ObjectId(comment_id)), None)

    return {"message": "Comment approved", "comment": await serialize_comment(updated_comment, db)}

# --- Delete Comment ---
@router.delete("/delete_comment/{message_id}/{comment_id}", response_model=dict)
async def delete_comment(
    message_id: str,
    comment_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")
    message = await ensure_message_access(message_id, db, user, action="update")
    membership = await db["memberships"].find_one({
        "user_id": user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    role = membership.get("role") if membership else None
    existing_comment = next(
        (c for c in message.get("comments", []) if c.get("_id") == ObjectId(comment_id)),
        None,
    )
    if not existing_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if existing_comment.get("user_id") != user["_id"] and role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only the author or an owner can delete this comment")

    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$pull": {"comments": {"_id": ObjectId(comment_id)}}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    await record_ticket_audit_log(
        db,
        message,
        user,
        membership,
        "Deleted comment",
        {"comment_id": comment_id},
    )

    return {"message": "Comment deleted"}

@router.patch("/{message_id}")
async def update_message_field(
    message_id: str,
    request: Request,
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    message = await ensure_message_access(message_id, db, current_user, action="update")
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": message["company_id"],
        "status": "active",
    })
    role = membership.get("role") if membership else None
    field = body.get("field")
    value = body.get("value")

    if not field:
        raise HTTPException(status_code=400, detail="Field is required")

    # Optionally, prevent updates to _id or forbidden fields
    allowed_fields = {"assigned_member_id", "status", "trashed", "archived", "default_store_id"}
    if field not in allowed_fields:
        raise HTTPException(status_code=400, detail="Field cannot be updated here")
    if field == "assigned_member_id" and role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only owners can assign messages")
    if field == "archived" and role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only owners can archive messages")
    if field == "trashed" and role not in OWNER_ROLES and not can_permanently_delete_ticket(membership):
        raise HTTPException(status_code=403, detail="Delete is not enabled for this account")
    
    # Convert to ObjectId where needed
    if field == "assigned_member_id" and value:
        try:
            value = ObjectId(value)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid assigned_member_id")
        assigned_membership = await db["memberships"].find_one({
            "user_id": value,
            "company_id": message["company_id"],
            "role": "agent",
            "status": "active",
        })
        if not assigned_membership:
            raise HTTPException(status_code=400, detail="Assigned user must be an active agent in this company")
    if field == "status":
        value = normalize_status(value)
        if value not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        if (
            value == "Resolved"
            and role not in OWNER_ROLES
            and not has_owner_approval_bypass(membership, PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL)
        ):
            value = "Awaiting Approval"
    if field == "default_store_id":
        if not value:
            set_payload = {
                "$unset": {"default_store_id": "", "default_store_shop": "", "order_info": "", "order_match_status": ""},
                "$set": {"last_updated": datetime.now(timezone.utc)},
            }
            result = await db["messages"].update_one({"_id": ObjectId(message_id)}, set_payload)
            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Message not found")
            return {"message": "default_store_id updated", "field": field, "value": ""}
        try:
            value = ObjectId(value)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid default_store_id")
        store = await db["shopify_cred"].find_one({
            "_id": value,
            "company_id": message["company_id"],
            "status": {"$ne": "disconnected"},
        })
        if not store:
            raise HTTPException(status_code=404, detail="Shopify store not found")
        scope_ids = message.get("order_matching_store_ids") or []
        if scope_ids and value not in scope_ids:
            raise HTTPException(status_code=400, detail="Store is not in this message's matching scope")
        field = "default_store_id"

    # Capture request metadata for audit tracing
    client_ip = None
    try:
        # Prefer X-Forwarded-For (common behind proxies/load-balancers)
        if request:
            xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
            xri = request.headers.get("x-real-ip") or request.headers.get("X-Real-Ip")
            if xff:
                # X-Forwarded-For may contain a comma-separated list; take the first
                client_ip = xff.split(",")[0].strip()
            elif xri:
                client_ip = xri.strip()
            else:
                client_ip = request.client.host if request.client else None
    except Exception:
        client_ip = None
    user_agent = request.headers.get("user-agent", "") if request else ""

    # Perform update
    set_payload = {field: value, "last_updated": datetime.now(timezone.utc)}
    if field == "default_store_id":
        set_payload["default_store_shop"] = store.get("shop")
        set_payload.pop("order_match_status", None)
    update_doc = {"$set": set_payload}
    if field == "default_store_id":
        update_doc["$unset"] = {
            "order_info": "",
            "order_match_status": "",
            "matched_order_id": "",
            "matched_order_name": "",
        }
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        update_doc
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")
    if field == "trashed" and value is True and not message.get("trashed"):
        await record_ticket_audit_log(db, message, current_user, membership, "Deleted ticket", details={"ip": client_ip, "user_agent": user_agent})
    elif field == "status" and normalize_status(message.get("status", "Open")) != value:
        await record_ticket_audit_log(
            db,
            message,
            current_user,
            membership,
            "Changed ticket status",
            {"old_status": normalize_status(message.get("status", "Open")), "new_status": value},
        )
    elif field == "assigned_member_id" and message.get("assigned_member_id") != value:
        assigned_user = await db["users"].find_one({"_id": value}) if value else None
        await record_ticket_audit_log(
            db,
            message,
            current_user,
            membership,
            "Assigned ticket" if value else "Unassigned ticket",
            {
                "old_assigned_member_id": str(message.get("assigned_member_id") or ""),
                "new_assigned_member_id": str(value or ""),
                "target_email": assigned_user.get("email", "") if assigned_user else "",
                "ip": client_ip,
                "user_agent": user_agent,
            },
        )
    elif field == "archived" and bool(message.get("archived")) != bool(value):
        # Include request metadata so we can trace unexpected archive actions
        await record_ticket_audit_log(
            db,
            message,
            current_user,
            membership,
            "Archived ticket" if value else "Unarchived ticket",
            {"old_archived": bool(message.get("archived")), "new_archived": bool(value), "ip": client_ip, "user_agent": user_agent},
        )
        try:
            logger.info("Archive change", extra={"actor_email": current_user.get("email", ""), "message_id": str(message.get("_id")), "old": bool(message.get("archived")), "new": bool(value), "ip": client_ip})
        except Exception:
            pass
    return {"message": f"{field} updated", "field": field, "value": serialize_for_json(value)}

def clean_json_response(response: str):
    """
    Cleans a model-generated JSON response by removing code fences and extra text.
    Returns a parsed Python dict, or a default 'no order' dict if parsing fails.
    """
    if not response:
        return {"order_id": "", "type": "", "status": 0, "msg": "No order found in message"}

    # Remove common Markdown code fences like ```json ... ```
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", response.strip())

    # Extract JSON object if surrounded by text accidentally
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # AI returned non-JSON text (e.g. "I don't see any order...")
        # Return a clean default instead of crashing
        logger.warning("AI response was not valid JSON: %s", response[:200])
        return {"order_id": "", "type": "", "status": 0, "msg": "No order found in message"}


def cacheable_order_info(order_info: dict, *, source: str = "ai", keep_shopify_order: bool = False) -> dict:
    """Strip volatile fields before storing analysis results on the message."""
    cached = dict(order_info or {})
    if keep_shopify_order and cached.get("shopify_order"):
        cached["shopify_order"] = serialize_for_json(cached["shopify_order"])
        cached["order_snapshot_updated_at"] = cached["shopify_order"].get("updated_at")
    else:
        cached.pop("shopify_order", None)
    cached["analysis_source"] = source
    cached["analyzed_at"] = datetime.now(timezone.utc)
    return cached


async def update_message_analysis_state(
    db: AsyncIOMotorDatabase,
    message_id: ObjectId,
    *,
    state: str,
    source: str = "",
    error: str = "",
) -> None:
    update = {
        "order_analysis.status": state,
        "order_analysis.updated_at": datetime.now(timezone.utc),
    }
    if source:
        update["order_analysis.source"] = source
    if error:
        update["order_analysis.error"] = error[:500]
    elif state in {"started", "success", "cached"}:
        update["order_analysis.error"] = ""

    await db["messages"].update_one(
        {"_id": message_id},
        {"$set": update},
    )


def serialize_order_action(action: dict) -> dict:
    serialized = dict(action)
    if serialized.get("created_at"):
        serialized["created_at"] = to_utc_iso(serialized["created_at"])
    if serialized.get("actor_id"):
        serialized["actor_id"] = str(serialized["actor_id"])
    return serialized


def parse_action_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def action_amount(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def refund_amount(refund: dict) -> float:
    transactions = refund.get("transactions") or []
    if transactions:
        return round(sum(action_amount(transaction.get("amount")) for transaction in transactions), 2)
    if refund.get("total_refunded") is not None:
        return action_amount(refund.get("total_refunded"))
    line_items = refund.get("refund_line_items") or []
    return round(
        sum(action_amount(item.get("subtotal")) + action_amount(item.get("total_tax")) for item in line_items),
        2,
    )


def refund_shipping_amount(refund: dict) -> float:
    shipping = refund.get("shipping") or {}
    for value in (
        shipping.get("amount"),
        (shipping.get("shop_money") or {}).get("amount"),
        (shipping.get("presentment_money") or {}).get("amount"),
    ):
        amount = action_amount(value)
        if amount:
            return amount

    for adjustment in refund.get("order_adjustments", []) or []:
        kind = str(adjustment.get("kind", "")).lower()
        reason = str(adjustment.get("reason", "")).lower()
        if "shipping" not in kind and "shipping" not in reason:
            continue
        amount = action_amount(adjustment.get("amount"))
        if not amount:
            amount = action_amount((adjustment.get("amount_set") or {}).get("shop_money", {}).get("amount"))
        if amount:
            return abs(amount)
    return 0.0


def find_order_line_item(order: dict, line_item_id) -> dict:
    line_item_id = str(line_item_id or "")
    for item in order.get("line_items", []) or []:
        if str(item.get("id", "")) == line_item_id:
            return item
    return {}


def format_action_line_item(*, name="", quantity=1, amount=None, line_item_id="", variant_id="") -> dict:
    return {
        "name": name or "Unknown item",
        "quantity": int(quantity or 1),
        "amount": action_amount(amount) if amount not in (None, "") else "",
        "line_item_id": str(line_item_id or ""),
        "variant_id": str(variant_id or ""),
    }


def build_refund_line_items(order: dict, refund: dict) -> list[dict]:
    items = []
    for refund_item in refund.get("refund_line_items", []) or []:
        nested_item = refund_item.get("line_item") or {}
        line_item_id = refund_item.get("line_item_id") or nested_item.get("id")
        order_item = find_order_line_item(order, line_item_id)
        items.append(format_action_line_item(
            name=nested_item.get("name") or order_item.get("name"),
            quantity=refund_item.get("quantity"),
            amount=refund_item.get("subtotal"),
            line_item_id=line_item_id,
            variant_id=nested_item.get("variant_id") or order_item.get("variant_id"),
        ))
    return items


def build_refund_shipping_line(refund: dict) -> dict | None:
    amount = refund_shipping_amount(refund)
    if not amount:
        return None
    return {
        "name": "Shipping refund",
        "amount": amount,
    }


def build_selected_line_items(order: dict, selected_items: list[dict]) -> list[dict]:
    items = []
    for selected in selected_items or []:
        line_item_id = selected.get("line_item_id") or selected.get("id")
        order_item = find_order_line_item(order, line_item_id)
        items.append(format_action_line_item(
            name=order_item.get("name"),
            quantity=selected.get("quantity"),
            amount=selected.get("amount") or order_item.get("price"),
            line_item_id=line_item_id,
            variant_id=order_item.get("variant_id"),
        ))
    return items


def enrich_action_details_with_line_items(action: dict, order: dict) -> dict:
    details = dict(action.get("details") or {})
    if details.get("line_items") or details.get("returned_items"):
        action["details"] = details
        return action

    selected_items = details.get("selected_items") or []
    if selected_items:
        line_items = build_selected_line_items(order, selected_items)
        details["line_items"] = line_items
        if action.get("type") in {"return", "exchange"}:
            details["returned_items"] = line_items

    exchange_items = details.get("exchange_items") or []
    if exchange_items:
        details["exchange_items"] = [
            format_action_line_item(
                name=item.get("name") or item.get("title") or f"Variant {item.get('variant_id')}",
                quantity=item.get("quantity"),
                variant_id=item.get("variant_id"),
            )
            for item in exchange_items
        ]

    action["details"] = details
    return action


def build_shopify_order_actions(order: dict) -> list[dict]:
    actions = []

    for refund in order.get("refunds", []) or []:
        amount = refund_amount(refund)
        actions.append({
            "type": "refund",
            "amount": amount,
            "actor_name": "Shopify",
            "actor_role": "system",
            "note": refund.get("note") or "Refund recorded in Shopify",
            "details": {
                "source": "shopify",
                "shopify_refund_id": refund.get("id"),
                "order_id": str(order.get("order_id", "")),
                "transactions": refund.get("transactions", []),
                "line_items": build_refund_line_items(order, refund),
                "shipping_refund": build_refund_shipping_line(refund),
            },
            "created_at": refund.get("created_at") or refund.get("processed_at") or order.get("updated_at") or "",
        })

    if order.get("cancelled_at"):
        actions.append({
            "type": "cancellation",
            "amount": action_amount(order.get("total_price")),
            "actor_name": "Shopify",
            "actor_role": "system",
            "note": order.get("cancel_reason") or "Cancellation recorded in Shopify",
            "details": {
                "source": "shopify",
                "order_id": str(order.get("order_id", "")),
                "cancel_reason": order.get("cancel_reason", ""),
            },
            "created_at": order.get("cancelled_at"),
        })

    for fulfillment in order.get("fulfillments", []) or []:
        if fulfillment.get("created_at"):
            actions.append({
                "type": "fulfillment",
                "amount": "",
                "actor_name": "Shopify",
                "actor_role": "system",
                "note": fulfillment.get("status") or "Fulfillment recorded in Shopify",
                "details": {
                    "source": "shopify",
                    "shopify_fulfillment_id": fulfillment.get("id"),
                    "tracking_number": fulfillment.get("tracking_number"),
                },
                "created_at": fulfillment.get("created_at"),
            })

    return actions


async def hydrate_shopify_refunds(db: AsyncIOMotorDatabase, order: dict) -> dict:
    if order.get("refunds") or str(order.get("payment_status", "")).lower() != "refunded":
        return order

    shop = order.get("shop")
    order_id = order.get("order_id")
    if not shop or not order_id:
        return order

    cred = await db["shopify_cred"].find_one({
        "shop": shop,
        "company_id": order.get("company_id"),
    })
    access_token = (cred or {}).get("access_token")
    if not access_token:
        return order

    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}/refunds.json"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
            )
    except Exception:
        return order

    if response.status_code >= 400:
        return order

    refunds = response.json().get("refunds", [])
    if refunds:
        order["refunds"] = refunds
        await db["orders"].update_one(
            {"_id": order["_id"]},
            {"$set": {"refunds": refunds}},
        )
    return order


def build_inferred_refund_action(order: dict) -> list[dict]:
    if str(order.get("payment_status", "")).lower() != "refunded":
        return []
    if order.get("refunds"):
        return []
    return [{
        "type": "refund",
        "amount": action_amount(order.get("total_price")),
        "actor_name": "Shopify",
        "actor_role": "system",
        "note": "Refunded in Shopify; detailed refund record was not available from Shopify.",
        "details": {
            "source": "shopify",
            "inferred": True,
            "order_id": str(order.get("order_id", "")),
        },
        "created_at": order.get("updated_at") or order.get("created_at") or "",
    }]


def dedupe_order_actions(actions: list[dict]) -> list[dict]:
    deduped = []
    for action in sorted(actions, key=lambda item: item.get("created_at", ""), reverse=True):
        action_type = action.get("type", "")
        amount = action_amount(action.get("amount"))
        created_at = parse_action_datetime(action.get("created_at"))
        duplicate = False
        for existing in deduped:
            if existing.get("type", "") != action_type:
                continue
            if action_amount(existing.get("amount")) != amount:
                continue
            existing_at = parse_action_datetime(existing.get("created_at"))
            if created_at and existing_at:
                if abs((created_at - existing_at).total_seconds()) <= 300:
                    duplicate = True
                    break
            elif action.get("details", {}).get("shopify_refund_id") and action.get("details", {}).get("shopify_refund_id") == existing.get("details", {}).get("shopify_refund_id"):
                duplicate = True
                break
        if not duplicate:
            deduped.append(action)
    return deduped


async def get_order_actions(db: AsyncIOMotorDatabase, order: dict) -> list[dict]:
    order = await hydrate_shopify_refunds(db, order)
    stored_actions = [
        enrich_action_details_with_line_items(serialize_order_action(action), order)
        for action in order.get("order_actions", [])
    ]
    shopify_actions = [
        *build_shopify_order_actions(order),
        *build_inferred_refund_action(order),
    ]

    audit_actions = []
    order_id_values = [str(order.get("order_id", "")), order.get("order_id")]
    cursor = db["audit_logs"].find({
        "company_id": order.get("company_id"),
        "entity_type": "order",
        "action": {"$in": ["Processed refund", "Cancelled order"]},
        "$or": [
            {"entity_id": order.get("_id")},
            {"details.order_id": {"$in": order_id_values}},
        ],
    }).sort("created_at", DESCENDING).limit(50)
    async for log in cursor:
        action_type = "refund" if log.get("action") == "Processed refund" else "cancellation"
        details = log.get("details", {}) or {}
        audit_actions.append({
            "type": action_type,
            "amount": details.get("amount"),
            "actor_id": str(log.get("actor_id") or ""),
            "actor_name": log.get("actor_name", "Unknown user"),
            "actor_role": log.get("actor_role", "unknown"),
            "note": details.get("note", ""),
            "details": details,
            "created_at": to_utc_iso(log.get("created_at")),
        })

    return dedupe_order_actions([*stored_actions, *shopify_actions, *audit_actions])


async def build_order_snapshot(db: AsyncIOMotorDatabase, order: dict) -> dict:
    snapshot = dict(order or {})
    snapshot["order_actions"] = await get_order_actions(db, snapshot)
    return serialize_for_json(snapshot)


def same_email(left: str | None, right: str | None) -> bool:
    return bool(left and right and str(left).strip().lower() == str(right).strip().lower())


def message_search_text(message_doc: dict) -> str:
    parts = [
        str(message_doc.get("subject") or ""),
        str(message_doc.get("snippet") or ""),
        str(message_doc.get("client") or ""),
    ]
    for entry in message_doc.get("messages", []) or []:
        if isinstance(entry, dict):
            parts.extend(
                str(entry.get(field) or "")
                for field in ("subject", "body", "content", "text", "message")
            )
        else:
            parts.append(str(entry))
    return "\n".join(parts)


def mentions_order_number(message_doc: dict) -> bool:
    return bool(re.search(r"#?[A-Za-z]{1,6}\d{3,}", message_search_text(message_doc), re.IGNORECASE))


def message_store_shop(message_doc: dict) -> str:
    return str(message_doc.get("default_store_shop") or "").strip()


def message_store_scope_shops(message_doc: dict) -> list[str]:
    values = message_doc.get("order_matching_store_shops") or []
    if not values and message_store_shop(message_doc):
        values = [message_store_shop(message_doc)]
    return [str(value).strip() for value in values if str(value or "").strip()]


def message_requires_store_scope(message_doc: dict) -> bool:
    return message_doc.get("channel") == "email" and not message_store_scope_shops(message_doc)


async def mark_message_needs_store_scope(db: AsyncIOMotorDatabase, message_doc: dict) -> dict:
    order_info = {
        "order_id": "",
        "type": "",
        "status": 0,
        "msg": "Select an order matching scope for this Gmail account",
        "store_required": True,
    }
    await db["messages"].update_one(
        {"_id": message_doc["_id"]},
        {
            "$set": {
                "order_info": cacheable_order_info(order_info, source="store_required"),
                "order_match_status": "possible",
            }
        },
    )
    await update_message_analysis_state(db, message_doc["_id"], state="success", source="store_required")
    return order_info


def scoped_order_query(message_doc: dict, query: dict) -> dict:
    scoped = dict(query)
    store_shop = message_store_shop(message_doc)
    if store_shop:
        scoped["shop"] = store_shop
    else:
        scope_shops = message_store_scope_shops(message_doc)
        if scope_shops:
            scoped["shop"] = {"$in": scope_shops}
    return scoped


async def find_order_for_message(db: AsyncIOMotorDatabase, message_doc: dict, order_name: str) -> tuple[dict | None, bool]:
    base_query = {
        "company_id": message_doc["company_id"],
        "name": order_name,
    }
    store_shop = message_store_shop(message_doc)
    if store_shop:
        scoped_order = await db["orders"].find_one({**base_query, "shop": store_shop})
        if scoped_order:
            return scoped_order, True
        fallback_order = await db["orders"].find_one(base_query)
        scope_shops = message_store_scope_shops(message_doc)
        if fallback_order and fallback_order.get("shop") in scope_shops:
            return fallback_order, True
        return fallback_order, False
    scope_shops = message_store_scope_shops(message_doc)
    if scope_shops:
        scoped_orders = await db["orders"].find({**base_query, "shop": {"$in": scope_shops}}).to_list(length=2)
        if len(scoped_orders) == 1:
            return scoped_orders[0], True
        if len(scoped_orders) > 1:
            return scoped_orders[0], False
        fallback_order = await db["orders"].find_one(base_query)
        return fallback_order, False
    return await db["orders"].find_one(base_query), True


async def matched_store_fields(db: AsyncIOMotorDatabase, message_doc: dict, order_doc: dict) -> dict:
    shop = order_doc.get("shop")
    if not shop:
        return {}
    fields = {"default_store_shop": shop}
    cred = await db["shopify_cred"].find_one({
        "company_id": message_doc.get("company_id"),
        "shop": shop,
        "status": {"$ne": "disconnected"},
    })
    if cred:
        fields["default_store_id"] = cred["_id"]
    return fields


async def refresh_confirmed_order_if_needed(
    db: AsyncIOMotorDatabase,
    message_doc: dict,
    order_info: dict,
    db_order: dict,
) -> tuple[dict, bool]:
    """For confirmed tickets, check one Shopify order's updated_at and refresh only if changed."""
    if not order_info.get("confirmed") or not db_order:
        return db_order, False

    snapshot = order_info.get("shopify_order") or {}
    shop = snapshot.get("shop") or db_order.get("shop")
    order_id = snapshot.get("order_id") or db_order.get("order_id")
    if not shop or not order_id:
        return db_order, False

    cred = await db["shopify_cred"].find_one({
        "company_id": message_doc["company_id"],
        "shop": shop,
        "status": "connected",
        "access_token": {"$exists": True, "$ne": ""},
    })
    if not cred:
        logger.warning("[ANALYZE] confirmed order refresh skipped; no connected Shopify token for %s", shop)
        return db_order, False

    remote_updated_at = await fetch_order_updated_at_from_shop(shop, cred["access_token"], order_id)
    local_snapshot_updated_at = _to_datetime(
        snapshot.get("updated_at")
        or order_info.get("order_snapshot_updated_at")
        or db_order.get("updated_at")
    )
    if remote_updated_at and local_snapshot_updated_at and remote_updated_at <= local_snapshot_updated_at:
        return db_order, False

    if not remote_updated_at:
        return db_order, False

    shopify_order = await fetch_order_from_shop(shop, cred["access_token"], order_id)
    if not shopify_order:
        return db_order, False

    await upsert_orders(db, shop, [shopify_order])
    refreshed_order = await db["orders"].find_one({
        "company_id": message_doc["company_id"],
        "shop": shop,
        "order_id": int(order_id),
    })
    return refreshed_order or db_order, bool(refreshed_order)
    
@router.post("/analyze_as_list", response_model=list)
async def analyze_email_message_as_list(
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    """
    Analyze all email ChatEntry objects in a message and extract order/refund/cancel info as JSON.
    Input: JSON body with { "message_id": str }.
    Output: List of JSON results, one per ChatEntry.
    """
    message_id = body.get("message_id")
    if not message_id or not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    doc = await ensure_message_access(message_id, db, current_user, action="read")

    result = await analyze_emails_with_ai(doc)
    order_list = []
    for entry in result:
        try:
            order_info = json.loads(entry["response"])
            if order_info.get("order_id") and order_info.get("status") == 1:
                order_info["shopify_order"] = {}
                order_list.append(order_info)
        except Exception:
            continue

    return order_list

@router.post("/analyze", response_model=dict)
async def analyze_email_message(
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    """
    Analyze the last three email ChatEntry objects in a message and extract order/refund/cancel info as JSON.
    Input: JSON body with { "message_id": str }.
    Output: Single JSON result for the combined analysis.
    """
    message_id = body.get("message_id")
    if not message_id or not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    logger.info("[ANALYZE] Request received for message_id=%s from user=%s", message_id, current_user.get("email", "?"))

    message_doc = await ensure_message_access(message_id, db, current_user, action="update")

    # Extract customer email for order lookups
    client_email = ""
    client_str = message_doc.get("client") or ""
    email_match = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', client_str)
    if email_match:
        client_email = email_match[0]

    if message_requires_store_scope(message_doc):
        logger.info("[ANALYZE] message_id=%s needs order matching scope", message_id)
        order_info = await mark_message_needs_store_scope(db, message_doc)
        order_info["shopify_order"] = {}
        return order_info

    # Requirement 1: If customer email has zero orders, skip AI entirely (no loading)
    if client_email and not mentions_order_number(message_doc):
        order_count = await db["orders"].count_documents(scoped_order_query(message_doc, {
            "company_id": message_doc["company_id"],
            "customer.email": {"$regex": f"^{re.escape(client_email)}$", "$options": "i"},
        }))
        if order_count == 0:
            logger.info("[ANALYZE] message_id=%s customer has no orders, skipping AI", message_id)
            order_info = {
                "order_id": "",
                "type": "",
                "status": 0,
                "msg": "No orders for this customer",
                "no_orders": True,
            }
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {"$set": {
                    "order_info": cacheable_order_info(order_info),
                    "order_match_status": "not_order",
                }}
            )
            await update_message_analysis_state(db, message_doc["_id"], state="success", source="no_orders")
            order_info["shopify_order"] = {}
            return order_info

    has_messages = bool(message_doc.get("messages"))
    logger.info("[ANALYZE] message_id=%s has_messages=%s has_order_info=%s", message_id, has_messages, bool(message_doc.get("order_info")))

    # Check cached order_info
    order_info = message_doc.get('order_info')
    if order_info and order_info.get("order_id"):
        # Requirement 3: Check if Shopify order was updated since last analysis
        shopify_updated = False
        order_name = order_info["order_id"] if order_info["order_id"].startswith("#") else "#" + order_info["order_id"]
        db_order, store_scoped_match = await find_order_for_message(db, message_doc, order_name)

        if db_order and order_info.get("analyzed_at"):
            try:
                analyzed_dt = _to_datetime(order_info["analyzed_at"])
                shopify_dt = _to_datetime(db_order.get("updated_at"))
                if analyzed_dt and shopify_dt and shopify_dt > analyzed_dt:
                    shopify_updated = True
                    logger.info("[ANALYZE] message_id=%s Shopify order updated, re-fetching", message_id)
            except Exception:
                pass

        if db_order and order_info.get("confirmed"):
            try:
                db_order, refreshed_from_shopify = await refresh_confirmed_order_if_needed(
                    db,
                    message_doc,
                    order_info,
                    db_order,
                )
                if refreshed_from_shopify:
                    shopify_updated = True
                    logger.info("[ANALYZE] message_id=%s confirmed order refreshed from Shopify", message_id)
            except Exception as exc:
                logger.warning("[ANALYZE] confirmed order refresh failed for %s: %s", message_id, exc)

        if not shopify_updated and db_order and store_scoped_match and (
            not client_email or same_email(db_order.get("customer", {}).get("email", ""), client_email)
        ):
            # Cached order_info is fresh: return immediately with attached shopify_order
            order_info["shopify_order"] = await build_order_snapshot(db, db_order)
            if order_info.get("confirmed"):
                store_fields = await matched_store_fields(db, message_doc, db_order)
                await db["messages"].update_one(
                    {"_id": message_doc["_id"]},
                    {"$set": {
                        "order_info": cacheable_order_info(
                            order_info,
                            source="confirmed",
                            keep_shopify_order=True,
                        ),
                        **store_fields,
                    }},
                )
            await update_message_analysis_state(db, message_doc["_id"], state="cached", source="order_info")
            logger.info(
                "Order analysis skipped; cached order_info is fresh",
                extra={
                    "message_id": message_id,
                    "company_id": str(message_doc.get("company_id", "")),
                    "ticket": message_doc.get("ticket", ""),
                    "order_id": str(order_info.get("order_id", "")),
                },
            )
            return order_info

        # Cached but Shopify was updated or email mismatch: fall through to re-attach shopify_order
        if not shopify_updated:
            await update_message_analysis_state(db, message_doc["_id"], state="cached", source="order_info")
        # else: fall through to re-process the shopify_order attachment below
    else:
        # No valid order_info: run AI analysis
        logger.info(
            "Order analysis started",
            extra={
                "message_id": message_id,
                "company_id": str(message_doc.get("company_id", "")),
                "ticket": message_doc.get("ticket", ""),
                "actor_email": current_user.get("email", ""),
            },
        )
        await update_message_analysis_state(db, message_doc["_id"], state="started", source="gemini")
        result = await analyze_emails_with_ai(message_doc)

        if isinstance(result, dict) and result.get("error"):
            error_message = str(result.get("msg", result.get("error", "Unknown AI error")))
            error_detail = result.get("error", "UNKNOWN")
            error_reason = result.get("reason", "")
            error_model = result.get("model", "")
            error_retry = result.get("retry_after_seconds", "")

            order_info = {
                "order_id": "",
                "type": "",
                "status": 0,
                "msg": error_message,
            }
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_match_status": "unknown",
                    }
                },
            )
            await update_message_analysis_state(
                db,
                message_doc["_id"],
                state="failed",
                source="gemini",
                error=error_message,
            )
            logger.warning(
                "[ANALYZE FAIL] message_id=%s code=%s reason=%s model=%s retry_after=%s msg=%s",
                message_id,
                error_detail,
                error_reason,
                error_model,
                error_retry,
                error_message[:200],
            )
            order_info["shopify_order"] = {}
            return order_info
        
        response = getattr(result, 'content', str(result))
        logger.debug("Email AI process response: %s", str(response)[:200])
        order_info = clean_json_response(response)

        await db["messages"].update_one(
            {"_id": message_doc["_id"]},
            {
                "$set": {
                    "order_info": cacheable_order_info(order_info),
                }
            }
        )
        await update_message_analysis_state(db, message_doc["_id"], state="success", source="gemini")
        logger.info(
            "Order analysis stored",
            extra={
                "message_id": message_id,
                "company_id": str(message_doc.get("company_id", "")),
                "ticket": message_doc.get("ticket", ""),
                "actor_email": current_user.get("email", ""),
                "order_id": str(order_info.get("order_id", "")),
            },
        )
    
    # --- Attach shopify_order from DB (runs for: fresh AI result, or cached but re-fetch needed) ---
    order_id = str(order_info.get("order_id", ""))
    if not order_id:
        order_info["msg"] = order_info.get("msg") or "No order found in message"
        await db["messages"].update_one(
            {"_id": message_doc["_id"]},
            {
                "$set": {
                    "order_info": cacheable_order_info(order_info),
                    "order_match_status": "not_order",
                }
            },
        )
        await update_message_analysis_state(db, message_doc["_id"], state="success", source="not_order")
        logger.info(
            "Order analysis completed as not_order",
            extra={
                "message_id": message_id,
                "company_id": str(message_doc.get("company_id", "")),
                "ticket": message_doc.get("ticket", ""),
            },
        )
        order_info["shopify_order"] = {}
        return order_info

    order_name = order_id if order_id.startswith("#") else "#" + order_id

    db_order, store_scoped_match = await find_order_for_message(db, message_doc, order_name)
    
    if db_order:
        order_info["shopify_order"] = await build_order_snapshot(db, db_order)
        if store_scoped_match and (not client_email or same_email(db_order.get("customer", {}).get("email", ""), client_email)):
            store_fields = await matched_store_fields(db, message_doc, db_order)
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_info": cacheable_order_info(
                            order_info,
                            source="confirmed" if order_info.get("confirmed") else "matched",
                            keep_shopify_order=bool(order_info.get("confirmed")),
                        ),
                        "order_match_status": "matched",
                        "matched_order_id": str(db_order.get("order_id", "")),
                        "matched_order_name": db_order.get("name", ""),
                        **store_fields,
                    }
                },
            )
            await update_message_analysis_state(db, message_doc["_id"], state="success", source="matched")
            logger.info(
                "Order analysis matched order",
                extra={
                    "message_id": message_id,
                    "company_id": str(message_doc.get("company_id", "")),
                    "ticket": message_doc.get("ticket", ""),
                    "order_name": db_order.get("name", ""),
                },
            )

        else:
            order_info["msg"] = "Store not matched" if not store_scoped_match else "Email not matched"
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_info": cacheable_order_info(
                            order_info,
                            source="confirmed" if order_info.get("confirmed") else "possible",
                            keep_shopify_order=bool(order_info.get("confirmed")),
                        ),
                        "order_match_status": "possible",
                        "matched_order_id": str(db_order.get("order_id", "")),
                        "matched_order_name": db_order.get("name", ""),
                    }
                },
            )
            await update_message_analysis_state(db, message_doc["_id"], state="success", source="possible")
            logger.info(
                "Order analysis found possible order",
                extra={
                    "message_id": message_id,
                    "company_id": str(message_doc.get("company_id", "")),
                    "ticket": message_doc.get("ticket", ""),
                    "order_name": db_order.get("name", ""),
                },
            )

    else:
        order_info["msg"] = "Order not found"
        order_info["shopify_order"] = {}
        await db["messages"].update_one(
            {"_id": message_doc["_id"]},
            {
                "$set": {
                    "order_info": cacheable_order_info(order_info),
                    "order_match_status": "unmatched",
                }
            },
        )
        await update_message_analysis_state(db, message_doc["_id"], state="success", source="unmatched")
        logger.info(
            "Order analysis did not find order",
            extra={
                "message_id": message_id,
                "company_id": str(message_doc.get("company_id", "")),
                "ticket": message_doc.get("ticket", ""),
                "order_id": order_id,
            },
        )

    return order_info


@router.get("/{id}/attachments/{gmail_message_id}/{attachment_id}", response_class=Response)
async def download_message_attachment(
    id: str,
    gmail_message_id: str,
    attachment_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    message = await ensure_message_access(id, db, current_user, action="read")
    matched_attachment = None

    for entry in message.get("messages", []) or []:
        for attachment in (entry.get("metadata") or {}).get("attachments") or []:
            if (
                attachment.get("gmail_message_id") == gmail_message_id
                and attachment.get("attachment_id") == attachment_id
            ):
                matched_attachment = attachment
                break
        if matched_attachment:
            break

    if not matched_attachment:
        raise HTTPException(status_code=404, detail="Attachment not found for this message.")

    account_email = matched_attachment.get("account_email")
    gmail_account = None
    if account_email:
        gmail_account = await db["gmail_accounts"].find_one({"email": account_email})
    if not gmail_account and message.get("gmail_account_id"):
        gmail_account = await db["gmail_accounts"].find_one({"_id": message.get("gmail_account_id")})
    if not gmail_account:
        agent_email = parseaddr(message.get("agent") or "")[1]
        if agent_email:
            gmail_account = await db["gmail_accounts"].find_one({"email": agent_email})
    if not gmail_account:
        raise HTTPException(status_code=400, detail="Gmail credentials not found for attachment download.")

    service = get_gmail_service(gmail_account)
    attachment = service.users().messages().attachments().get(
        userId="me",
        messageId=gmail_message_id,
        id=attachment_id,
    ).execute()

    data = decode_gmail_attachment_data(attachment.get("data", ""))
    filename = sanitize_attachment_filename(matched_attachment.get("filename"))
    return Response(
        content=data,
        media_type=matched_attachment.get("mime_type") or "application/octet-stream",
        headers={"Content-Disposition": attachment_disposition(filename)},
    )


@router.post("/{id}/reply", response_model=dict)
async def reply_to_message(
    id: str,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    """
    Reply to a message by adding a new ChatEntry and sending email via Gmail API.
    Input: Message ID (path) and reply content (body).
    Output: Updated message document.
    """
    
    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    content_type = request.headers.get("content-type", "")
    attachments: list[dict] = []

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        content = str(form.get("content") or "").strip()
        client_request_id = str(form.get("client_request_id") or "").strip()
        uploaded_files = form.getlist("files")
        total_size = 0
        for uploaded_file in uploaded_files:
            filename = sanitize_attachment_filename(getattr(uploaded_file, "filename", ""))
            if not getattr(uploaded_file, "filename", ""):
                continue
            data = await uploaded_file.read()
            validate_attachment(filename, data)
            total_size += len(data)
            if total_size > MAX_TOTAL_ATTACHMENT_SIZE:
                raise HTTPException(status_code=400, detail="Total attachment size cannot exceed 20MB.")
            mime_type = getattr(uploaded_file, "content_type", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(data),
                    "content_hash": hashlib.sha256(data).hexdigest(),
                    "data": data,
                }
            )
    else:
        body = await request.json()
        content = (body.get("content") or "").strip()
        client_request_id = str(body.get("client_request_id") or "").strip()

    if not content and not attachments:
        raise HTTPException(status_code=400, detail="Reply content or attachment is required")

    message = await ensure_message_access(id, db, current_user, action="update")
    if client_request_id:
        duplicate_message = next(
            (
                item
                for item in message.get("messages", [])
                if (item.get("metadata") or {}).get("client_request_id") == client_request_id
            ),
            None,
        )
        if duplicate_message:
            logger.info(
                "Duplicate email reply request ignored for message=%s request_id=%s",
                id,
                client_request_id,
            )
            return serialize_for_json(message)

    attachments, skipped_attachments = _dedupe_reply_attachments(message, attachments)
    if skipped_attachments:
        logger.info(
            "Skipped %d duplicate email reply attachment(s) for message=%s",
            len(skipped_attachments),
            id,
        )
    if not content and not attachments:
        raise HTTPException(
            status_code=400,
            detail="This attachment was already sent. Add a reply message to send without the duplicate attachment.",
        )
    
    # Find latest client message for threading
    client_message = None
    for msg in reversed(message.get("messages", [])):
        if msg.get("sender") == message.get("client"):
            client_message = msg
            break
    if not client_message:
        raise HTTPException(status_code=400, detail="No client message to reply to.")
    
    # Identify Gmail user (agent sending reply)
    agent_id = None
    agent_id = message.get("agent")  # agent_id should be the email of the agent

    if not agent_id:
        raise HTTPException(status_code=400, detail="No Gmail user found in participants.")

    _, agent_email = parseaddr(agent_id)
    user_creds = await db["gmail_accounts"].find_one({"email": agent_email})
    if not user_creds:
        raise HTTPException(status_code=400, detail="User Gmail credentials not found.")

    thread_id = message.get("thread_id")
    subject = client_message.get("title", "No Subject")
    client_metadata = client_message.get("metadata", {})
    original_gmail_id = client_metadata.get("gmail_id")
    original_msg_id = _normalize_rfc_message_id(
        client_metadata.get("rfc_message_id") or client_metadata.get("message_id")
    )
    references = _build_references(client_metadata.get("references"), original_msg_id)
    to_addr = _reply_recipient(client_message, message.get("client"))
    if not to_addr:
        raise HTTPException(status_code=400, detail="No recipient email found for this reply.")

    # Send via Gmail API
    try:
        service = get_gmail_service(user_creds)
    except RefreshError:
        logger.warning("Gmail credentials need reconnect for %s while sending reply", agent_email, exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Gmail credentials need reconnect. Please reconnect this Gmail account.",
        )
    except Exception:
        logger.error("Failed to initialize Gmail service for %s while sending reply", agent_email, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to connect to Gmail. Please try again.")

    if not original_msg_id and original_gmail_id:
        try:
            original_full = service.users().messages().get(
                userId="me",
                id=original_gmail_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References", "Reply-To", "From"],
            ).execute()
            original_headers = (original_full.get("payload") or {}).get("headers", [])
            original_msg_id = _normalize_rfc_message_id(
                _gmail_header(original_headers, "Message-ID")
            )
            references = _build_references(
                _gmail_header(original_headers, "References"),
                original_msg_id,
            )
            to_addr = _reply_recipient(
                {
                    **client_message,
                    "metadata": {
                        **client_metadata,
                        "reply_to": client_metadata.get("reply_to") or _gmail_header(original_headers, "Reply-To"),
                        "from": client_metadata.get("from") or _gmail_header(original_headers, "From"),
                    },
                },
                message.get("client"),
            )
        except HttpError:
            logger.warning(
                "Could not load RFC Message-ID for Gmail reply threading",
                exc_info=True,
            )
        except Exception:
            logger.warning("Could not load original Gmail headers for reply threading", exc_info=True)

    if attachments:
        mime_msg = MIMEMultipart("mixed")
        mime_msg.attach(_reply_body_part(content or ""))
        for attachment in attachments:
            maintype, subtype = attachment["mime_type"].split("/", 1) if "/" in attachment["mime_type"] else ("application", "octet-stream")
            part = MIMEBase(maintype, subtype)
            part.set_payload(attachment["data"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=attachment["filename"])
            mime_msg.attach(part)
    else:
        mime_msg = _reply_body_part(content)

    mime_msg['To'] = to_addr
    mime_msg['From'] = agent_email
    mime_msg['Subject'] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original_msg_id:
        mime_msg['In-Reply-To'] = original_msg_id
    if references:
        mime_msg['References'] = references
    mime_msg['Date'] = formatdate(localtime=True)

    raw_message = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        sent = service.users().messages().send(
            userId="me",
            body={
                'raw': raw_message,
                'threadId': thread_id
            }
        ).execute()
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None) or 502
        logger.error("Gmail send failed for %s with status %s", agent_email, status, exc_info=True)
        if status in (401, 403):
            raise HTTPException(
                status_code=400,
                detail="Gmail send permission failed. Please reconnect this Gmail account and approve Gmail read/send access.",
            )
        raise HTTPException(status_code=502, detail="Gmail send failed. Please try again.")
    except Exception:
        logger.error("Unexpected Gmail send failure for %s", agent_email, exc_info=True)
        raise HTTPException(status_code=502, detail="Gmail send failed. Please try again.")

    sent_attachments = []
    if attachments:
        try:
            sent_full = service.users().messages().get(
                userId="me",
                id=sent.get("id"),
                format="full",
            ).execute()
            sent_attachments = extract_gmail_attachments(
                sent_full.get("payload", {}),
                gmail_message_id=sent.get("id"),
                account_email=agent_email,
            )
            by_key = {
                _attachment_key(item["filename"], item["size"]): item.get("content_hash")
                for item in attachments
            }
            for sent_attachment in sent_attachments:
                content_hash = by_key.get(
                    _attachment_key(sent_attachment.get("filename", ""), sent_attachment.get("size", 0))
                )
                if content_hash:
                    sent_attachment["content_hash"] = content_hash
        except Exception:
            sent_attachments = [
                {
                    "filename": item["filename"],
                    "mime_type": item["mime_type"],
                    "size": item["size"],
                    "content_hash": item.get("content_hash"),
                    "gmail_message_id": sent.get("id"),
                    "account_email": agent_email,
                }
                for item in attachments
            ]

    # Construct ChatEntry and save to DB
    now = datetime.now(timezone.utc).astimezone()
    reply_entry = {
        "sender": agent_email,
        "recipient": to_addr,
        "content": content,
        "title": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        "timestamp": datetime.now(timezone.utc),
        "message_type": "html",
        "channel": "email",
        "metadata": {
            "gmail_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "from": agent_email,
            "to": to_addr,
            "reply_to_used": client_metadata.get("reply_to", ""),
            "date": format_datetime(now),
            "in_reply_to": original_msg_id,
            "references": references,
            "client_request_id": client_request_id,
            "attachments": sent_attachments,
        }
    }

    await db["messages"].update_one(
        {"_id": ObjectId(id)},
        {
            "$push": {"messages": reply_entry},
            "$set": {"last_updated": reply_entry["timestamp"]}
        }
    )
    await sio.emit(
        "gmail_update",
        {
            "user_id": str(message.get("user_id", "")),
            "company_id": str(message.get("company_id", "")),
            "email": agent_email,
            "message": f"Reply sent from {agent_email}",
        },
    )

    updated_message = await db["messages"].find_one({"_id": ObjectId(id)})

    return serialize_for_json(updated_message)
