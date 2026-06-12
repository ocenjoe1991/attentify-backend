# app/routes/message.py

from fastapi import APIRouter, HTTPException, Depends, Body, Query
import os
import httpx
from app.services.gmail_service import fetch_all_gmail_accounts, get_gmail_service
from app.db.mongodb import get_database
from app.models.message import Message, ChatEntry, PyObjectId 
from typing import List
import re
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from app.services.ai_service import analyze_emails_with_ai
import json
from bson import ObjectId
import base64
from email.utils import formatdate, format_datetime
from email.mime.text import MIMEText
from datetime import datetime, timezone
from email.utils import parseaddr
from pymongo import ASCENDING, DESCENDING
from app.core.security import get_current_user
from app.core.permissions import (
    OWNER_ROLES,
    PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL,
    can_permanently_delete_ticket,
    has_owner_approval_bypass,
)
from app.core.audit import record_audit_log

from math import ceil

router = APIRouter()

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")

TICKET_STATUSES = {
    "Open",
    "Assigned",
    "In Progress",
    "Pending",
    "Resolved",
    "Escalated",
    "Awaiting Approval",
    "Canceled",
}

LEGACY_STATUS_MAP = {
    "Closed": "Resolved",
    "Cancelled": "Canceled",
    "open": "Open",
    "closed": "Resolved",
    "pending": "Pending",
}

ACTIVE_STATUSES = {
    "Open",
    "Assigned",
    "In Progress",
    "Pending",
    "Escalated",
    "Awaiting Approval",
}

ARCHIVED_STATUSES = {
    "Resolved",
    "Canceled",
}

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

def doc_to_message(doc: dict) -> Message:
    # Clean client
    raw_client = doc.get("client", "")
    cleaned_client = extract_name(raw_client)

    return Message(
        id=PyObjectId(doc['_id']),
        client=cleaned_client,
        agent=doc.get("agent"),
        session_id=doc.get("session_id"),
        started_at=doc.get("started_at"),
        last_updated=doc.get("last_updated"),
        status=normalize_status(doc.get("status", "Open")),
        channel=doc.get("channel"),
        title=doc.get("title"),
        ai_summary=doc.get("ai_summary"),
        tags=doc.get("tags", []),
        resolved_by_ai=doc.get("resolved_by_ai", False),
    )

def doc_to_message_detail(doc: dict) -> Message:
    return Message(
        id=doc["_id"],
        client=extract_name(doc.get("client", "")),
        agent=doc.get("agent"),
        session_id=doc.get("session_id"),
        started_at=doc.get("started_at"),
        last_updated=doc.get("last_updated"),
        status=normalize_status(doc.get("status", "Open")),
        channel=doc.get("channel"),
        title=doc.get("title"),
        ai_summary=doc.get("ai_summary"),
        tags=doc.get("tags", []),
        resolved_by_ai=doc.get("resolved_by_ai", False),
        messages=[]  # or omit this line if optional in schema
    )

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

@router.get("/", response_model=List[dict])
async def get_messages(db=Depends(get_database), current_user: dict = Depends(get_current_user)):
    cursor = db["messages"].find({"user_id": current_user["_id"]}).sort("last_updated", DESCENDING)
    messages = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"]) 
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])
        raw_client = doc.get("client", "")
        cleaned_client = extract_name(raw_client)
        doc["client"] = cleaned_client

        # Assigned member
        member = None
        assigned_member_id = doc.get("assigned_member_id")
        if assigned_member_id:
            try:
                member_obj = await db["users"].find_one({"_id": assigned_member_id if isinstance(assigned_member_id, ObjectId) else ObjectId(assigned_member_id)})
                if member_obj:
                    member_obj["_id"] = str(member_obj["_id"])
                    # Include only desired member fields
                    member = {
                        "id": member_obj["_id"],
                        "name": f"{member_obj.get('first_name', '')} {member_obj.get('last_name', '')}".strip(),
                        "email": member_obj.get("email", "")
                    }
            except Exception:
                member = None
        doc["assigned_to"] = member
        if "assigned_member_id" in doc and doc["assigned_member_id"]:
            doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        messages.append(doc)
    return messages

