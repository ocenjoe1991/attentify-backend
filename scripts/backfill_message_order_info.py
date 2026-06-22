import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


def load_env(env_path: str | None) -> None:
    if env_path:
        load_dotenv(env_path)
        return

    default_env = Path(__file__).resolve().parents[2] / "attentify-backend.env"
    load_dotenv(default_env)
    load_dotenv()


def build_order_info(message: dict) -> dict | None:
    status = message.get("order_match_status")
    now = datetime.now(timezone.utc)

    if status == "matched":
        order_id = message.get("matched_order_name") or message.get("matched_order_id") or ""
        return {
            "order_id": order_id,
            "type": "order",
            "status": 1 if order_id else 0,
            "msg": "",
            "analysis_source": "backfill",
            "analyzed_at": now,
        }

    if status == "possible":
        order_id = message.get("matched_order_name") or message.get("matched_order_id") or ""
        return {
            "order_id": order_id,
            "type": "order",
            "status": 1 if order_id else 0,
            "msg": "Email not matched",
            "analysis_source": "backfill",
            "analyzed_at": now,
        }

    if status == "unmatched":
        order_id = message.get("matched_order_name") or message.get("matched_order_id") or ""
        return {
            "order_id": order_id,
            "type": "order",
            "status": 1 if order_id else 0,
            "msg": "Order not found",
            "analysis_source": "backfill",
            "analyzed_at": now,
        }

    if status == "not_order":
        return {
            "order_id": "",
            "type": "",
            "status": 0,
            "msg": "No order found in message",
            "analysis_source": "backfill",
            "analyzed_at": now,
        }

    return None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill messages.order_info from existing order_match_status without calling AI."
    )
    parser.add_argument("--env", help="Path to env file. Defaults to ../attentify-backend.env then .env.")
    parser.add_argument("--company-id", help="Optional company ObjectId to limit the backfill.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without updating documents.")
    args = parser.parse_args()

    load_env(args.env)
    mongo_url = os.getenv("MONGO_URL")
    db_name = os.getenv("DB_NAME", "attentify")
    if not mongo_url:
        raise RuntimeError("MONGO_URL is not set")

    query = {
        "order_info": {"$exists": False},
        "order_match_status": {"$in": ["matched", "possible", "unmatched", "not_order"]},
    }
    if args.company_id:
        if not ObjectId.is_valid(args.company_id):
            raise RuntimeError("Invalid company id")
        query["company_id"] = ObjectId(args.company_id)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    scanned = 0
    updated = 0
    async for message in db["messages"].find(query):
        scanned += 1
        order_info = build_order_info(message)
        if not order_info:
            continue
        if not args.dry_run:
            result = await db["messages"].update_one(
                {"_id": message["_id"], "order_info": {"$exists": False}},
                {"$set": {"order_info": order_info}},
            )
            updated += result.modified_count
        else:
            updated += 1

    client.close()
    mode = "would update" if args.dry_run else "updated"
    print(f"Scanned {scanned} messages; {mode} {updated}.")


if __name__ == "__main__":
    asyncio.run(main())
