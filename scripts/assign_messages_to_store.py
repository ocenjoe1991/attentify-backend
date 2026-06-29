import argparse
import asyncio
import os
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
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Assign all company messages to a Shopify store.")
    parser.add_argument("--shop", default="punkcasesnz.myshopify.com")
    parser.add_argument("--company-id", default="")
    parser.add_argument("--env-file", default="../../attentify-backend.env")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--invalidate-order-cache",
        action="store_true",
        help="Unset cached order_info/order_match_status so tickets are re-matched using the new store scope.",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    env_path = (script_dir / args.env_file).resolve()
    load_env_file(env_path)

    mongo_url = os.getenv("MONGO_URL")
    db_name = os.getenv("DB_NAME", "attentify")
    if not mongo_url:
        raise RuntimeError("MONGO_URL is not set")

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    store_query = {"shop": args.shop, "status": {"$ne": "disconnected"}}
    if args.company_id:
        if not ObjectId.is_valid(args.company_id):
            raise RuntimeError("Invalid company id")
        store_query["company_id"] = ObjectId(args.company_id)

    stores = await db["shopify_cred"].find(store_query).to_list(length=10)
    if not stores:
        raise RuntimeError(f"No connected Shopify store found for {args.shop}")
    if len(stores) > 1:
        raise RuntimeError("More than one matching store found. Pass --company-id.")

    store = stores[0]
    company_id = store.get("company_id")
    message_query = {"company_id": company_id}
    total = await db["messages"].count_documents(message_query)
    already_assigned = await db["messages"].count_documents({
        **message_query,
        "default_store_id": store["_id"],
        "default_store_shop": store["shop"],
    })

    print(f"Store: {store['shop']} ({store['_id']})")
    print(f"Company: {company_id}")
    print(f"Messages in company: {total}")
    print(f"Already assigned: {already_assigned}")

    if not args.apply:
        print("Dry run only. Re-run with --apply to update messages.")
        client.close()
        return

    update_doc = {
        "$set": {
            "default_store_id": store["_id"],
            "default_store_shop": store["shop"],
        }
    }
    if args.invalidate_order_cache:
        update_doc["$unset"] = {
            "order_info": "",
            "order_match_status": "",
            "order_analysis": "",
        }

    result = await db["messages"].update_many(message_query, update_doc)
    print(f"Matched messages: {result.matched_count}")
    print(f"Modified messages: {result.modified_count}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
