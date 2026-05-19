#app/main.py
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
import os
from dotenv import load_dotenv
load_dotenv()  # Load from .env at startup
from app.db.mongodb import get_database
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.core.config import settings
import asyncio

import socketio

origins = os.getenv("ORIGINS", "http://localhost:5173").split(",")

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[origin.strip() for origin in origins]
)

# CORS origins
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "attentify")
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

async def set_gmail_watches_periodically():
    while True:
        print("Setting up Gmail Watches...")
        db = app.state.db
        cursor = db["gmail_accounts"].find()
        async for cred in cursor:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, set_gmail_watch, cred)
            print(response)
            
        await asyncio.sleep(24 * 3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        # Try to ping the server to check connection
        await mongo_client.admin.command("ping")
        print("✅ Connected to MongoDB")
        app.state.mongo_client = mongo_client
        app.state.db = mongo_client[DB_NAME]
    except Exception as e:
        print("❌ Failed to connect to MongoDB:", e)
        raise e  # Optional: prevent app from starting if DB fails

    asyncio.create_task(set_gmail_watches_periodically())

    yield  # App runs

    print("🔌 Closing MongoDB connection")
    mongo_client.close()

app = FastAPI(title="Attentify APP", lifespan=lifespan)

# Mount Socket.IO app inside FastAPI
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

app.add_middleware(SessionMiddleware, secret_key="supersecret")
# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Example event
@sio.event
async def connect(sid, environ):
    print("Client connected:", sid)

@sio.event
async def disconnect(sid):
    print("Client disconnected:", sid)

# Custom event
@sio.event
async def ping_from_client(sid, data):
    print("Received:", data)
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