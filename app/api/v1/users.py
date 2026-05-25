from fastapi import APIRouter, Depends, HTTPException
from typing import List
from pymongo.collection import Collection
from app.models.user import AdminUserCreate, AdminUserUpdate, UserPublic
from app.db.mongodb import get_database
from datetime import datetime
from bson import ObjectId
from app.utils.bson import PyObjectId  # helper to handle ObjectId correctly
from passlib.context import CryptContext

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# -------------------
# GET /users - List Users
# -------------------
@router.get("/", response_model=List[UserPublic])
async def list_users(db: Collection = Depends(get_database)):
    cursor = db["users"].find()
    users = []
    async for user in cursor:
        users.append(user)

    user_ids = [user["_id"] for user in users]
    memberships_by_user = {}
    company_ids = set()

    if user_ids:
        memberships_cursor = db["memberships"].find(
            {"user_id": {"$in": user_ids}}
        ).sort("last_used_at", -1)

        async for membership in memberships_cursor:
            user_id = membership["user_id"]
            if user_id not in memberships_by_user:
                memberships_by_user[user_id] = membership
                company_ids.add(membership["company_id"])

    companies_by_id = {}
    if company_ids:
        companies_cursor = db["companies"].find({"_id": {"$in": list(company_ids)}})
        async for company in companies_cursor:
            companies_by_id[company["_id"]] = company

    for user in users:
        membership = memberships_by_user.get(user["_id"])

        if membership:
            company = companies_by_id.get(membership["company_id"])
            user["membership_id"] = str(membership["_id"])
            user["company_id"] = str(membership["company_id"])
            user["team_name"] = company.get("name") if company else None
            if user.get("role") != "admin":
                user["role"] = membership.get("role", user.get("role"))
            user["status"] = membership.get("status", user.get("status"))
        elif user.get("role") == "admin" and not user.get("status"):
            user["status"] = "active"

        user["_id"] = str(user["_id"])

    return users


# -------------------
# POST /users - Create New User (identity only)
# -------------------
@router.post("/", response_model=UserPublic)
async def create_user(user: AdminUserCreate, db: Collection = Depends(get_database)):
    existing_user = await db["users"].find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already exists")

    now = datetime.utcnow()
    hashed_pw = pwd_context.hash(user.password or "changeme")

    user_doc = {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role or "readonly",
        "status": user.status or "invited",
        "team_id": user.team_id,
        "hashed_password": hashed_pw,
        "created_at": now,
        "updated_at": now,
        "last_login": None
    }

    result = await db["users"].insert_one(user_doc)
    created_user = await db["users"].find_one({"_id": result.inserted_id})
    created_user["_id"] = str(created_user["_id"])
    return created_user


# -------------------
# PUT /users/{user_id} - Update User
# -------------------
@router.put("/{user_id}", response_model=UserPublic)
async def update_user(user_id: str, user: AdminUserUpdate, db: Collection = Depends(get_database)):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    existing = await db["users"].find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = {"updated_at": datetime.utcnow()}

    for field in ["email", "first_name", "last_name", "role", "status", "team_id"]:
        value = getattr(user, field)
        if value is not None:
            update_data[field] = value

    if user.password:
        update_data["hashed_password"] = pwd_context.hash(user.password)

    await db["users"].update_one({"_id": oid}, {"$set": update_data})

    membership_update = {}
    if user.role is not None and user.role != "admin":
        membership_update["role"] = user.role
    if user.status is not None:
        membership_update["status"] = user.status

    if membership_update:
        latest_membership = await db["memberships"].find_one(
            {"user_id": oid},
            sort=[("last_used_at", -1)]
        )
        if latest_membership:
            await db["memberships"].update_one(
                {"_id": latest_membership["_id"]},
                {"$set": membership_update}
            )

    updated_user = await db["users"].find_one({"_id": oid})
    latest_membership = await db["memberships"].find_one(
        {"user_id": oid},
        sort=[("last_used_at", -1)]
    )
    if latest_membership:
        company = await db["companies"].find_one({"_id": latest_membership["company_id"]})
        updated_user["membership_id"] = str(latest_membership["_id"])
        updated_user["company_id"] = str(latest_membership["company_id"])
        updated_user["team_name"] = company.get("name") if company else None
        if updated_user.get("role") != "admin":
            updated_user["role"] = latest_membership.get("role", updated_user.get("role"))
        updated_user["status"] = latest_membership.get("status", updated_user.get("status"))
    elif updated_user.get("role") == "admin" and not updated_user.get("status"):
        updated_user["status"] = "active"

    updated_user["_id"] = str(updated_user["_id"])
    return updated_user


# -------------------
# DELETE /users/{user_id}
# -------------------
@router.delete("/{user_id}")
async def delete_user(user_id: str, db: Collection = Depends(get_database)):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    existing = await db["users"].find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    await db["users"].delete_one({"_id": oid})
    return {"message": "User deleted"}
