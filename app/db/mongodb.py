from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase

_db: AsyncIOMotorDatabase | None = None


def set_database(db: AsyncIOMotorDatabase) -> None:
    """Store the database instance for background tasks that lack request context."""
    global _db
    _db = db


def get_db() -> AsyncIOMotorDatabase:
    """Return the database instance for background tasks (no request context)."""
    if _db is None:
        raise RuntimeError("Database not initialized – call set_database() during startup")
    return _db


async def get_database(request: Request) -> AsyncIOMotorDatabase:
    """FastAPI dependency – returns database instance from request.app.state."""
    return request.app.state.db
