from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError


def _as_object_id(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


async def claim_gmail_message(db, *, company_id, user_id, gmail_id: str, thread_id: str = "") -> bool:
    if not gmail_id:
        return False
    try:
        await db["processed_gmail_messages"].insert_one(
            {
                "company_id": _as_object_id(company_id),
                "user_id": _as_object_id(user_id),
                "gmail_id": gmail_id,
                "thread_id": thread_id,
                "claimed_at": datetime.now(timezone.utc),
            }
        )
        return True
    except DuplicateKeyError:
        return False


async def release_gmail_message_claim(db, *, company_id, user_id, gmail_id: str) -> None:
    if not gmail_id:
        return
    await db["processed_gmail_messages"].delete_one(
        {
            "company_id": _as_object_id(company_id),
            "user_id": _as_object_id(user_id),
            "gmail_id": gmail_id,
        }
    )
