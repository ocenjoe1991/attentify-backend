# routers/invitations.py
from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timedelta
from bson import ObjectId
from app.db.mongodb import get_database
from app.models.invitation import InvitationBase, InvitationDetails, AcceptInvitationRequest
from app.utils.token_utils import create_invitation_token, verify_invitation_token
from app.utils.email_utils import send_invitation_email
from jose import jwt, JWTError
from app.core.config import settings
from fastapi.responses import RedirectResponse
from app.core.security import get_current_user, create_access_token

router = APIRouter()

# POST /api/v1/invitations/send
@router.post("/send")
async def send_invitation(invite: InvitationBase, db=Depends(get_database)):
    if not ObjectId.is_valid(str(invite.company_id)):
        raise HTTPException(status_code=400, detail="Invalid company ID")

    # Check if invitation already exists
    existing_invite = await db["invitations"].find_one(
        {"email": invite.email, "company_id": ObjectId(invite.company_id)}
    )

    # If already accepted, stop here
    if existing_invite and existing_invite.get("status") == "accepted":
        return {"message": "This user has already accepted the invitation."}

    token = create_invitation_token(invite.email, str(invite.company_id), invite.role)
    invite_link = f"{settings.FRONTEND_URL}/accept-invite?token={token}"

    # Update if exists (pending or expired), otherwise create new
    result = await db["invitations"].update_one(
        {"email": invite.email, "company_id": ObjectId(invite.company_id)},
        {
            "$set": {
                "role": invite.role,
                "token": token,
                "invited_at": datetime.utcnow(),
                "status": "pending"
            }
        },
        upsert=True
    )

    await send_invitation_email(invite.email, invite_link)

    if result.matched_count > 0:
        return {"message": "Invitation updated successfully."}
    else:
        return {"message": "Invitation created successfully."}

@router.post("/accept-invitation-token")
async def accept_invitation_token(
    payload: AcceptInvitationRequest,
    db=Depends(get_database)
):
    try:
        data = verify_invitation_token(payload.token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired invitation token")

    email = data["email"]
    company_id = data["company_id"]

    invitation = await db["invitations"].find_one({"token": payload.token, "status": "pending"})
    if not invitation:
        raise HTTPException(status_code=400, detail="Invitation already used or invalid")

    # Check if user exists
    user = await db["users"].find_one({"email": email})
    if not user:
        # Frontend can redirect to signup page if user doesn't exist
        return {"redirect_url": f"/signup?token={payload.token}"}

    # Add user to memberships
    await db["memberships"].insert_one({
        "user_id": user["_id"],
        "company_id": ObjectId(company_id),
        "role": invitation["role"],
        "status": "active",
        "joined_at": datetime.utcnow(),
        "last_used_at": datetime.utcnow()
    })

    # Mark invitation as accepted
    await db["invitations"].update_one(
        {"_id": invitation["_id"]},
        {"$set": {"status": "accepted"}}
    )

    return {"redirect_url": f"/login"}

# GET endpoint
@router.get("/invitation-status/{token}", response_model=InvitationDetails)
def get_invitation(token: str):
    payload = verify_invitation_token(token)

    return InvitationDetails(
        email=payload["email"],
        company_id=payload["company_id"],
        role=payload["role"],
        expires_at=datetime.utcnow() + timedelta(seconds=172800)  # optional
    )

@router.get("/invitation-status")
async def get_invitation_status(db=Depends(get_database), current_user=Depends(get_current_user)):
    """Returns company & role info for pending invitation."""
    invitation = await db.invitations.find_one({
        "email": current_user["email"],
        "status": "pending"
    })

    if not invitation:
        raise HTTPException(status_code=404, detail="No pending invitation found")

    company = await db.companies.find_one({"_id": invitation["company_id"]})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return {
        "company_id": str(company["_id"]),
        "company_name": company.get("name", ""),
        "role": invitation["role"]
    }

@router.post("/invitation-accept")
async def accept_invitation(db=Depends(get_database), current_user=Depends(get_current_user)):
    """Accepts the pending invitation."""
    now = datetime.utcnow()
    invitation = await db.invitations.find_one({
        "email": current_user["email"],
        "status": "pending"
    })

    if not invitation:
        raise HTTPException(status_code=404, detail="No pending invitation found")

    # Add to memberships
    await db.memberships.insert_one({
        "user_id": ObjectId(current_user["_id"]),
        "company_id": invitation["company_id"],
        "role": invitation["role"],
        "status": "active",
        "joined_at": now,
        "last_used_at": now
    })

    # Mark invitation as accepted
    await db.invitations.update_one(
        {"_id": invitation["_id"]},
        {"$set": {"status": "accepted"}}
    )

    token = create_access_token(data={
        "sub": current_user["email"],
        "user_id": str(current_user["_id"]),
        "company_id": str(invitation["company_id"]),
        "role": invitation["role"]
    })

    company = await db.companies.find_one({"_id": invitation["company_id"]})
    company_list = []
    if company:
        company_list.append({
            "id": str(company["_id"]),
            "name": company.get("name", "")
        })

    return {
        "token": token,
        "user": {
            "id": str(current_user["_id"]),  # ✅ FIXED: convert ObjectId to str
            "name": f"{current_user['first_name']} {current_user['last_name']}".strip(),
            "email": current_user["email"],
            "company_id": str(invitation["company_id"]),
            "role": invitation["role"],
            "companies": company_list
        },
        "redirect_url": "/dashboard"
    }


@router.post("/invitation-cancel")
async def cancel_invitation(db=Depends(get_database), current_user=Depends(get_current_user)):
    """Cancels the pending invitation."""
    invitation = await db.invitations.find_one({
        "email": current_user["email"],
        "status": "pending"
    })

    if not invitation:
        raise HTTPException(status_code=404, detail="No pending invitation found")

    await db.invitations.update_one(
        {"_id": invitation["_id"]},
        {"$set": {"status": "cancelled"}}
    )

    return {"message": "Invitation cancelled"}