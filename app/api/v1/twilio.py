from datetime import datetime
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.core.security import get_current_user
from app.core.audit import record_audit_log
from app.db.mongodb import get_database
from app.models.message import ChatEntry
from app.utils.datetime_utils import to_utc_iso

router = APIRouter()


class TwilioAccountCreate(BaseModel):
    company_id: str
    account_sid: str
    auth_token: str
    phone_number: str
    label: Optional[str] = None


class SMSRequest(BaseModel):
    to: str
    message: str
    company_id: str
    from_phone: Optional[str] = None
    thread_id: Optional[str] = None


def normalize_phone(value: str) -> str:
    return (value or "").strip().replace(" ", "")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def phone_account_helper(account: dict) -> dict:
    return {
        "id": str(account["_id"]),
        "company_id": str(account["company_id"]),
        "user_id": str(account["user_id"]),
        "phone_number": account["phone_number"],
        "label": account.get("label", ""),
        "status": account.get("status", "connected"),
        "provider": account.get("provider", "twilio"),
        "account_sid": mask_secret(account.get("account_sid", "")),
        "created_at": to_utc_iso(account.get("created_at")),
        "updated_at": to_utc_iso(account.get("updated_at")),
    }


async def require_company_member(db, current_user: dict, company_id: str) -> dict:
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")

    membership = await db["memberships"].find_one(
        {"user_id": current_user["_id"], "company_id": ObjectId(company_id)}
    )
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")
    return membership


def validate_twilio_number(account_sid: str, auth_token: str, phone_number: str) -> None:
    try:
        client = Client(account_sid, auth_token)
        numbers = client.incoming_phone_numbers.list(phone_number=phone_number, limit=1)
    except TwilioRestException as exc:
        raise HTTPException(status_code=400, detail=f"Twilio validation failed: {exc.msg}")

    if not numbers:
        raise HTTPException(
            status_code=400,
            detail="This phone number was not found in the provided Twilio account.",
        )


