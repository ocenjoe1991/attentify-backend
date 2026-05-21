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
        user["_id"] = str(user["_id"])
        users.append(user)
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
    updated_user = await db["users"].find_one({"_id": oid})
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
