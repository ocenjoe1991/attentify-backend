from fastapi import APIRouter, Depends, HTTPException, status, Body, Query
from datetime import datetime, timezone
from app.models.company import CompanyCreate, SimpleCompanyOut, CompanyInDB, UpdateCompanyRequest
from app.models.user import UserPublic
from bson import ObjectId
from app.db.mongodb import get_database
from app.core.security import get_current_user
from typing import List
from app.core.security import create_access_token
from app.core.permissions import (
    ROLE_ADMIN,
    ROLE_COMPANY_OWNER,
    ROLE_PERMISSIONS,
    normalize_custom_permissions,
)
from app.core.audit import record_audit_log, serialize_audit_log
from app.utils.datetime_utils import to_utc_iso
from pymongo import ASCENDING, DESCENDING

router = APIRouter()

VALID_MESSAGE_DELETE_ROLES = {"company_owner", "store_owner", "agent", "readonly"}

def transform_company(company):
    return {
        "id": str(company["_id"]),
        **{k: v for k, v in company.items() if k != "_id"}
    }

async def get_active_membership(db, user_id, company_id):
    return await db["memberships"].find_one({
        "user_id": user_id,
        "company_id": company_id,
        "status": "active",
    })

async def ensure_member_admin(db, current_user, company_id):
    if current_user.get("role") == ROLE_ADMIN:
        return

    membership = await get_active_membership(db, current_user["_id"], company_id)
    if not membership or membership.get("role") != ROLE_COMPANY_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators or owners can manage members")

async def count_active_company_owners(db, company_id):
    return await db["memberships"].count_documents({
        "company_id": company_id,
        "role": ROLE_COMPANY_OWNER,
        "status": "active",
    })

def serialize_dashboard_message(message):
    return {
        "_id": str(message["_id"]),
        "title": message.get("title") or "Untitled",
        "client": message.get("client", ""),
        "status": message.get("status", ""),
        "ticket": message.get("ticket", ""),
        "order_match_status": message.get("order_match_status", "unknown"),
        "matched_order_name": message.get("matched_order_name", ""),
        "created_at": to_utc_iso(message.get("started_at") or message.get("created_at")),
        "last_updated": to_utc_iso(message.get("last_updated")),
    }

def serialize_dashboard_approval(request_doc):
    return {
        "_id": str(request_doc["_id"]),
        "type": request_doc.get("type", ""),
        "status": request_doc.get("status", ""),
        "requester_name": request_doc.get("requester_name", ""),
        "requester_email": request_doc.get("requester_email", ""),
        "created_at": to_utc_iso(request_doc.get("created_at")),
        "payload": request_doc.get("payload", {}),
    }

async def ensure_owner_change_allowed(db, current_user, member, new_role=None, deleting=False):
    if member.get("role") != ROLE_COMPANY_OWNER:
        return

    is_self = member.get("user_id") == current_user["_id"]
    if deleting and is_self:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own owner account")
    if not deleting and is_self and new_role != ROLE_COMPANY_OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot remove your own owner role")

    owner_count = await count_active_company_owners(db, member["company_id"])
    if owner_count <= 1 and (deleting or new_role != ROLE_COMPANY_OWNER):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one company owner must remain")

#GET /api/v1/company/
@router.get("/", response_model=List[SimpleCompanyOut])
async def list_companies(current_user: dict = Depends(get_current_user), db = Depends(get_database)):
    user_id = current_user["_id"]

    memberships_cursor = db["memberships"].find({
        "user_id": user_id,
        "status": "active"
    })

    company_ids = [m["company_id"] for m in await memberships_cursor.to_list(length=100)]

    if not company_ids:
        return []

    companies_cursor = db["companies"].find({
        "_id": {"$in": company_ids}
    })

    companies = await companies_cursor.to_list(length=100)

    # Convert _id to id
    return [transform_company(company) for company in companies]


# /api/v1/compnany/create
from bson import ObjectId

