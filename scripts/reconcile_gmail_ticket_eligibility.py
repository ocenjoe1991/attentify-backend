import argparse
import asyncio
import os
import re
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.utils.message_text import visible_email_text


TICKET_GENERATION_POLICY = "verified-order-reference-v1"
ORDER_MENTION_PATTERN = re.compile(r"\border\b", re.IGNORECASE)
ORDER_REFERENCE_PATTERN = re.compile(r"(?<![\w#])#(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{3,}\b")
UNHASHED_ORDER_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,6}\d{3,}[A-Z0-9-]*|\d{3,}[A-Z]{2,6}[A-Z0-9-]*)\b"
)


def load_env(env_path: str | None) -> None:
    if env_path:
        load_dotenv(env_path)
        return
    load_dotenv(Path(__file__).resolve().parents[2] / "attentify-backend.env")
    load_dotenv()


def first_email_entry(message: dict) -> dict:
    entries = [entry for entry in message.get("messages") or [] if isinstance(entry, dict)]
    return min(entries, key=lambda entry: str(entry.get("timestamp") or ""), default={})


def order_reference_values(text: str) -> set[str]:
    matches = [
        *ORDER_REFERENCE_PATTERN.findall(text),
        *UNHASHED_ORDER_REFERENCE_PATTERN.findall(text),
    ]
    return {match.lstrip("#").upper() for match in matches if match.lstrip("#")}


async def ticket_is_eligible(db, message: dict) -> bool:
    entry = first_email_entry(message)
    subject = entry.get("title") or message.get("title") or ""
    content = entry.get("content") or ""
    is_html = entry.get("message_type") == "html"
    text = f"{visible_email_text(subject, is_html=False)} {visible_email_text(content, is_html=is_html)}"

    if ORDER_MENTION_PATTERN.search(text):
        return True

    references = order_reference_values(text)
    if not references:
        return False

    order_query = [
        {"name": {"$regex": f"^#?{re.escape(reference)}$", "$options": "i"}}
        for reference in references
    ]
    return await db["orders"].find_one(
        {"company_id": message["company_id"], "$or": order_query}, {"_id": 1}
    ) is not None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove Gmail tickets that do not meet the current ticket-generation policy."
    )
    parser.add_argument("--env", help="Path to env file. Defaults to ../attentify-backend.env then .env.")
    parser.add_argument("--company-id", required=True, help="Company ObjectId to reconcile.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without updating documents.")
    args = parser.parse_args()

    if not ObjectId.is_valid(args.company_id):
        raise SystemExit("Invalid company ID")
    load_env(args.env)
    mongo_url = os.getenv("MONGO_URL")
    if not mongo_url:
        raise SystemExit("MONGO_URL is not set")

    client = AsyncIOMotorClient(mongo_url)
    db = client[os.getenv("DB_NAME", "attentify")]
    company_id = ObjectId(args.company_id)
    scanned = removed = retained = 0

    try:
        cursor = db["messages"].find(
            {
                "company_id": company_id,
                "channel": "email",
                "ticket": {"$type": "string", "$ne": ""},
            }
        )
        async for message in cursor:
            scanned += 1
            eligible = await ticket_is_eligible(db, message)
            if eligible:
                retained += 1
                if not args.dry_run:
                    await db["messages"].update_one(
                        {"_id": message["_id"], "ticket": message["ticket"]},
                        {"$set": {
                            "ticket_generation_policy": TICKET_GENERATION_POLICY,
                            "ticket_generation_eligible": True,
                        }},
                    )
                continue

            removed += 1
            if not args.dry_run:
                await db["messages"].update_one(
                    {"_id": message["_id"], "ticket": message["ticket"]},
                    {
                        "$unset": {"ticket": ""},
                        "$set": {
                            "ticket_generation_policy": TICKET_GENERATION_POLICY,
                            "ticket_generation_eligible": False,
                        },
                    },
                )
    finally:
        client.close()

    action = "would remove" if args.dry_run else "removed"
    print(f"Scanned {scanned} ticketed Gmail threads; retained {retained}; {action} {removed}.")


if __name__ == "__main__":
    asyncio.run(main())
