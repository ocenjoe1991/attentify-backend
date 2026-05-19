from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from twilio.rest import Client
from datetime import datetime
from bson import ObjectId
from app.models.message import Message, ChatEntry  # assuming these are in models.py
import os

router = APIRouter()

# Twilio setup
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(ACCOUNT_SID, AUTH_TOKEN)

# Request body schema
class SMSRequest(BaseModel):
    to: str
    message: str
    thread_id: Optional[str] = None  # Optional: use if you want to link to existing thread

# api/v1/twilio/send-sms
@router.post("/send-sms")
async def send_sms(data: SMSRequest, request: Request):
    try:
        # Send SMS
        message = client.messages.create(
            to=data.to,
            from_=TWILIO_PHONE,
            body=data.message
        )

        # Create ChatEntry for this outgoing SMS
        chat_entry = ChatEntry(
            sender=TWILIO_PHONE,
            recipient=data.to,
            content=data.message,
            timestamp=datetime.utcnow(),
            channel="sms",
            message_type="text",
            metadata={"twilio_sid": message.sid}
        )
        
        db = request.app.state.db
        # Upsert Message document (either new or existing thread)
        if data.thread_id:
            result = await db.messages.update_one(
                {"thread_id": data.thread_id},
                {
                    "$push": {"messages": chat_entry.dict()},
                    "$set": {"last_updated": datetime.utcnow()}
                },
                upsert=True
            )
        else:
            new_message = Message(
                thread_id=str(ObjectId()),  # or generate based on business logic
                participants=[TWILIO_PHONE, data.to],
                client_id=data.to,
                channel="sms",
                messages=[chat_entry]
            )
            result = await db.messages.insert_one(new_message.dict(by_alias=True))

        return {
            "sid": message.sid,
            "status": message.status,
            "thread_id": data.thread_id or str(new_message.thread_id)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
