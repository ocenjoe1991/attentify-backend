#app/main.py
from fastapi import FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
import os
import certifi
from dotenv import load_dotenv
load_dotenv()  # Load from .env at startup
from app.db.mongodb import get_database
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from app.core.config import settings
import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("attentify")

import socketio
from socketio.exceptions import ConnectionRefusedError
from bson import ObjectId
from google.cloud import pubsub_v1
from google.oauth2 import service_account

origins = os.getenv("ORIGINS", "http://localhost:5173").split(",")

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[origin.strip() for origin in origins]
)

# CORS origins
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017").strip()
DB_NAME = os.getenv("DB_NAME", "attentify").strip()
from starlette.middleware.sessions import SessionMiddleware

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

def set_gmail_watch(cred):
    access_token = cred["access_token"]
    refresh_token = cred["refresh_token"]
            
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )

    gmail = build("gmail", "v1", credentials=creds)
    watch_request = {
        "labelIds": ["INBOX"],
        "topicName": f"projects/{settings.PUBSUB_PROJECT}/topics/{settings.PUBSUB_TOPIC}",
    }
    return gmail.users().watch(userId="me", body=watch_request).execute()

def ensure_pubsub_subscription():
    service_account_info = json.loads(settings.SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/pubsub"],
    )

    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    topic_path = subscriber.topic_path(settings.PUBSUB_PROJECT, settings.PUBSUB_TOPIC)
    subscription_path = subscriber.subscription_path(settings.PUBSUB_PROJECT, settings.PUBSUB_SUBSCRIPTION)
    push_endpoint = f"{settings.BACKEND_URL}/api/v1/gmail/pubsub/push"

    try:
        subscription = subscriber.get_subscription(request={"subscription": subscription_path})
        if subscription.push_config.push_endpoint != push_endpoint:
            subscriber.modify_push_config(
                request={
                    "subscription": subscription_path,
                    "push_config": {"push_endpoint": push_endpoint},
                }
            )
    except Exception:
        subscriber.create_subscription(
            request={
                "name": subscription_path,
                "topic": topic_path,
                "push_config": {"push_endpoint": push_endpoint},
            }
        )

    return subscription_path

