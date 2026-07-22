#!/usr/bin/env python3
"""
Reset Gmail account history IDs to force full resync of emails with correct UTC timestamps.
Run this script to normalize existing email timestamps after timezone fix deployment.
"""

import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017").strip()
DB_NAME = os.getenv("DB_NAME", "attentify").strip()


async def reset_gmail_history():
    """Reset history_id for all Gmail accounts to trigger full resync"""
    print(f"Connecting to MongoDB: {MONGO_URL}")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    
    gmail_accounts = db["gmail_accounts"]
    
    try:
        # Reset all history IDs
        result = await gmail_accounts.update_many(
            {},
            {"$set": {"history_id": ""}}
        )
        
        print(f"Reset {result.modified_count} Gmail account history IDs")
        print("  Next Gmail sync will fetch all emails with correct UTC timestamps")
        
        # Show updated accounts
        accounts = await gmail_accounts.find({"status": "connected"}).to_list(100)
        print(f"\nTotal connected Gmail accounts: {len(accounts)}")
        for acc in accounts:
            print(f"  - {acc.get('email', 'unknown')}")
            
    except Exception as e:
        print(f"Error resetting history IDs: {e}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    print("=" * 60)
    print("Gmail History ID Reset")
    print("=" * 60)
    print()
    print("This will reset all Gmail account history IDs,")
    print("forcing a full resync of emails with correct UTC timestamps.")
    print()
    response = input("Continue? (y/n): ").strip().lower()
    if response == "y":
        asyncio.run(reset_gmail_history())
        print("\nComplete. Emails will resync on next fetch.")
    else:
        print("Cancelled.")
