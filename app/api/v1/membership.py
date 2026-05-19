from fastapi import APIRouter, Depends, HTTPException, status
from app.models.membership import UpdateMembershipRequest
from bson import ObjectId
from app.db.mongodb import get_database
from app.core.security import get_current_user

router = APIRouter()

#POST /api/v1/membership/update
@router.post("/update")
async def update_membership(
    payload: UpdateMembershipRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(payload.membership_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid membership ID")

    # Build dynamic update fields
    update_data = {}
    if payload.role is not None:
        update_data["role"] = payload.role
    if payload.status is not None:
        update_data["status"] = payload.status

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")

    update_membership = await db["memberships"].find_one_and_update(
        {"_id": ObjectId(payload.membership_id)},
        {"$set": update_data},
        return_document=True  # Returns updated document
    )

    if not update_membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")

    return {
        "id": str(update_membership["_id"]),
        "role": update_membership.get("role"),
        "status": update_membership.get("status")
    }
