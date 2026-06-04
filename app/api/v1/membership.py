from fastapi import APIRouter, Depends, HTTPException, status
from app.models.membership import UpdateMembershipRequest
from bson import ObjectId
from app.db.mongodb import get_database
from app.core.security import get_current_user

router = APIRouter()

VALID_CUSTOM_PERMISSIONS = {"permanent_delete_ticket"}

async def ensure_membership_admin(db, current_user, target_membership):
    if current_user.get("role") == "admin":
        return

    admin_membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": target_membership["company_id"],
        "role": "company_owner",
        "status": "active",
    })
    if not admin_membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators or owners can update memberships")

def normalize_custom_permissions(permissions):
    if permissions is None:
        return None
    cleaned_permissions = []
    for permission in permissions:
        if permission not in VALID_CUSTOM_PERMISSIONS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid custom permission")
        if permission not in cleaned_permissions:
            cleaned_permissions.append(permission)
    return cleaned_permissions

#POST /api/v1/membership/update
@router.post("/update")
async def update_membership(
    payload: UpdateMembershipRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(payload.membership_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid membership ID")

    target_membership = await db["memberships"].find_one({"_id": ObjectId(payload.membership_id)})
    if not target_membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")

    await ensure_membership_admin(db, current_user, target_membership)

    # Build dynamic update fields
    update_data = {}
    if payload.role is not None:
        update_data["role"] = payload.role
    if payload.status is not None:
        update_data["status"] = payload.status
    normalized_permissions = normalize_custom_permissions(payload.custom_permissions)
    if normalized_permissions is not None:
        update_data["custom_permissions"] = normalized_permissions

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")

    update_membership = await db["memberships"].find_one_and_update(
        {"_id": ObjectId(payload.membership_id)},
        {"$set": update_data},
        return_document=True  # Returns updated document
    )

    return {
        "id": str(update_membership["_id"]),
        "role": update_membership.get("role"),
        "status": update_membership.get("status"),
        "custom_permissions": update_membership.get("custom_permissions", []),
    }