@router.get("/company_messages", response_model=dict)
async def get_company_messages(
    company_id: str = Query(..., description="ID of the company"),
    search: str = Query("", description="Search by message title or client name/email"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Page size"),
    view_mode: str = Query("inbox", description="inbox, archived, or trashed"),
    assigned_filter: str = Query("all", description="all, assigned, or unassigned"),
    status_filter: str = Query("all", description="Message status or all"),
    order_filter: str = Query("all", description="all, order, other, or needs_review"),
    sort_by: str = Query("last_updated", description="started_at or last_updated"),
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
        query["assigned_member_id"] = current_user["_id"]
    elif role not in ["company_owner", "store_owner", "agent", "readonly"]:
        query["user_id"] = current_user["_id"]

    if view_mode == "inbox":
        query["trashed"] = {"$ne": True}
        query["archived"] = {"$ne": True}
        query["status"] = {"$in": list(ACTIVE_STATUSES)}
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
        query["$or"] = [
            {"title": search_regex},
            {"client": search_regex},
            {"ticket": search_regex},
        ]

    if assigned_filter == "assigned":
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

    if status_filter != "all":
        status_filter = normalize_status(status_filter)
        if status_filter not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status filter")
        if view_mode == "inbox" and status_filter not in ACTIVE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status for inbox")
        if view_mode == "archived" and status_filter not in ARCHIVED_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status for archive")
        query["status"] = status_filter

    sort_field = "started_at" if sort_by == "started_at" else "last_updated"
    sort_direction = ASCENDING if sort_order == "asc" else DESCENDING

    # Count total documents for pagination
    total_count = await db["messages"].count_documents(query)
    totalPages = ceil(total_count / size)

    # Pagination
    skip = (page - 1) * size

    pipeline = [
        {"$match": query},
        {
            "$addFields": {
                "_sort_date": {
                    "$ifNull": [
                        f"${sort_field}",
                        {"$ifNull": ["$last_updated", "$started_at"]},
                    ]
                }
            }
        },
        {"$sort": {"_sort_date": sort_direction, "_id": sort_direction}},
        {"$skip": skip},
        {"$limit": size},
    ]

    messages = []
    async for doc in db["messages"].aggregate(pipeline):
        doc["_id"] = str(doc["_id"])
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])
        doc["status"] = normalize_status(doc.get("status", "Open"))
        doc["order_match_status"] = doc.get("order_match_status", "unknown")
        doc.pop("_sort_date", None)

        # Clean client name
        raw_client = doc.get("client", "")
        doc["client"] = extract_name(raw_client)

        # Get assigned member details
        assigned_member_id = doc.get("assigned_member_id")
        member = None
        if assigned_member_id:
            try:
                member_obj = await db["users"].find_one(
                    {"_id": assigned_member_id if isinstance(assigned_member_id, ObjectId) else ObjectId(assigned_member_id)}
                )
                if member_obj:
                    member = {
                        "id": str(member_obj["_id"]),
                        "name": f"{member_obj.get('first_name', '')} {member_obj.get('last_name', '')}".strip(),
                        "email": member_obj.get("email", "")
                    }
            except Exception:
                member = None
        doc["assigned_to"] = member

        # Cleanup unused fields
        doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        doc.pop("comments", None)

        messages.append(doc)

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

    # Convert ObjectIds to strings
    doc["_id"] = str(doc["_id"])
    doc["user_id"] = str(doc["user_id"])
    doc["company_id"] = str(doc["company_id"])
    if "assigned_member_id" in doc and doc["assigned_member_id"]:
        doc["assigned_member_id"] = str(doc["assigned_member_id"])
    doc["status"] = normalize_status(doc.get("status", "Open"))

    # Properly await comment serialization
    comments = []
    for c in doc.get("comments", []):
        comments.append(await serialize_comment(c, db))
    doc["comments"] = comments

    return doc

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
        safe_payload["order_match_status"] = "matched"
        if safe_payload.get("order_info.order_id"):
            safe_payload["matched_order_name"] = safe_payload["order_info.order_id"]
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
    safe_payload["last_updated"] = datetime.utcnow()
    await db["messages"].find_one_and_update(
        {"_id": ObjectId(id)},
        {"$set": safe_payload}
    )
    return {"message": "Message updated"}

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
    if role == "agent" and message.get("assigned_member_id") != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Message is not assigned to this user")
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

    result = await db["messages"].delete_one({"_id": message["_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    await record_ticket_audit_log(db, message, current_user, membership, "Permanently deleted ticket")

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
        "created_at": comment["created_at"].strftime("%Y-%m-%d %H:%M:%S") if comment.get("created_at") else None,
        "updated_at": comment["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if comment.get("updated_at") else None,
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
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
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
                "comments.$.updated_at": datetime.utcnow()
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
                "comments.$.updated_at": datetime.utcnow()
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
    allowed_fields = {"assigned_member_id", "status", "trashed", "archived"}
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

    # Perform update
    set_payload = {field: value, "last_updated": datetime.utcnow()}
    if field == "assigned_member_id" and value and normalize_status(message.get("status", "Open")) == "Open":
        set_payload["status"] = "Assigned"
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$set": set_payload}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")
    if field == "trashed" and value is True and not message.get("trashed"):
        await record_ticket_audit_log(db, message, current_user, membership, "Deleted ticket")
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
            },
        )
    elif field == "archived" and bool(message.get("archived")) != bool(value):
        await record_ticket_audit_log(
            db,
            message,
            current_user,
            membership,
            "Archived ticket" if value else "Unarchived ticket",
        )
    return {"message": f"{field} updated"}

def clean_json_response(response: str):
    """
    Cleans a model-generated JSON response by removing code fences and extra text.
    Returns a parsed Python dict.
    """
    if not response:
        return {}

    # Remove common Markdown code fences like ```json ... ```
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", response.strip())

    # Extract JSON object if surrounded by text accidentally
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON response: {e}\nRaw text: {response}")


def serialize_order_action(action: dict) -> dict:
    serialized = dict(action)
    if serialized.get("created_at"):
        serialized["created_at"] = serialized["created_at"].isoformat() if hasattr(serialized["created_at"], "isoformat") else serialized["created_at"]
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
            "created_at": log.get("created_at").isoformat() if log.get("created_at") else "",
        })

    return dedupe_order_actions([*stored_actions, *shopify_actions, *audit_actions])
    
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

    message_doc = await ensure_message_access(message_id, db, current_user, action="update")

    if not (order_info := message_doc.get('order_info')):
        result = await analyze_emails_with_ai(message_doc)
        # result is now a single dict, not a list

        if isinstance(result, dict) and result.get("error"):
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {"$set": {"order_match_status": "unknown"}},
            )
            return {
                "order_id": "",
                "type": "",
                "status": 0,
                "msg": result["error"],
                "shopify_order": {},
            }
        
        response = getattr(result, 'content', str(result))
        print("Email AI process response: ", response)
        try:
            order_info = clean_json_response(response)
        except ValueError as exc:
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {"$set": {"order_match_status": "unknown"}},
            )
            return {
                "order_id": "",
                "type": "",
                "status": 0,
                "msg": str(exc),
                "shopify_order": {},
            }

        if (order_info.get('order_id')):
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_info": order_info,
                    }
                }
            )
    
    order_id = str(order_info.get("order_id", ""))
    if not order_id:
        await db["messages"].update_one(
            {"_id": message_doc["_id"]},
            {"$set": {"order_match_status": "not_order"}},
        )
        order_info["msg"] = order_info.get("msg") or "No order found in message"
        order_info["shopify_order"] = {}
        return order_info

    order_name = order_id if order_id.startswith("#") else "#" + order_id

    db_order = await db["orders"].find_one({"name": order_name})
    
    if db_order:
        match = re.findall(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
            message_doc.get("client") or "",
        )
        email = match[0] if match else ""

        if email and db_order.get("customer", {}).get("email", "") == email:
            db_order["order_actions"] = await get_order_actions(db, db_order)
            db_order["_id"] = str(db_order["_id"])
            db_order["user_id"] = str(db_order.get("user_id", ""))
            db_order["company_id"] = str(db_order.get("company_id", ""))
            order_info["shopify_order"] = db_order
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_match_status": "matched",
                        "matched_order_id": str(db_order.get("order_id", "")),
                        "matched_order_name": db_order.get("name", ""),
                    }
                },
            )

        else:
            order_info["msg"] = "Email not matched"
            order_info["shopify_order"] = {}
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_match_status": "possible",
                        "matched_order_id": str(db_order.get("order_id", "")),
                        "matched_order_name": db_order.get("name", ""),
                    }
                },
            )

    else:
        order_info["msg"] = "Order not found"
        order_info["shopify_order"] = {}
        await db["messages"].update_one(
            {"_id": message_doc["_id"]},
            {"$set": {"order_match_status": "unmatched"}},
        )

    return order_info

