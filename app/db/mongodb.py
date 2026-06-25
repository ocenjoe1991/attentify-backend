from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase

_db: AsyncIOMotorDatabase | None = None


def set_database(db: AsyncIOMotorDatabase) -> None:
    """Store the database instance for background tasks that lack request context."""
    global _db
    _db = db


async def get_database(request: Request | None = None) -> AsyncIOMotorDatabase:
    """Dependency that returns the database instance.
    Uses request.app.state.db when available, falls back to module-level _db for background tasks."""
    if request is not None:
        return request.app.state.db
    if _db is not None:
        return _db
    raise RuntimeError("Database not initialized – call set_database() during startup")
