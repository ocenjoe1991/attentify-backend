from fastapi import APIRouter, Depends, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from app.models.user import UserCreate
from app.core.security import verify_password, get_password_hash, create_access_token, get_current_user
from app.db.mongodb import get_database
from app.utils.token_utils import verify_invitation_token
from bson import ObjectId
from authlib.integrations.starlette_client import OAuth
import os
from motor.motor_asyncio import AsyncIOMotorDatabase
from jose import JWTError, jwt
from app.utils.email_utils import send_reset_password_email
from app.models.auth import ForgotPasswordRequest, ResetPasswordRequest
from app.core.config import settings
from app.utils.rate_limit import auth_rate_limiter, sensitive_rate_limiter

router = APIRouter()

VALID_ROLES = {"admin", "store_owner", "agent", "readonly"}

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
GOOGLE_AUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_AUTH_REDIRECT_URI",
    f"{BACKEND_URL}/api/v1/auth/google/callback",
)

async def build_membership_login_payload(db, user: dict, user_id: str):
    memberships_cursor = db.memberships.find({
        "user_id": user["_id"],
        "status": "active"
    }).sort("last_used_at", -1)

    memberships = await memberships_cursor.to_list(length=None)

    if not memberships:
        invitation_result = await db.invitations.find_one({
            "email": user["email"],
            "status": "pending"
        })

        redirect_url = "/ask-accept-invitation" if invitation_result else "/register-company"
        token = create_access_token({
            "sub": user_id,
            "user_id": user_id,
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "companies": [],
            "redirect_url": redirect_url,
        })
        return {
            "token": token,
            "user": {
                "id": user_id,
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "companies": [],
            },
            "redirect_url": redirect_url,
        }
    
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
        "companies": company_list,
        "redirect_url": "/dashboard",
    })

    return {
        "token": token,
        "user": {
            "id": user_id,
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "company_id": str(company_id),
            "role": role,
            "companies": company_list,
        },
        "redirect_url": "/dashboard",
    }

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
    return await oauth.google.authorize_redirect(request, GOOGLE_AUTH_REDIRECT_URI)

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

        # No token, check pending invitation
        invitation_result = await db.invitations.find_one({
            "email": email,
            "status": "pending"
        })

        redirect_url = "/register-company"
        if invitation_result:
            redirect_url = "/ask-accept-invitation"

        jwt_token = create_access_token({
            "sub": user_id, 
            "user_id": user_id,
            "name": f"{first_name} {last_name}".strip(),
            "email": email,
            "redirect_url": redirect_url
        })

        response = RedirectResponse(url=f"{FRONTEND_URL}/oauth/callback/register")
        _set_auth_cookies(response, jwt_token)
        return response

    # If user exists, sign in
    await db["users"].update_one(
        {"email": user["email"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )

    user_id = str(user["_id"])

    if user.get("role") == "admin":
        jwt_token = create_access_token(data={
            "sub": user_id,
            "user_id": user_id,
            "name":  f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user["email"],
            "role": "admin"
        })

        response = RedirectResponse(url=f"{FRONTEND_URL}/oauth/callback/login")
        _set_auth_cookies(response, jwt_token)
        return response
    
    payload = await build_membership_login_payload(db, user, user_id)
    callback_path = "login" if payload.get("user", {}).get("company_id") else "register"
    response = RedirectResponse(url=f"{FRONTEND_URL}/oauth/callback/{callback_path}")
    _set_auth_cookies(response, payload["token"])
    return response


def _set_auth_cookies(response: Response, jwt_token: str) -> None:
    """Set JWT as httpOnly cookie and a status cookie for the frontend."""
    # Detect if running on localhost (development) vs production
    is_local = FRONTEND_URL.startswith("http://localhost") or FRONTEND_URL.startswith("http://127.0.0.1")
    cookie_kwargs = dict(
        httponly=True,
        secure=not is_local,
        samesite="lax" if is_local else "none",
        max_age=1800,  # 30 minutes
        path="/",
    )
    response.set_cookie(
        key="access_token",
        value=jwt_token,
        **cookie_kwargs,
    )
    response.set_cookie(
        key="auth_status",
        value="success",
        httponly=False,
        secure=not is_local,
        samesite="lax" if is_local else "none",
        max_age=60,  # 1 minute - just long enough for frontend to read
        path="/",
    )


# /api/v1/auth/me - returns current user from httpOnly cookie (used by OAuth callback flow)
@router.get("/me")
async def get_me(request: Request):
    db = request.app.state.db
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    user_id = str(user["_id"])
    redirect_url = payload.get("redirect_url", "/dashboard")
    role = payload.get("role", "")

    if role == "admin":
        return {
            "token": token,
            "user": {
                "id": user_id,
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": "admin",
                "companies": [],
            },
            "redirect_url": "/admin/dashboard",
        }

    return await build_membership_login_payload(db, user, user_id)

# /api/v1/auth/register
@router.post("/register")
async def register(
    user: UserCreate,
    db=Depends(get_database),
    _rate: None = Depends(auth_rate_limiter),
):
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
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db=Depends(get_database),
    _rate: None = Depends(auth_rate_limiter),
):
    user = await db.users.find_one({"email": form_data.username})

    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    # Google OAuth users who haven't set a password yet
    if user.get("auth_provider") == "google" and not user.get("hashed_password"):
        raise HTTPException(
            status_code=400,
            detail="This account uses Google Sign-In. Please log in with Google, or use 'Forgot Password' to set a password."
        )

    if not verify_password(form_data.password, user.get("hashed_password", "")):
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

        response = JSONResponse({
            "token": token,
            "user": {
                "id": user_id,
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email": user["email"],
                "role": "admin"
            }
        })
        _set_auth_cookies(response, token)
        return response
        
    payload = await build_membership_login_payload(db, user, user_id)
    response = JSONResponse(payload)
    _set_auth_cookies(response, payload["token"])
    return response


# /api/v1/auth/forgot-password
@router.post("/forgot-password")
async def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db=Depends(get_database),
    _rate: None = Depends(sensitive_rate_limiter),
):
    user = await db['users'].find_one({"email": request.email})

    # Always return the same response to prevent email enumeration
    if user:
        # Create token valid for 15 minutes (works for both regular and Google OAuth users)
        payload = {
            "sub": str(user["_id"]),
            "exp": datetime.utcnow() + timedelta(minutes=15)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        reset_link = f"{FRONTEND_URL}/reset-password?token={token}"
        background_tasks.add_task(send_reset_password_email, request.email, reset_link)

    return {"message": "If an account with that email exists, a reset link has been sent."}


# /api/v1/auth/reset-password
@router.post("/reset-password")
async def reset_password(
    request: ResetPasswordRequest,
    db=Depends(get_database),
    _rate: None = Depends(sensitive_rate_limiter),
):
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
