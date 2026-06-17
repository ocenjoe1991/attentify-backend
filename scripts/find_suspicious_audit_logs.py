"""
Simple script to query MongoDB for recent Archived/Unarchived audit log entries
and print relevant fields (created_at, actor_email, details.ip, details.user_agent).

Usage:
    python scripts/find_suspicious_audit_logs.py --mongo-uri mongodb://localhost:27017 --db your_db --days 7 --limit 200

Requires: pymongo
"""
import argparse
from datetime import datetime, timedelta
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    client = MongoClient(args.mongo_uri)
    db = client[args.db]

    since = datetime.utcnow() - timedelta(days=args.days)
    query = {
        "action": {"$in": ["Archived ticket", "Unarchived ticket"]},
        "created_at": {"$gte": since}
    }

    cursor = db.audit_logs.find(query).sort("created_at", -1).limit(args.limit)
    for doc in cursor:
        created_at = doc.get("created_at")
        actor_email = doc.get("actor_email")
        details = doc.get("details", {})
        ip = details.get("ip") or details.get("actor_ip") or None
        ua = details.get("user_agent") or details.get("userAgent") or None
        print(f"{created_at} | actor={actor_email} | ip={ip} | ua={ua} | id={doc.get('_id')}")

if __name__ == "__main__":
    main()