@router.post("/{id}/reply", response_model=dict)
async def reply_to_message(
    id: str,
    body: dict = Body(...),  # expects: { "content": "the reply text" }
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
    
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Reply content is required")

    message = await ensure_message_access(id, db, current_user, action="update")
    
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
    original_msg_id = client_message.get("metadata", {}).get("gmail_id")
    to_addr = message.get("client")  # recipient (client)

    if original_msg_id and not original_msg_id.startswith("<"):
        original_msg_id = f"<{original_msg_id}>"

    mime_msg = MIMEText(content, "html")
    mime_msg['To'] = to_addr
    mime_msg['From'] = agent_email
    mime_msg['Subject'] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original_msg_id:
        mime_msg['In-Reply-To'] = original_msg_id
        mime_msg['References'] = original_msg_id
    mime_msg['Date'] = formatdate(localtime=True)

    raw_message = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    
    # Send via Gmail API
    service = get_gmail_service(user_creds)
    sent = service.users().messages().send(
        userId="me",
        body={
            'raw': raw_message,
            'threadId': thread_id
        }
    ).execute()

    # Construct ChatEntry and save to DB
    now = datetime.now(timezone.utc).astimezone()
    reply_entry = {
        "sender": agent_email,
        "recipient": to_addr,
        "content": content,
        "title": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        "timestamp": datetime.utcnow(),
        "message_type": "html",
        "channel": "email",
        "metadata": {
            "gmail_id": sent.get("id"),
            "from": agent_email,
            "to": to_addr,
            "date": format_datetime(now)
        }
    }

    await db["messages"].update_one(
        {"_id": ObjectId(id)},
        {
            "$push": {"messages": reply_entry},
            "$set": {"last_updated": reply_entry["timestamp"]}
        }
    )

    updated_message = await db["messages"].find_one({"_id": ObjectId(id)})

    if '_id' in updated_message:
        updated_message['_id'] = str(updated_message['_id'])
        updated_message['user_id'] = str(updated_message['user_id'])
        updated_message['company_id'] = str(updated_message['company_id'])
    return updated_message