@router.get("/accounts")
async def list_twilio_accounts(
    company_id: str = Query(...),
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    await require_company_member(db, current_user, company_id)

    cursor = db["phone_accounts"].find(
        {"company_id": ObjectId(company_id), "provider": "twilio"}
    ).sort("created_at", -1)

    accounts = []
    async for account in cursor:
        accounts.append(phone_account_helper(account))
    return {"accounts": accounts}


@router.post("/accounts")
async def create_twilio_account(
    payload: TwilioAccountCreate,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    membership = await require_company_member(db, current_user, payload.company_id)
    if membership.get("role") not in {"company_owner", "store_owner"}:
        raise HTTPException(status_code=403, detail="Only owners can connect phone accounts")

    phone_number = normalize_phone(payload.phone_number)
    if not phone_number.startswith("+"):
        raise HTTPException(status_code=400, detail="Phone number must be in E.164 format, e.g. +15551234567")

    validate_twilio_number(payload.account_sid, payload.auth_token, phone_number)

    now = datetime.utcnow()
    account_doc = {
        "company_id": ObjectId(payload.company_id),
        "user_id": current_user["_id"],
        "provider": "twilio",
        "phone_number": phone_number,
        "label": payload.label or "",
        "account_sid": payload.account_sid,
        "auth_token": payload.auth_token,
        "status": "connected",
        "created_at": now,
        "updated_at": now,
    }

    result = await db["phone_accounts"].update_one(
        {"company_id": ObjectId(payload.company_id), "provider": "twilio", "phone_number": phone_number},
        {"$set": account_doc},
        upsert=True,
    )

    if result.upserted_id:
        account_doc["_id"] = result.upserted_id
    else:
        existing = await db["phone_accounts"].find_one(
            {"company_id": ObjectId(payload.company_id), "provider": "twilio", "phone_number": phone_number}
        )
        account_doc["_id"] = existing["_id"]

    await record_audit_log(
        db,
        company_id=ObjectId(payload.company_id),
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action="Connected Twilio phone account",
        entity_type="phone_account",
        entity_id=account_doc["_id"],
        details={"phone_number": phone_number, "label": payload.label or ""},
    )

    return phone_account_helper(account_doc)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_twilio_account(
    account_id: str,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status_code=400, detail="Invalid phone account ID")

    account = await db["phone_accounts"].find_one({"_id": ObjectId(account_id), "provider": "twilio"})
    if not account:
        raise HTTPException(status_code=404, detail="Phone account not found")

    membership = await require_company_member(db, current_user, str(account["company_id"]))
    if membership.get("role") not in {"company_owner", "store_owner"}:
        raise HTTPException(status_code=403, detail="Only owners can remove phone accounts")

    await db["phone_accounts"].delete_one({"_id": account["_id"]})
    await record_audit_log(
        db,
        company_id=account["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action="Removed Twilio phone account",
        entity_type="phone_account",
        entity_id=account["_id"],
        details={"phone_number": account.get("phone_number"), "label": account.get("label", "")},
    )
    return None


async def get_company_twilio_account(db, company_id: ObjectId, from_phone: Optional[str] = None) -> dict:
    query = {"company_id": company_id, "provider": "twilio", "status": "connected"}
    if from_phone:
        query["phone_number"] = normalize_phone(from_phone)

    account = await db["phone_accounts"].find_one(query)
    if not account and from_phone:
        account = await db["phone_accounts"].find_one(
            {"company_id": company_id, "provider": "twilio", "status": "connected"}
        )
    if not account:
        raise HTTPException(status_code=400, detail="No connected Twilio phone account found for this company")
    return account


async def send_twilio_sms(account: dict, to_phone: str, body: str):
    try:
        client = Client(account["account_sid"], account["auth_token"])
        return client.messages.create(
            to=normalize_phone(to_phone),
            from_=account["phone_number"],
            body=body,
        )
    except TwilioRestException as exc:
        raise HTTPException(status_code=400, detail=f"Twilio send failed: {exc.msg}")


@router.post("/send-sms")
async def send_sms(
    data: SMSRequest,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    await require_company_member(db, current_user, data.company_id)
    company_id = ObjectId(data.company_id)
    account = await get_company_twilio_account(db, company_id, data.from_phone)
    sent = await send_twilio_sms(account, data.to, data.message)

    now = datetime.utcnow()
    chat_entry = ChatEntry(
        sender=account["phone_number"],
        recipient=normalize_phone(data.to),
        content=data.message,
        title=data.message,
        timestamp=now,
        channel="sms",
        message_type="text",
        metadata={
            "twilio_sid": sent.sid,
            "from": account["phone_number"],
            "to": normalize_phone(data.to),
            "date": now,
        },
    )

    if data.thread_id and ObjectId.is_valid(data.thread_id):
        await db["messages"].update_one(
            {"_id": ObjectId(data.thread_id), "company_id": company_id, "channel": "sms"},
            {"$push": {"messages": chat_entry.dict()}, "$set": {"last_updated": now}},
        )
        message_id = data.thread_id
    else:
        thread_id = f"sms:{company_id}:{normalize_phone(data.to)}:{account['phone_number']}"
        message_doc = {
            "user_id": current_user["_id"],
            "company_id": company_id,
            "thread_id": thread_id,
            "participants": [account["phone_number"], normalize_phone(data.to)],
            "channel": "sms",
            "status": "Open",
            "title": data.message[:80],
            "client": normalize_phone(data.to),
            "agent": account["phone_number"],
            "messages": [chat_entry.dict()],
            "last_updated": now,
            "started_at": now,
            "ai_summary": None,
            "tags": [],
            "resolved_by_ai": False,
        }
        result = await db["messages"].insert_one(message_doc)
        message_id = str(result.inserted_id)

    return {"sid": sent.sid, "status": sent.status, "message_id": message_id}


@router.post("/messages/{message_id}/reply")
async def reply_to_sms_message(
    message_id: str,
    payload: dict = Body(...),
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Reply content is required")

    message = await db["messages"].find_one({"_id": ObjectId(message_id), "channel": "sms"})
    if not message:
        raise HTTPException(status_code=404, detail="SMS message not found")

    await require_company_member(db, current_user, str(message["company_id"]))
    account = await get_company_twilio_account(db, message["company_id"], message.get("agent"))

    to_phone = message.get("client")
    for entry in reversed(message.get("messages", [])):
        metadata = entry.get("metadata", {}) or {}
        sender = metadata.get("from") or entry.get("sender")
        if sender and normalize_phone(sender) != account["phone_number"]:
            to_phone = sender
            break

    sent = await send_twilio_sms(account, to_phone, content)
    now = datetime.utcnow()
    chat_entry = ChatEntry(
        sender=account["phone_number"],
        recipient=normalize_phone(to_phone),
        content=content,
        title=content,
        timestamp=now,
        channel="sms",
        message_type="text",
        metadata={
            "twilio_sid": sent.sid,
            "from": account["phone_number"],
            "to": normalize_phone(to_phone),
            "date": now,
        },
    )

    await db["messages"].update_one(
        {"_id": message["_id"]},
        {
            "$push": {"messages": chat_entry.dict()},
            "$set": {
                "last_updated": now,
                "agent": account["phone_number"],
                "client": normalize_phone(to_phone),
            },
        },
    )

    return {"sid": sent.sid, "status": sent.status}
