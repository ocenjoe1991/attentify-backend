import argparse
import asyncio
import os
import re
from pathlib import Path

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def order_name_from_message(message: dict) -> str:
    order_info = message.get("order_info") or {}
    value = (
        order_info.get("order_id")
        or message.get("matched_order_name")
        or message.get("matched_order_id")
        or ""
    )
    value = str(value).strip()
    if not value:
        parts = [
            str(message.get("title") or ""),
            str(message.get("client") or ""),
        ]
        for entry in message.get("messages") or []:
            if isinstance(entry, dict):
                parts.extend([
                    str(entry.get("title") or ""),
                    str(entry.get("content") or ""),
                    str((entry.get("metadata") or {}).get("subject") or ""),
                ])
        match = re.search(r"#?[A-Za-z]{1,6}\d{3,}", "\n".join(parts), re.IGNORECASE)
        if not match:
            return ""
        value = match.group(0)
    return value if value.startswith("#") else f"#{value}"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile message selected store from matched Shopify orders.")
    parser.add_argument("--company-id", default="")
    parser.add_argument("--env-file", default="../../attentify-backend.env")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_env_file((Path(__file__).resolve().parent / args.env_file).resolve())
    mongo_url = os.getenv("MONGO_URL")
    db_name = os.getenv("DB_NAME", "attentify")
    if not mongo_url:
        raise RuntimeError("MONGO_URL is not set")

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    message_query = {"channel": "email"}
    if args.company_id:
        if not ObjectId.is_valid(args.company_id):
            raise RuntimeError("Invalid company id")
        message_query["company_id"] = ObjectId(args.company_id)

    checked = 0
    matched = 0
    mismatched = 0
    ambiguous = 0
    modified = 0

    async for message in db["messages"].find(message_query):
        order_name = order_name_from_message(message)
        if not order_name:
            continue
        checked += 1
        orders = await db["orders"].find({
            "company_id": message.get("company_id"),
            "name": order_name,
        }).to_list(length=3)
        if len(orders) != 1:
            ambiguous += 1
            continue

        order = orders[0]
        order_shop = order.get("shop")
        if not order_shop:
            continue
        matched += 1
        if message.get("default_store_shop") == order_shop:
            continue

        mismatched += 1
        cred = await db["shopify_cred"].find_one({
            "company_id": message.get("company_id"),
            "shop": order_shop,
            "status": {"$ne": "disconnected"},
        })
        update_doc = {
            "$set": {
                "default_store_shop": order_shop,
            }
        }
        if cred:
            update_doc["$set"]["default_store_id"] = cred["_id"]
        if args.apply:
            result = await db["messages"].update_one({"_id": message["_id"]}, update_doc)
            modified += result.modified_count
        else:
            print(f"Would update {message['_id']}: {message.get('default_store_shop')} -> {order_shop} ({order_name})")

    print(f"Checked messages with order id: {checked}")
    print(f"Matched unique orders: {matched}")
    print(f"Mismatched message stores: {mismatched}")
    print(f"Ambiguous/missing orders: {ambiguous}")
    if args.apply:
        print(f"Modified messages: {modified}")
    else:
        print("Dry run only. Re-run with --apply to update messages.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
