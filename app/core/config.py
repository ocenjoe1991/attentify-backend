import json

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_PORT: int = 587
    MAIL_SERVER: str
    MAIL_FROM_NAME: str = "Attentify"
    SECRET_KEY: str 
    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = ""
    PUBSUB_TOPIC: str
    PUBSUB_PROJECT: str
    PUBSUB_SUBSCRIPTION: str
    SERVICE_ACCOUNT_JSON: str
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()


def service_account_info() -> dict:
    """Parse Render's escaped service-account JSON safely."""
    value = settings.SERVICE_ACCOUNT_JSON
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Render environment exports can preserve a backslash immediately before
        # a physical private-key line break, which is invalid JSON.
        normalized = value.replace("\\\r\n", "\\n").replace("\\\n", "\\n")
        return json.loads(normalized)
