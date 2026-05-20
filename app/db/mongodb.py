from motor.motor_asyncio import AsyncIOMotorClient
import os
import certifi

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017").strip()
DB_NAME = os.getenv("DB_NAME", "attentify").strip()

mongo_options = {}
if MONGO_URL.startswith("mongodb+srv://"):
    mongo_options["tlsCAFile"] = certifi.where()

client = AsyncIOMotorClient(MONGO_URL, **mongo_options)
db = client[DB_NAME]

async def get_database():
    return db
