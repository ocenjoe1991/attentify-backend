from fastapi import APIRouter, Form, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime, timezone
from bson import ObjectId

router = APIRouter()

from motor.motor_asyncio import AsyncIOMotorClient
import os
from app.models.message import Message, ChatEntry


def normalize_phone(value: str) -> str:
    return (value or "").strip().replace(" ", "")


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
    db = request.app.state.db
    from_phone = normalize_phone(From)
    to_phone = normalize_phone(To)
    account = await db.phone_accounts.find_one(
        {"provider": "twilio", "phone_number": to_phone, "status": "connected"}
    )
    if not account:
        return Response(content="", status_code=204)

    company_id = account["company_id"]
    user_id = account["user_id"]
    thread_id = f"sms:{company_id}:{from_phone}:{to_phone}"

    # Try to find the existing thread
    doc = await db.messages.find_one({"thread_id": thread_id, "channel": "sms"})
    now = datetime.now(timezone.utc)

    chat_entry = ChatEntry(
        sender=from_phone,
        recipient=to_phone,
        content=Body,
        title=Body,
        timestamp=now,
        channel="sms",
        message_type="text",
        metadata={
            "MessageSid": MessageSid,
            "SmsSid": SmsSid,
            "SmsMessageSid": SmsMessageSid,
            "from": from_phone,
            "to": to_phone,
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
            user_id=user_id,
            company_id=company_id,
            thread_id=thread_id,
            participants=[from_phone, to_phone],
            client=from_phone,
            agent=to_phone,
            channel="sms",
            status="Open",
            title=Body[:80],
            started_at=now,
            last_updated=now,
            messages=[chat_entry],
        )
        await db.messages.insert_one(msg_obj.dict(by_alias=True))

    resp = MessagingResponse()
    resp.message("We've got your message!, we'll get back to you soon.")

    return Response(content=str(resp), media_type="application/xml")
