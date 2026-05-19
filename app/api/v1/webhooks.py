from fastapi import APIRouter, Form, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime
from bson import ObjectId

router = APIRouter()

from motor.motor_asyncio import AsyncIOMotorClient
import os
from app.models.message import Message, ChatEntry


@router.post("/twilio/sms")
async def twilio_sms_webhook(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),
    SmsSid: str = Form(...),
    SmsMessageSid: str = Form(None),
    request: Request = None,
):
    thread_id = From  # Or customize: e.g. f"{From}:{To}"

    # Try to find the existing thread
    db = request.app.state.db
    doc = await db.messages.find_one({"thread_id": thread_id, "channel": "sms"})
    now = datetime.utcnow()

    chat_entry = ChatEntry(
        sender=From,
        recipient=To,
        content=Body,
        title=Body,
        channel="sms",
        message_type="text",
        metadata={
            "MessageSid": MessageSid,
            "SmsSid": SmsSid,
            "SmsMessageSid": SmsMessageSid,
            "from": From,
            "to": To,
            "date": now
        }
    )

    if doc:
        # Update thread: add new entry, update last_updated
        await db.messages.update_one(
            {"_id": doc["_id"]},
            {
                "$push": {"messages": chat_entry.dict()},
                "$set": {"last_updated": now}
            }
        )
    else:
        # New thread
        msg_obj = Message(
            thread_id=thread_id,
            participants=[From, To],
            client_id=From,
            channel="sms",
            status="open",
            started_at=now,
            last_updated=now,
            messages=[chat_entry],
        )
        await db.messages.insert_one(msg_obj.dict(by_alias=True))

    resp = MessagingResponse()
    resp.message("We've got your message!, we'll get back to you soon.")

    return Response(content=str(resp), media_type="application/xml")