async def set_gmail_watches_periodically():
    """Periodically renew Gmail watch subscriptions with retry logic."""
    import logging
    logger = logging.getLogger("gmail_watch")

    retry_delay = 300  # 5 minutes initial retry if PubSub setup fails
    max_retry_delay = 3600  # max 1 hour between retries

    while True:
        logger.info("Setting up Gmail Watches...")
        pubsub_ok = False

        # Retry Pub/Sub subscription setup with exponential backoff
        for attempt in range(3):
            try:
                loop = asyncio.get_running_loop()
                subscription_path = await loop.run_in_executor(None, ensure_pubsub_subscription)
                logger.info(f"Pub/Sub subscription ready: {subscription_path}")
                pubsub_ok = True
                retry_delay = 300  # reset retry delay on success
                break
            except Exception as e:
                logger.error(f"Pub/Sub setup attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(min(retry_delay * (2 ** attempt), max_retry_delay))

        if not pubsub_ok:
            logger.error("Pub/Sub subscription failed after 3 attempts, will retry in %s seconds", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)
            continue

        # Set watches for all Gmail accounts
        db = app.state.db
        success_count = 0
        fail_count = 0
        cursor = db["gmail_accounts"].find()
        async for cred in cursor:
            try:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(None, set_gmail_watch, cred)
                update_data = {
                    "status": "connected",
                    "watch_expiration": response.get("expiration"),
                    "last_watch_renewed_at": datetime.now(timezone.utc),
                }
                if not cred.get("history_id") and response.get("historyId"):
                    update_data["history_id"] = response["historyId"]
                await db["gmail_accounts"].update_one(
                    {"_id": cred["_id"]},
                    {"$set": update_data, "$unset": {"last_error": ""}},
                )
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to set Gmail watch for {cred.get('email')}: {e}")
                update_data = {
                    "last_error": f"Failed to renew Gmail watch: {e}",
                    "last_error_at": datetime.now(timezone.utc),
                }
                if isinstance(e, RefreshError) or "invalid_grant" in str(e):
                    update_data["status"] = "disconnected"
                    update_data["last_error"] = (
                        "Google refresh token is invalid or revoked. Reconnect this Gmail account."
                    )
                await db["gmail_accounts"].update_one(
                    {"_id": cred["_id"]},
                    {"$set": update_data, "$unset": {"watch_expiration": ""}},
                )
                fail_count += 1

        logger.info("Gmail watch renewal complete: %d succeeded, %d failed", success_count, fail_count)

        # If all watches failed, retry sooner; otherwise wait 24 hours
        sleep_duration = 3600 if fail_count > 0 and success_count == 0 else 24 * 3600
        await asyncio.sleep(sleep_duration)

async def ensure_database_indexes(db):
    # Orders
    await db["orders"].create_index([("company_id", 1), ("created_at", -1)])
    await db["orders"].create_index([("company_id", 1), ("shop", 1), ("created_at", -1)])
    await db["orders"].create_index([("company_id", 1), ("name", 1)])
    await db["orders"].create_index([("company_id", 1), ("order_id", 1)])
    await db["orders"].create_index([("company_id", 1), ("customer.email", 1)])

    # Messages
    await db["messages"].create_index([("company_id", 1), ("last_updated", -1)])
    await db["messages"].create_index([("company_id", 1), ("started_at", -1)])
    await db["messages"].create_index([("company_id", 1), ("status", 1), ("last_updated", -1)])
    await db["messages"].create_index([("company_id", 1), ("order_match_status", 1), ("last_updated", -1)])
    await db["messages"].create_index([("thread_id", 1), ("channel", 1)])
    await db["deleted_gmail_messages"].create_index(
        [("company_id", 1), ("user_id", 1), ("gmail_id", 1)],
        unique=True,
    )
    await db["deleted_gmail_messages"].create_index([("deleted_at", -1)])
    await db["processed_gmail_messages"].create_index(
        [("company_id", 1), ("user_id", 1), ("gmail_id", 1)],
        unique=True,
    )
    await db["processed_gmail_messages"].create_index([("claimed_at", -1)])

    # Memberships — frequently queried by user+status and company+role
    await db["memberships"].create_index([("user_id", 1), ("status", 1)])
    await db["memberships"].create_index([("company_id", 1), ("role", 1), ("status", 1)])

    # Users — lookup by email (login)
    await db["users"].create_index([("email", 1)], unique=True)

    # Gmail accounts — lookup by company
    await db["gmail_accounts"].create_index([("company_id", 1)])
    await db["gmail_accounts"].create_index([("email", 1)])

    # Shopify credentials — lookup by company and shop
    await db["shopify_cred"].create_index([("company_id", 1)])
    await db["shopify_cred"].create_index([("shop", 1)])

    # Audit logs — lookup by company + date
    await db["audit_logs"].create_index([("company_id", 1), ("created_at", -1)])

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        mongo_options = {"serverSelectionTimeoutMS": 20000}
        if MONGO_URL.startswith("mongodb+srv://"):
            mongo_options["tlsCAFile"] = certifi.where()

        mongo_client = AsyncIOMotorClient(MONGO_URL, **mongo_options)
        # Try to ping the server to check connection
        await mongo_client.admin.command("ping")
        logger.info("Connected to MongoDB")
        app.state.mongo_client = mongo_client
        app.state.db = mongo_client[DB_NAME]
        await ensure_database_indexes(app.state.db)
    except Exception as e:
        logger.critical("Failed to connect to MongoDB: %s", e)
        raise e

    asyncio.create_task(set_gmail_watches_periodically())

    yield  # App runs

    logger.info("Closing MongoDB connection")
    mongo_client.close()

app = FastAPI(title="Attentify APP", lifespan=lifespan)

# Mount Socket.IO app inside FastAPI
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

@app.get("/pingtest")
async def pingtest():
    return {"status": "ok"}

@app.head("/pingtest")
async def pingtest_head():
    return Response(status_code=200)

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Socket.IO authentication and events
@sio.event
async def connect(sid, environ, auth):
    """Authenticate socket connection using JWT token."""
    if not auth or not isinstance(auth, dict):
        raise ConnectionRefusedError("Authentication required")

    token = auth.get("token")
    if not token:
        raise ConnectionRefusedError("Authentication token required")

    try:
        from jose import jwt as jose_jwt, JWTError as JOSEJWTError
        payload = jose_jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("user_id")
        if not user_id:
            raise ConnectionRefusedError("Invalid token payload")

        # Optionally verify user exists in DB
        db = app.state.db
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise ConnectionRefusedError("User not found")

        # Store user info in session for later use
        async with sio.session(sid) as session:
            session["user_id"] = user_id
            session["email"] = user.get("email", "")
            session["name"] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()

        logger.info("Authenticated client connected: %s (user: %s)", sid, user.get("email"))

    except ConnectionRefusedError:
        raise
    except JOSEJWTError:
        raise ConnectionRefusedError("Invalid or expired token")
    except Exception as e:
        logger.error("Socket auth error: %s", e)
        raise ConnectionRefusedError("Authentication failed")

@sio.event
async def disconnect(sid):
    logger.info("Client disconnected: %s", sid)

# Custom event
@sio.event
async def ping_from_client(sid, data):
    logger.debug("Socket ping received: %s", data)
    await sio.emit("pong_from_server", {"msg": "pong!"}, to=sid)

# Routers
from app.api.v1 import auth
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
from app.api.v1 import gmail
app.include_router(gmail.router, prefix="/api/v1/gmail", tags=["Gmail"])
from app.api.v1 import message
app.include_router(message.router, prefix="/api/v1/message", tags=["Message"])
from app.api.v1 import shopify
app.include_router(shopify.router, prefix="/api/v1/shopify", tags=["Shopify"])
from app.api.v1 import users
app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
from app.api.v1 import admin
app.include_router(admin.router, prefix="/api/v1/admin", tags=["Admin"])
from app.api.v1 import company
app.include_router(company.router, prefix="/api/v1/company", tags=["Company"])
from app.api.v1 import membership
app.include_router(membership.router, prefix="/api/v1/membership", tags=["Membership"])
from app.api.v1 import invitation
app.include_router(invitation.router, prefix="/api/v1/invitations", tags=["Invitations"])
#app.include_router(inbox.router, prefix="/api/v1/inbox", tags=["Inbox"])
#app.include_router(ai.router, prefix="/api/v1/ai", tags=["AI"])
#app.include_router(templates.router, prefix="/api/v1/templates", tags=["Templates"])
from app.api.v1 import webhooks
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["Webhooks"])
#app.include_router(shopify.router, prefix="/api/v1/shopify", tags=["Shopify"])
from app.api.v1 import twilio
app.include_router(twilio.router, prefix="/api/v1/twilio", tags=["Twilio"])

#app.include_router(stripe.router, prefix="/api/v1/stripe", tags=["Stripe"])

@app.get("/")
def read_root():
    return {"status": "ok"}

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/test-db")
async def test(db=Depends(get_database)):
    collections = await db.list_collection_names()
    return {"collections": collections}