@router.post("/create")
async def create_company(company: CompanyCreate, current_user: dict = Depends(get_current_user), db=Depends(get_database)):
    now = datetime.now(timezone.utc)
    company_doc = {
        "name": company.name,
        "site_url": company.site_url,
        "email": company.email,
        "created_by": ObjectId(current_user["_id"]),
        "created_at": now,
    }
    
    result = await db.companies.insert_one(company_doc)
    company_id = result.inserted_id

    # Create membership for the current user
    membership_doc = {
        "user_id": ObjectId(current_user["_id"]),
        "company_id": company_id,
        "role": "company_owner",
        "status": "active",
        "custom_permissions": [],
        "joined_at": now,
        "last_used_at": now,
    }

    await db.memberships.insert_one(membership_doc)

    # Generate access token
    token = create_access_token(data={
        "sub": current_user["email"],
        "user_id": str(current_user["_id"]), 
        "company_id": str(company_id),
        "role": "company_owner"
    })

    company_list = [{
        "id": str(company_id),
        "name": company.name
    }]

    return {
        "token": token,
        "user": {
            "id": str(current_user["_id"]),
            "name": f"{current_user.get('first_name', '')} {current_user.get('last_name', '')}".strip(),
            "email": current_user.get("email", ""),
            "company_id": str(company_id),
            "role": "company_owner",
            "companies": company_list
        },
        "redirect_url": "/dashboard"
    }

