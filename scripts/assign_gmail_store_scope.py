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
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Assign multiple Shopify stores as Gmail/message matching scope.")
    parser.add_argument("--shops", default="punkcaseca.myshopify.com,punkcasesnz.myshopify.com")
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
    shops = [shop.strip() for shop in args.shops.split(",") if shop.strip()]
    query = {"shop": {"$in": shops}, "status": {"$ne": "disconnected"}}
    if args.company_id:
        query["company_id"] = ObjectId(args.company_id)
    stores = await db["shopify_cred"].find(query).to_list(length=20)
    if len(stores) != len(shops):
        found = {store.get("shop") for store in stores}
        missing = [shop for shop in shops if shop not in found]
        raise RuntimeError(f"Missing stores: {missing}")
    company_ids = {store["company_id"] for store in stores}
    if len(company_ids) != 1:
        raise RuntimeError("Stores belong to different companies. Pass a single company scope.")
    company_id = next(iter(company_ids))
    by_shop = {store["shop"]: store for store in stores}
    ordered_stores = [by_shop[shop] for shop in shops]
    store_ids = [store["_id"] for store in ordered_stores]
    store_shops = [store["shop"] for store in ordered_stores]

    gmail_query = {"company_id": company_id}
    message_query = {"company_id": company_id, "channel": "email"}
    gmail_count = await db["gmail_accounts"].count_documents(gmail_query)
    message_count = await db["messages"].count_documents(message_query)

    print(f"Company: {company_id}")
    print(f"Stores: {', '.join(store_shops)}")
    print(f"Gmail accounts: {gmail_count}")
    print(f"Email messages: {message_count}")

    if not args.apply:
        print("Dry run only. Re-run with --apply to update Gmail accounts and email messages.")
        client.close()
        return

    gmail_result = await db["gmail_accounts"].update_many(
        gmail_query,
        {"$set": {"store_ids": store_ids}, "$unset": {"store_id": ""}},
    )
    message_result = await db["messages"].update_many(
        message_query,
        {
            "$set": {
                "order_matching_store_ids": store_ids,
                "order_matching_store_shops": store_shops,
            }
        },
    )
    print(f"Modified Gmail accounts: {gmail_result.modified_count}")
    print(f"Modified email messages: {message_result.modified_count}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
