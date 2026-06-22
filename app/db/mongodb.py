from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase


async def get_database(request: Request) -> AsyncIOMotorDatabase:
    """Dependency that returns the database instance from the app's lifespan state."""
    return request.app.state.db