#GET /api/v1/company/{company_id}
@router.get("/{company_id}", response_model=CompanyInDB)
async def get_company(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    company = await db["companies"].find_one({"_id": ObjectId(company_id)})

    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    membership = await get_active_membership(db, current_user["_id"], ObjectId(company_id))
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Convert ObjectId fields to str
    company["id"] = str(company["_id"])
    company["created_by"] = str(company["created_by"])
    company["current_user_custom_permissions"] = membership.get("custom_permissions", [])
    company["current_user_role"] = membership.get("role")

    return CompanyInDB.parse_obj(company)
    
#POST /api/v1/company/update-company
@router.post("/update-company")
async def update_company(
    payload: UpdateCompanyRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(payload.company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    membership = await get_active_membership(db, current_user["_id"], ObjectId(payload.company_id))
    if current_user.get("role") != ROLE_ADMIN and (not membership or membership.get("role") != ROLE_COMPANY_OWNER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only company owners can update company settings")

    # Build dynamic update fields
    update_data = {}
    if payload.name is not None:
        update_data["name"] = payload.name
    if payload.site_url is not None:
        update_data["site_url"] = payload.site_url
    if payload.email is not None:
        update_data["email"] = payload.email

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")

    updated_company = await db["companies"].find_one_and_update(
        {"_id": ObjectId(payload.company_id)},
        {"$set": update_data},
        return_document=True  # Returns updated document
    )

    if not updated_company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    await record_audit_log(
        db,
        company_id=ObjectId(payload.company_id),
        actor=current_user,
        actor_role=membership.get("role") if membership else ROLE_ADMIN,
        action="Updated company settings",
        entity_type="company",
        entity_id=ObjectId(payload.company_id),
        details={"fields": sorted(update_data.keys())},
    )

    return {
        "id": str(updated_company["_id"]),
        "name": updated_company.get("name"),
        "site_url": updated_company.get("site_url"),
        "email": updated_company.get("email")
    }

@router.post("/update-member")
async def update_company_member(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    member_id = payload.get("id")
    member_status = payload.get("status", "active")
    role = payload.get("role")
    custom_permissions = normalize_custom_permissions(payload.get("custom_permissions", []))

    if not member_id or not ObjectId.is_valid(member_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid member ID")
    if member_status not in {"active", "pending"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid member status")
    if role not in VALID_MESSAGE_DELETE_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    collection_name = "memberships" if member_status == "active" else "invitations"
    member = await db[collection_name].find_one({"_id": ObjectId(member_id)})
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    await ensure_member_admin(db, current_user, member["company_id"])
    if collection_name == "memberships":
        await ensure_owner_change_allowed(db, current_user, member, new_role=role)

    old_role = member.get("role")
    old_permissions = sorted(member.get("custom_permissions", []))
    target_email = member.get("email", "")
    if collection_name == "memberships":
        target_user = await db["users"].find_one({"_id": member.get("user_id")})
        target_email = target_user.get("email", "") if target_user else ""
    actor_membership = await get_active_membership(db, current_user["_id"], member["company_id"])

    result = await db[collection_name].update_one(
        {"_id": ObjectId(member_id)},
        {"$set": {"role": role, "custom_permissions": custom_permissions}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    new_permissions = sorted(custom_permissions)
    if old_role != role:
        await record_audit_log(
            db,
            company_id=member["company_id"],
            actor=current_user,
            actor_role=actor_membership.get("role") if actor_membership else ROLE_ADMIN,
            action="Changed member role",
            entity_type=collection_name[:-1],
            entity_id=ObjectId(member_id),
            details={"target_email": target_email, "old_role": old_role, "new_role": role},
        )
    if old_permissions != new_permissions:
        await record_audit_log(
            db,
            company_id=member["company_id"],
            actor=current_user,
            actor_role=actor_membership.get("role") if actor_membership else ROLE_ADMIN,
            action="Changed member permissions",
            entity_type=collection_name[:-1],
            entity_id=ObjectId(member_id),
            details={
                "target_email": target_email,
                "old_permissions": old_permissions,
                "new_permissions": new_permissions,
            },
        )

    return {
        "id": member_id,
        "role": role,
        "custom_permissions": custom_permissions,
    }

#GET /api/v1/company/{company_id}/members
@router.get("/{company_id}/members", response_model=dict)
async def list_company_members(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    membership = await get_active_membership(db, current_user["_id"], ObjectId(company_id))
    if current_user.get("role") != ROLE_ADMIN and not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    current_user_role = ROLE_ADMIN if current_user.get("role") == ROLE_ADMIN else membership.get("role")

    members_cursor = db["memberships"].find({
        "company_id": ObjectId(company_id),
        "status": "active"
    })

    memberships = []
    async for membership in members_cursor:
        user = await db["users"].find_one({"_id": membership["user_id"]})
        if user:
            memberships.append({
                "id": str(membership["_id"]),
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": membership["role"],
                "status": "active",
                "custom_permissions": membership.get("custom_permissions", []),
            })

    invitations_cursor = db["invitations"].find({
        "company_id": ObjectId(company_id),
        "status": "pending"
    })

    async for invitation in invitations_cursor:
        memberships.append({
            "id": str(invitation["_id"]),
            "email": invitation["email"],
            "role": invitation["role"],
            "status": "pending",
            "custom_permissions": invitation.get("custom_permissions", []),
        })

    return {
        "members": memberships,
        "current_user_role": current_user_role,
    }

@router.get("/{company_id}/role-permissions", response_model=dict)
async def get_role_permissions(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    membership = await get_active_membership(db, current_user["_id"], ObjectId(company_id))
    if current_user.get("role") != ROLE_ADMIN and not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return {
        role: sorted(list(permissions))
        for role, permissions in ROLE_PERMISSIONS.items()
    }

@router.get("/{company_id}/audit-logs", response_model=dict)
async def list_audit_logs(
    company_id: str,
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0),
    category: str = Query("all"),
    search: str = Query(""),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    membership = await get_active_membership(db, current_user["_id"], ObjectId(company_id))
    if current_user.get("role") != ROLE_ADMIN and not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    query = {"company_id": ObjectId(company_id)}
    category_map = {
        "tickets": ["ticket"],
        "orders": ["order"],
        "team": ["membership", "invitation"],
        "settings": ["company"],
        "integrations": ["shopify_cred", "gmail_account", "phone_account"],
    }
    if category != "all":
        if category not in category_map:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audit log category")
        query["entity_type"] = {"$in": category_map[category]}

    if search.strip():
        search_regex = {"$regex": search.strip(), "$options": "i"}
        query["$or"] = [
            {"actor_name": search_regex},
            {"actor_email": search_regex},
            {"action": search_regex},
            {"ticket": search_regex},
            {"customer": search_regex},
            {"details.target_email": search_regex},
            {"details.order_id": search_regex},
            {"details.shop": search_regex},
            {"details.email": search_regex},
            {"details.phone_number": search_regex},
        ]

    total = await db["audit_logs"].count_documents(query)
    cursor = (
        db["audit_logs"]
        .find(query)
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(limit)
    )
    logs = []
    async for doc in cursor:
        logs.append(serialize_audit_log(doc))
    return {"logs": logs, "total": total, "has_more": skip + len(logs) < total}

@router.get("/{company_id}/dashboard", response_model=dict)
async def get_company_dashboard(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    company_object_id = ObjectId(company_id)
    membership = await get_active_membership(db, current_user["_id"], company_object_id)
    if current_user.get("role") != ROLE_ADMIN and not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    base_message_query = {"company_id": company_object_id}
    open_statuses = ["Open", "Assigned", "In Progress", "Pending", "Escalated", "Awaiting Approval"]
    needs_review_filter = {
        "$or": [
            {"order_match_status": {"$exists": False}},
            {"order_match_status": {"$in": ["unknown", "possible"]}},
        ]
    }

    open_tickets = await db["messages"].count_documents({
        **base_message_query,
        "status": {"$in": open_statuses},
    })
    pending_tickets = await db["messages"].count_documents({
        **base_message_query,
        "status": {"$in": ["Pending", "Awaiting Approval"]},
    })
    resolved_tickets = await db["messages"].count_documents({
        **base_message_query,
        "status": "Resolved",
    })
    order_messages = await db["messages"].count_documents({
        **base_message_query,
        "order_match_status": "matched",
    })
    needs_review = await db["messages"].count_documents({
        **base_message_query,
        **needs_review_filter,
    })
    unmatched_orders = await db["messages"].count_documents({
        **base_message_query,
        "order_match_status": "unmatched",
    })
    approval_count = await db["approval_requests"].count_documents({
        "company_id": company_object_id,
        "status": "pending",
    })
    connected_gmail = await db["gmail_accounts"].count_documents({
        "company_id": company_object_id,
        "status": "connected",
    })
    connected_shopify = await db["shopify_cred"].count_documents({
        "company_id": company_object_id,
        "status": "connected",
    })

    recent_messages = []
    # Use same inbox filtering as message list: exclude trashed/archived and only active statuses
    inbox_query = {
        **base_message_query,
        "trashed": {"$ne": True},
        "archived": {"$ne": True},
        "status": {"$in": open_statuses},
    }
    message_cursor = (
        db["messages"]
        .find(inbox_query)
        .sort("started_at", DESCENDING)
        .limit(5)
    )
    async for message in message_cursor:
        recent_messages.append(serialize_dashboard_message(message))

    review_messages = []
    review_cursor = (
        db["messages"]
        .find({**base_message_query, **needs_review_filter})
        .sort("started_at", DESCENDING)
        .limit(5)
    )
    async for message in review_cursor:
        review_messages.append(serialize_dashboard_message(message))

    approvals = []
    my_approvals = []
    team_approvals = []
    approval_cursor = (
        db["approval_requests"]
        .find({"company_id": company_object_id, "status": "pending"})
        .sort("created_at", ASCENDING)
        .limit(5)
    )
    async for request_doc in approval_cursor:
        team_approvals.append(serialize_dashboard_approval(request_doc))

    can_process_approvals = (
        current_user.get("role") == ROLE_ADMIN
        or (membership and membership.get("role") == ROLE_COMPANY_OWNER)
    )
    if can_process_approvals:
        my_approvals = team_approvals
    else:
        my_approval_cursor = (
            db["approval_requests"]
            .find({"company_id": company_object_id, "status": "pending", "requested_by": current_user["_id"]})
            .sort("created_at", ASCENDING)
            .limit(5)
        )
        async for request_doc in my_approval_cursor:
            my_approvals.append(serialize_dashboard_approval(request_doc))

    recent_activity = []
    audit_cursor = (
        db["audit_logs"]
        .find({"company_id": company_object_id, "actor_id": current_user["_id"]})
        .sort("created_at", DESCENDING)
        .limit(6)
    )
    async for log in audit_cursor:
        recent_activity.append(serialize_audit_log(log))

    return {
        "summary": {
            "open_tickets": open_tickets,
            "pending_tickets": pending_tickets,
            "resolved_tickets": resolved_tickets,
            "awaiting_approval": approval_count,
            "order_messages": order_messages,
            "needs_review": needs_review,
            "unmatched_orders": unmatched_orders,
        },
        "connections": {
            "gmail_connected": connected_gmail,
            "shopify_connected": connected_shopify,
        },
        "recent_messages": recent_messages,
        "review_messages": review_messages,
        "my_pending_approvals": my_approvals,
        "team_pending_approvals": team_approvals,
        "recent_activity": recent_activity,
        "current_user_role": (membership or {}).get("role", current_user.get("role", "")),
    }

#GET /api/v1/company/{company_id}/active_members
@router.get("/{company_id}/active_members", response_model=List[dict])
async def active_members(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    members_cursor = db["memberships"].find({
        "company_id": ObjectId(company_id),
        "role": "agent",
        "status": "active"
    })

    members = []
    async for membership in members_cursor:
        user = await db["users"].find_one({"_id": membership["user_id"]})
        if user:
            members.append({
                "id": str(user["_id"]),
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": membership["role"],
                "status": "active"
            })
   
    return members

@router.delete("/delete-member")
async def delete_membership(
    payload: dict = Body(...),  # Expect a JSON body
    db=Depends(get_database),
    current_user=Depends(get_current_user)
):
    id = payload.get("id")
    if not id or not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid membership ID")

    status = payload.get("status", "active")
    if status not in ["active", "pending"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    result = None
    now = datetime.now(timezone.utc)
    if status == "active":
        membership = await db.memberships.find_one({"_id": ObjectId(id)})

        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")
        await ensure_member_admin(db, current_user, membership["company_id"])
        await ensure_owner_change_allowed(db, current_user, membership, deleting=True)

        result = await db.memberships.update_one(
            {"_id": ObjectId(id)},
            {
                "$set": {
                    "status": "removed",
                    "removed_at": now,
                    "removed_by": current_user["_id"],
                    "updated_at": now,
                }
            },
        )

        # Keep invitation history, but make sure pending invites for this member are no longer usable.
        company_id = membership.get("company_id")
        deleted_user_id = membership.get("user_id")
        deleted_user = await db.users.find_one({"_id": deleted_user_id})
        deleted_user_email = deleted_user.get("email") if deleted_user else "Unknown"

        await db["invitations"].update_many({
            "email": deleted_user_email,
            "company_id": company_id,
            "status": {"$in": ["pending", "accepted"]},
        }, {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancelled_by": current_user["_id"],
            }
        })
        actor_membership = await get_active_membership(db, current_user["_id"], company_id)
        await record_audit_log(
            db,
            company_id=company_id,
            actor=current_user,
            actor_role=actor_membership.get("role") if actor_membership else ROLE_ADMIN,
            action="Removed team member",
            entity_type="membership",
            entity_id=ObjectId(id),
            details={"target_email": deleted_user_email, "old_role": membership.get("role")},
        )

    elif status == "pending":
        invitation = await db.invitations.find_one({"_id": ObjectId(id)})

        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")
        await ensure_member_admin(db, current_user, invitation["company_id"])

        result = await db.invitations.update_one(
            {"_id": ObjectId(id)},
            {
                "$set": {
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": current_user["_id"],
                }
            },
        )
        actor_membership = await get_active_membership(db, current_user["_id"], invitation["company_id"])
        await record_audit_log(
            db,
            company_id=invitation["company_id"],
            actor=current_user,
            actor_role=actor_membership.get("role") if actor_membership else ROLE_ADMIN,
            action="Cancelled team invitation",
            entity_type="invitation",
            entity_id=ObjectId(id),
            details={"target_email": invitation.get("email"), "old_role": invitation.get("role")},
        )
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to delete membership")

    return {"success": True, "message": "Member removed"}
