from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne


def _as_object_id(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def extract_gmail_ids(message: dict) -> list[str]:
    gmail_ids: list[str] = []
    for item in message.get("messages", []):
        if not isinstance(item, dict):
            continue
        gmail_id = (item.get("metadata") or {}).get("gmail_id")
        if gmail_id and gmail_id not in gmail_ids:
            gmail_ids.append(gmail_id)
    return gmail_ids


async def record_deleted_gmail_messages(db, message: dict, actor: dict | None = None) -> int:
    gmail_ids = extract_gmail_ids(message)
    if not gmail_ids:
        return 0

    now = datetime.now(timezone.utc)
    company_id = _as_object_id(message.get("company_id"))
    user_id = _as_object_id(message.get("user_id"))
    actor_id = _as_object_id(actor.get("_id")) if actor else None
    message_id = _as_object_id(message.get("_id"))
    operations = [
        UpdateOne(
            {
                "company_id": company_id,
                "user_id": user_id,
                "gmail_id": gmail_id,
            },
            {
                "$set": {
                    "company_id": company_id,
                    "user_id": user_id,
                    "gmail_id": gmail_id,
                    "thread_id": message.get("thread_id", ""),
                    "message_id": message_id,
                    "deleted_by": actor_id,
                    "deleted_at": now,
                }
            },
            upsert=True,
        )
        for gmail_id in gmail_ids
    ]

    result = await db["deleted_gmail_messages"].bulk_write(operations, ordered=False)
    return result.upserted_count + result.modified_count


async def is_deleted_gmail_message(db, *, company_id, user_id, gmail_id: str) -> bool:
    if not gmail_id:
        return False
    deleted = await db["deleted_gmail_messages"].find_one(
        {
            "company_id": _as_object_id(company_id),
            "user_id": _as_object_id(user_id),
            "gmail_id": gmail_id,
        },
        {"_id": 1},
    )
    return deleted is not None
