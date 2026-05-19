from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from app.models.user import UserCreate
from app.core.security import verify_password, get_password_hash, create_access_token
from app.db.mongodb import get_database
from app.utils.token_utils import verify_invitation_token
from bson import ObjectId
from authlib.integrations.starlette_client import OAuth
import os
from app.db.mongodb import get_database
from motor.motor_asyncio import AsyncIOMotorDatabase
from jose import JWTError, jwt
from app.utils.email_utils import send_reset_password_email
from app.models.auth import ForgotPasswordRequest, ResetPasswordRequest

router = APIRouter()

VALID_ROLES = {"admin", "store_owner", "agent", "readonly"}

SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")
ALGORITHM = "HS256"
FRONTEND_URL = os.getenv("FRONTEND_URL")

# --- OAuth Setup --
oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# /api/v1/auth/google/login
@router.get("/google/login")
async def google_login(request: Request):
    redirect_uri = request.url_for("google_callback")
    print(redirect_uri)
    return await oauth.google.authorize_redirect(request, redirect_uri)

# /api/v1/auth/google/callback
@router.get("/google/callback")
async def google_callback(request: Request, db: AsyncIOMotorDatabase = Depends(get_database)):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")

    if not user_info:
        raise HTTPException(status_code=400, detail="Google login failed")

    email = user_info["email"]
    first_name = user_info.get("given_name", "")
    last_name = user_info.get("family_name", "")

    user = await db["users"].find_one({"email": email})
    
    # If user doesn't exist, sign up
    if not user:
        now = datetime.utcnow()

        user_doc = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "created_at": now, 
            "updated_at": now,
            "last_login": now,
            "auth_provider": "google",
        }

        result = await db.users.insert_one(user_doc)
        user_id = str(result.inserted_id)

        # In invitation token is provided, handle it
        # if user.invitation token:

        # No token, check pending invitation
        invitation_result = await db.invitations.find_one({
            "email": email,
            "status": "pending"
        })

        redirect_url = "/register-company"
        if invitation_result:
            redirect_url = "/ask-accept-invitation"

        token = create_access_token({
            "sub": user_id, 
            "user_id": user_id,
            "name": f"{first_name} {last_name}".strip(),
            "email": email,
            "redirect_url": redirect_url
        })

        redirect_url = f"{FRONTEND_URL}/oauth/callback/register?token={token}"
        return RedirectResponse(url=redirect_url)

    # If user exists, sign in
    await db["users"].update_one(
        {"email": user["email"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )

    user_id = str(user["_id"])

    if user.get("role") == "admin":
        token = create_access_token(data={
            "sub": user_id,
            "user_id": user_id,
            "name":  f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "role": "admin"
        })

        return {
            "token": token,
            "user": {
                "id": user_id,  
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": "admin"
            }
        }
    
    memberships_cursor = db.memberships.find({
        "user_id": user["_id"],
        "status": "active"
    }).sort("last_used_at", -1)

    memberships = await memberships_cursor.to_list(length=None)

    if not memberships:
        raise HTTPException(status_code=403, detail="No active company membership found")
    
    selected_membership = memberships[0]
    company_id = selected_membership["company_id"]
    role = selected_membership.get("role", "readonly")

    await db.memberships.update_one(
        {"_id": selected_membership["_id"]},
        {"$set": {"last_used_at": datetime.utcnow()}}
    )

    company_ids = [m["company_id"] for m in memberships]
    companies_cursor = db.companies.find({"_id": {"$in": company_ids}})
    companies_map = {str(c["_id"]): c async for c in companies_cursor}

    company_list = []
    for m in memberships:
        cid = str(m["company_id"])
        company = companies_map.get(cid)
        if company:
            company_list.append({
                "id": cid,
                "name": company.get("name", "")
            })

    token = create_access_token(data={
        "sub": user_id,
        "user_id": user_id,
        "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
        "email": user["email"],
        "company_id": str(company_id),
        "role": role,
        "companies": company_list
    })

    redirect_url = f"{FRONTEND_URL}/oauth/callback/login?token={token}"
    return RedirectResponse(url=redirect_url)

# /api/v1/auth/register
@router.post("/register")
async def register(user: UserCreate, db=Depends(get_database)):
    existing = await db.users.find_one({"email": user.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = get_password_hash(user.password)
    now = datetime.utcnow()

    user_doc = {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "hashed_password": hashed_password,
        "created_at": now,
        "updated_at": now,
        "last_login": now,
    }

    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    # If invitation token is provided, handle it
    if user.invitation_token:
        try:
            data = verify_invitation_token(user.invitation_token)
            company_id = data["company_id"]
            role = data["role"]

            if role not in VALID_ROLES:
                raise HTTPException(status_code=400, detail="Invalid role in invitation token")

            # Add user to memberships
            await db.memberships.insert_one({
                "user_id": result.inserted_id,
                "company_id": ObjectId(company_id),
                "role": role,
                "status": "active",
                "joined_at": now,
                "last_used_at": now
            })

            await db.invitations.update_one(
                {"token": user.invitation_token},
                {"$set": {"status": "accepted"}}
            )

            company = await db.companies.find_one({"_id": ObjectId(company_id)})

            company_list = []
            if company:
                company_list.append({
                    "id": str(company["_id"]),
                    "name": company.get("name", "")
                })

            token = create_access_token(data={
                "sub": user_id,
                "user_id": user_id,
                "name": f"{user.first_name} {user.last_name}".strip(),
                "email": user.email,
                "company_id": str(company_id),
                "role": role,
                "companies": company_list,
                "redirect_url": "/dashboard"
            })

            return {
                "token": token,
                "user": {
                    "id": user_id,
                    "name": f"{user.first_name} {user.last_name}".strip(),
                    "email": user.email,
                    "company_id": str(company_id),
                    "role": role,
                    "companies": company_list
                },
                "redirect_url": "/dashboard"
            }

        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # No token, check pending invitation
    invitation_result = await db.invitations.find_one({
        "email": user.email,
        "status": "pending"
    })

    redirect_url = "/register-company"
    if invitation_result:
        redirect_url = "/ask-accept-invitation"

    token = create_access_token({
        "sub": user_id, 
        "user_id": user_id,
        "name": f"{user.first_name} {user.last_name}".strip(),
        "email": user.email,
        "redirect_url": redirect_url
    })

    return {
        "token": token,
        "user": {
            "id": user_id,
            "name": f"{user.first_name} {user.last_name}".strip(),
            "email": user.email,
        },
        "redirect_url": redirect_url
    }

# /api/v1/auth/login
@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db=Depends(get_database)):
    user = await db.users.find_one({"email": form_data.username})
    
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    
    if user.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended. Contact admin.")

    await db.users.update_one(
        {"email": user["email"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )

    user_id = str(user["_id"])  # Convert ObjectId to string

    if user.get("role") == "admin":
        token = create_access_token(data={
            "sub": user_id,
            "user_id": user_id,
            "name":  f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "role": "admin"
        })

        return {
            "token": token,
            "user": {
                "id": user_id,  
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": "admin"
            }
        }
        
    memberships_cursor = db.memberships.find({
        "user_id": user["_id"],
        "status": "active"
    }).sort("last_used_at", -1)

    memberships = await memberships_cursor.to_list(length=None)

    if not memberships:
        raise HTTPException(status_code=403, detail="No active company membership found")
    
    selected_membership = memberships[0]
    company_id = selected_membership["company_id"]
    role = selected_membership.get("role", "readonly")

    await db.memberships.update_one(
        {"_id": selected_membership["_id"]},
        {"$set": {"last_used_at": datetime.utcnow()}}
    )

    # === Fetch Company Info ===
    company_ids = [m["company_id"] for m in memberships]
    companies_cursor = db.companies.find({"_id": {"$in": company_ids}})
    companies_map = {str(c["_id"]): c async for c in companies_cursor}

    company_list = []
    for m in memberships:
        cid = str(m["company_id"])
        company = companies_map.get(cid)
        if company:
            company_list.append({
                "id": cid,
                "name": company.get("name", "")
            })
    
    token = create_access_token(data={
        "sub": user_id,
        "user_id": user_id,
        "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
        "email": user["email"],
        "company_id": str(company_id),
        "role": role,
        "companies": company_list
    })

    return {
        "token": token,
    }


# /api/v1/auth/forgot-password
@router.post("/forgot-password")
async def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db=Depends(get_database)
):
    user = await db['users'].find_one({"email": request.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # create token valid for 15 minutes
    payload = {
        "sub": str(user["_id"]),
        "exp": datetime.utcnow() + timedelta(minutes=15)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    reset_link = f"{FRONTEND_URL}/reset-password?token={token}"

    # Send in background
    background_tasks.add_task(send_reset_password_email, request.email, reset_link)

    return {"message": "Reset link sent if email exists"}


# /api/v1/auth/reset-password
@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest, db=Depends(get_database)):
    try:
        payload = jwt.decode(request.token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload["sub"]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    hashed_pw = get_password_hash(request.new_password)

    # if using Motor (async Mongo)
    await db["users"].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"hashed_password": hashed_pw}}
    )

    return {"message": "Password reset successful"}