# routers/invitations.py
from fastapi import APIRouter, Depends, HTTPException, Request, status
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from app.db.mongodb import get_database
from app.models.invitation import InvitationBase, InvitationDetails, AcceptInvitationRequest
from app.utils.token_utils import create_invitation_token, verify_invitation_token
from app.utils.email_utils import send_invitation_email
from jose import jwt, JWTError
from app.core.config import settings
from fastapi.responses import RedirectResponse
from app.core.security import get_current_user, create_access_token
from app.core.permissions import ROLE_ADMIN, ROLE_COMPANY_OWNER, normalize_custom_permissions
from app.core.audit import record_audit_log

router = APIRouter()

async def get_authenticated_user_from_request(request: Request, db):
    auth_header = request.headers.get("authorization", "")
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        return None

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("user_id")
        if not user_id:
            return None
        return await db["users"].find_one({"_id": ObjectId(user_id)})
    except Exception:
        return None

# POST /api/v1/invitations/send
@router.post("/send")
async def send_invitation(invite: InvitationBase, db=Depends(get_database), current_user=Depends(get_current_user)):
    if not ObjectId.is_valid(str(invite.company_id)):
        raise HTTPException(status_code=400, detail="Invalid company ID")

    company_id = ObjectId(invite.company_id)
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": company_id,
        "status": "active",
    })
    if current_user.get("role") != ROLE_ADMIN and (not membership or membership.get("role") != ROLE_COMPANY_OWNER):
        raise HTTPException(status_code=403, detail="Only administrators or owners can send invitations")

    custom_permissions = normalize_custom_permissions(invite.custom_permissions)
    now = datetime.now(timezone.utc)

    existing_user = await db["users"].find_one({"email": invite.email})
    if existing_user:
        existing_membership = await db["memberships"].find_one({
            "user_id": existing_user["_id"],
            "company_id": company_id,
        })
        if existing_membership and existing_membership.get("status") == "active":
            return {"message": "This user is already a team member."}

    # Check if invitation already exists
    existing_invite = await db["invitations"].find_one(
        {"email": invite.email, "company_id": company_id}
    )

    # If already accepted, stop here
    if existing_invite and existing_invite.get("status") == "accepted":
        return {"message": "This user has already accepted the invitation."}

    token = create_invitation_token(invite.email, str(invite.company_id), invite.role)
    invite_link = f"{settings.FRONTEND_URL}/accept-invite?token={token}"

    # Update if exists (pending or expired), otherwise create new
    result = await db["invitations"].update_one(
        {"email": invite.email, "company_id": company_id},
        {
            "$set": {
                "role": invite.role,
                "custom_permissions": custom_permissions,
                "token": token,
                "invited_at": now,
                "status": "pending"
            }
        },
        upsert=True
    )

    await send_invitation_email(invite.email, invite_link)

    invitation_doc = await db["invitations"].find_one({"email": invite.email, "company_id": company_id})
    await record_audit_log(
        db,
        company_id=company_id,
        actor=current_user,
        actor_role=membership.get("role") if membership else ROLE_ADMIN,
        action="Invited team member" if result.matched_count == 0 else "Updated team invitation",
        entity_type="invitation",
        entity_id=invitation_doc["_id"] if invitation_doc else None,
        details={"target_email": invite.email, "role": invite.role, "custom_permissions": custom_permissions},
    )

    if result.matched_count > 0:
        return {"message": "Invitation updated successfully."}
    else:
        return {"message": "Invitation created successfully."}

@router.post("/accept-invitation-token")
async def accept_invitation_token(
    payload: AcceptInvitationRequest,
    request: Request,
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

    authenticated_user = await get_authenticated_user_from_request(request, db)
    if authenticated_user and authenticated_user.get("email") != email:
        raise HTTPException(status_code=403, detail="This invitation belongs to a different email address")

    now = datetime.now(timezone.utc)
    existing_membership = await db["memberships"].find_one({
        "user_id": user["_id"],
        "company_id": ObjectId(company_id),
    })
    if existing_membership:
        await db["memberships"].update_one(
            {"_id": existing_membership["_id"]},
            {
                "$set": {
                    "role": invitation["role"],
                    "status": "active",
                    "custom_permissions": invitation.get("custom_permissions", []),
                    "rejoined_at": now,
                    "last_used_at": now,
                    "updated_at": now,
                },
                "$unset": {
                    "removed_at": "",
                    "removed_by": "",
                },
            },
        )
    else:
        await db["memberships"].insert_one({
            "user_id": user["_id"],
            "company_id": ObjectId(company_id),
            "role": invitation["role"],
            "status": "active",
            "custom_permissions": invitation.get("custom_permissions", []),
            "joined_at": now,
            "last_used_at": now,
        })

    # Mark invitation as accepted
    await db["invitations"].update_one(
        {"_id": invitation["_id"]},
        {"$set": {"status": "accepted"}}
    )

    if authenticated_user:
        company = await db.companies.find_one({"_id": ObjectId(company_id)})
        company_list = []
        if company:
            company_list.append({
                "id": str(company["_id"]),
                "name": company.get("name", "")
            })

        token = create_access_token(data={
            "sub": user["email"],
            "user_id": str(user["_id"]),
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "company_id": str(company_id),
            "role": invitation["role"],
            "companies": company_list,
            "redirect_url": "/dashboard",
        })

        return {
            "token": token,
            "user": {
                "id": str(user["_id"]),
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "company_id": str(company_id),
                "role": invitation["role"],
                "companies": company_list,
            },
            "redirect_url": "/dashboard",
        }

    return {"redirect_url": f"/login"}

# GET endpoint
@router.get("/invitation-status/{token}", response_model=InvitationDetails)
def get_invitation(token: str):
    payload = verify_invitation_token(token)

    return InvitationDetails(
        email=payload["email"],
        company_id=payload["company_id"],
        role=payload["role"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=172800)
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
    now = datetime.now(timezone.utc)
    invitation = await db.invitations.find_one({
        "email": current_user["email"],
        "status": "pending"
    })

    if not invitation:
        raise HTTPException(status_code=404, detail="No pending invitation found")

    existing_membership = await db.memberships.find_one({
        "user_id": ObjectId(current_user["_id"]),
        "company_id": invitation["company_id"],
    })
    if existing_membership:
        await db.memberships.update_one(
            {"_id": existing_membership["_id"]},
            {
                "$set": {
                    "role": invitation["role"],
                    "status": "active",
                    "custom_permissions": invitation.get("custom_permissions", []),
                    "rejoined_at": now,
                    "last_used_at": now,
                    "updated_at": now,
                },
                "$unset": {
                    "removed_at": "",
                    "removed_by": "",
                },
            },
        )
    else:
        await db.memberships.insert_one({
            "user_id": ObjectId(current_user["_id"]),
            "company_id": invitation["company_id"],
            "role": invitation["role"],
            "status": "active",
            "custom_permissions": invitation.get("custom_permissions", []),
            "joined_at": now,
            "last_used_at": now,
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
            "id": str(current_user["_id"]),
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
