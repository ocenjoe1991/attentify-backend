from datetime import datetime, timezone
from typing import Any

from bson import ObjectId


def display_user_name(user: dict | None) -> str:
    if not user:
        return "Unknown user"
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return name or user.get("email", "Unknown user")


def to_object_id(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def to_json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json_safe(item) for key, item in value.items()}
    return value


def flatten_search_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = []
        for key, item in value.items():
            values.append(str(key).replace("_", " "))
            values.extend(flatten_search_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(flatten_search_values(item))
        return values
    if value is None:
        return []
    return [str(value)]


async def record_audit_log(
    db,
    *,
    company_id,
    actor: dict | None,
    actor_role: str = "",
    action: str,
    entity_type: str = "",
    entity_id=None,
    ticket: str = "",
    customer: str = "",
    details: dict | None = None,
) -> None:
    safe_details = to_json_safe(details or {})
    safe_entity_id = to_object_id(entity_id)
    actor_name = display_user_name(actor) if actor else ("System" if actor_role == "system" else "Unknown user")
    actor_email = actor.get("email", "") if actor else ""
    search_values = [
        actor_name,
        actor_email,
        actor_role or (actor.get("role", "unknown") if actor else "unknown"),
        action,
        entity_type,
        str(safe_entity_id or ""),
        ticket,
        customer,
        *flatten_search_values(safe_details),
    ]
    search_text = " ".join(str(value) for value in search_values if value)[:16000]

    await db["audit_logs"].insert_one({
        "company_id": to_object_id(company_id),
        "actor_id": actor.get("_id") if actor else None,
        "actor_name": actor_name,
        "actor_email": actor_email,
        "actor_role": actor_role or (actor.get("role", "unknown") if actor else "unknown"),
        "action": action,
        "entity_type": entity_type,
        "entity_id": safe_entity_id,
        "ticket": ticket,
        "customer": customer,
        "details": safe_details,
        "search_text": search_text,
        "created_at": datetime.now(timezone.utc),
    })


def serialize_audit_log(doc: dict) -> dict:
    return to_json_safe(doc)
