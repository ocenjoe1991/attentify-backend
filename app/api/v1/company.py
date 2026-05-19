from fastapi import APIRouter, Depends, HTTPException, status, Body
from datetime import datetime
from app.models.company import CompanyCreate, SimpleCompanyOut, CompanyInDB, UpdateCompanyRequest
from app.models.user import UserPublic
from bson import ObjectId
from app.db.mongodb import get_database
from app.core.security import get_current_user
from typing import List
from app.core.security import create_access_token

router = APIRouter()

def transform_company(company):
    return {
        "id": str(company["_id"]),
        **{k: v for k, v in company.items() if k != "_id"}
    }

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
    now = datetime.utcnow()
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

    # Optional: check if current_user has access rights to this company
    # if company["created_by"] != current_user["_id"]:
    #     raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Convert ObjectId fields to str
    company["id"] = str(company["_id"])
    company["created_by"] = str(company["created_by"])

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

    return {
        "id": str(updated_company["_id"]),
        "name": updated_company.get("name"),
        "site_url": updated_company.get("site_url"),
        "email": updated_company.get("email")
    }

#GET /api/v1/company/{company_id}/members
@router.get("/{company_id}/members", response_model=List[dict])
async def list_company_members(
    company_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

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
                "status": "active"
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
            "status": "pending"
        })

    if not memberships:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active members found for this company")
    return memberships

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
   
    if not members:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active members found for this company")
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
    if status == "active":
        membership = await db.memberships.find_one({"_id": ObjectId(id)})

        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")

        result = await db.memberships.delete_one({"_id": ObjectId(id)})

        # Remove associated invitations
        company_id = membership.get("company_id")
        deleted_user_id = membership.get("user_id")
        deleted_user = await db.users.find_one({"_id": deleted_user_id})
        deleted_user_email = deleted_user.get("email") if deleted_user else "Unknown"

        await db["invitations"].delete_many({
            "email": deleted_user_email,
            "company_id": company_id
        })

        deleted_user_membership = await db.memberships.find_one({"user_id": deleted_user_id})
        if not deleted_user_membership:
            await db.users.delete_one({"_id": deleted_user_id})

    elif status == "pending":
        invitation = await db.invitations.find_one({"_id": ObjectId(id)})

        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")

        result = await db.invitations.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=500, detail="Failed to delete membership")

    return {"success": True, "message": "Membership deleted"}