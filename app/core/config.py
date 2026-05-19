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
