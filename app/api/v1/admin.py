from datetime import datetime, timezone
from copy import deepcopy
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.utils.datetime_utils import to_utc_iso
from app.db.mongodb import get_database

router = APIRouter()

SETTINGS_KEY = "admin_governance"

DEFAULT_PERMISSIONS = {
    "company_owner": {
        "manage_members": True,
        "manage_stores": True,
        "manage_tickets": True,
        "process_refunds": True,
        "process_cancellations": True,
        "view_reports": True,
    },
    "store_owner": {
        "manage_members": False,
        "manage_stores": True,
        "manage_tickets": True,
        "process_refunds": True,
        "process_cancellations": True,
        "view_reports": True,
    },
    "agent": {
        "manage_members": False,
        "manage_stores": False,
        "manage_tickets": True,
        "process_refunds": False,
        "process_cancellations": False,
        "view_reports": False,
    },
    "readonly": {
        "manage_members": False,
        "manage_stores": False,
        "manage_tickets": False,
        "process_refunds": False,
        "process_cancellations": False,
        "view_reports": True,
    },
}

DEFAULT_APPROVALS = {
    "ticket_resolution_requires_owner": False,
    "refund_requires_owner": True,
    "cancellation_requires_owner": True,
    "high_value_refund_requires_owner": True,
    "high_value_refund_threshold": 100,
}

DEFAULT_NOTIFICATIONS = {
    "admin_new_user": True,
    "admin_store_added": True,
    "owner_approval_requested": True,
    "escalated_ticket": True,
    "assigned_ticket": True,
    "unresolved_reply": True,
    "comment_mentions": True,
    "email_digest": False,
}


class GovernanceSettingsPayload(BaseModel):
    permissions: Dict[str, Dict[str, bool]] = Field(default_factory=dict)
    approvals: Dict[str, Any] = Field(default_factory=dict)
    notifications: Dict[str, bool] = Field(default_factory=dict)


def ensure_admin(current_user: dict):
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


def serialize_doc(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    if "actor_id" in doc and isinstance(doc["actor_id"], ObjectId):
        doc["actor_id"] = str(doc["actor_id"])
    if "created_at" in doc:
        doc["created_at"] = to_utc_iso(doc.get("created_at"))
    if "updated_at" in doc:
        doc["updated_at"] = to_utc_iso(doc.get("updated_at"))
    return doc


def default_settings() -> dict:
    return {
        "key": SETTINGS_KEY,
        "permissions": deepcopy(DEFAULT_PERMISSIONS),
        "approvals": deepcopy(DEFAULT_APPROVALS),
        "notifications": deepcopy(DEFAULT_NOTIFICATIONS),
        "updated_at": None,
        "updated_by": None,
    }


def merge_with_defaults(settings: Optional[dict]) -> dict:
    merged = default_settings()
    if not settings:
        return merged

    for role, permissions in settings.get("permissions", {}).items():
        if role in merged["permissions"] and isinstance(permissions, dict):
            merged["permissions"][role].update(permissions)

    merged["approvals"].update(settings.get("approvals", {}))
    merged["notifications"].update(settings.get("notifications", {}))
    merged["updated_at"] = settings.get("updated_at")
    merged["updated_by"] = settings.get("updated_by")
    return merged


@router.get("/governance")
async def get_governance_settings(
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    ensure_admin(current_user)

    settings = await db["admin_settings"].find_one({"key": SETTINGS_KEY})
    settings = merge_with_defaults(settings)

    settings.pop("_id", None)
    return settings


@router.put("/governance")
async def update_governance_settings(
    payload: GovernanceSettingsPayload,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    ensure_admin(current_user)

    existing = await db["admin_settings"].find_one({"key": SETTINGS_KEY})
    merged = merge_with_defaults(existing)
    for role, permissions in payload.permissions.items():
        if role in merged["permissions"]:
            merged["permissions"][role].update(permissions)
    merged["approvals"].update(payload.approvals)
    merged["notifications"].update(payload.notifications)
    merged["updated_at"] = datetime.now(timezone.utc)
    merged["updated_by"] = str(current_user["_id"])

    await db["admin_settings"].update_one(
        {"key": SETTINGS_KEY},
        {"$set": merged},
        upsert=True,
    )

    await db["admin_notifications"].insert_one(
        {
            "type": "governance_settings_updated",
            "title": "Governance settings updated",
            "message": "Permissions, approval rules, or notification policies were changed.",
            "actor_id": current_user["_id"],
            "actor_email": current_user.get("email"),
            "created_at": datetime.now(timezone.utc),
            "read": False,
        }
    )

    merged.pop("_id", None)
    return merged


@router.get("/notifications", response_model=List[dict])
async def list_admin_notifications(
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    ensure_admin(current_user)

    cursor = db["admin_notifications"].find().sort("created_at", -1).limit(50)
    notifications = []
    async for doc in cursor:
        notifications.append(serialize_doc(doc))
    return notifications


@router.post("/notifications/{notification_id}/read")
async def mark_admin_notification_read(
    notification_id: str,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    ensure_admin(current_user)

    if not ObjectId.is_valid(notification_id):
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    result = await db["admin_notifications"].update_one(
        {"_id": ObjectId(notification_id)},
        {"$set": {"read": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"success": True}
