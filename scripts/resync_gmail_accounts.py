#!/usr/bin/env python3
"""
Reset Gmail history IDs and resync all connected Gmail accounts.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import certifi
from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.gmail_service import fetch_and_save_gmail


def clean_mongo_url(value: str) -> str:
    return (value or "mongodb://localhost:27017").split(" #", 1)[0].strip()


async def main(env_file: str | None, reset_only: bool) -> None:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    mongo_url = clean_mongo_url(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
    db_name = os.getenv("DB_NAME", "attentify").strip()
    mongo_options = {"serverSelectionTimeoutMS": 20000}
    if mongo_url.startswith("mongodb+srv://"):
        mongo_options["tlsCAFile"] = certifi.where()

    client = AsyncIOMotorClient(mongo_url, **mongo_options)
    db = client[db_name]

    try:
        await client.admin.command("ping")
        reset_result = await db["gmail_accounts"].update_many(
            {"status": "connected"},
            {"$set": {"history_id": ""}},
        )
        print(f"Reset history_id on {reset_result.modified_count} connected Gmail account(s).")

        if reset_only:
            return

        accounts = await db["gmail_accounts"].find({"status": "connected"}).to_list(None)
        print(f"Resyncing {len(accounts)} connected Gmail account(s).")

        for account in accounts:
            user_id = account.get("user_id")
            company_id = account.get("company_id")
            if not isinstance(user_id, ObjectId) or not isinstance(company_id, ObjectId):
                print(f"- {account.get('email', 'unknown')}: skipped invalid user/company id")
                continue

            token_data = {
                "account_id": account["_id"],
                "email": account["email"],
                "access_token": account["access_token"],
                "refresh_token": account["refresh_token"],
                "client_id": account["client_id"],
                "client_secret": account["client_secret"],
                "expires_at": account.get("expires_at"),
            }
            result = await fetch_and_save_gmail(
                token_data,
                db,
                user_id=str(user_id),
                company_id=str(company_id),
            )
            print(
                f"- {result.get('email')}: {result.get('status')} "
                f"fetched={result.get('fetched_count', 0)} "
                f"stored={result.get('stored_count', 0)} "
                f"updated={result.get('updated_count', 0)}"
            )
            if result.get("status") != "ok":
                print(f"  reason={result.get('reason')} message={result.get('message')}")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parents[2] / "env(attentify-backend)"),
        help="Path to the backend environment file.",
    )
    parser.add_argument("--reset-only", action="store_true")
    args = parser.parse_args()

    asyncio.run(main(args.env_file, args.reset_only))
