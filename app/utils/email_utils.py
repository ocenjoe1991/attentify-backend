# email_utils.py
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from app.core.config import settings

conf = ConnectionConfig(
    MAIL_USERNAME=settings.MAIL_USERNAME,
    MAIL_PASSWORD=settings.MAIL_PASSWORD,
    MAIL_FROM=settings.MAIL_FROM,
    MAIL_PORT=settings.MAIL_PORT,
    MAIL_SERVER=settings.MAIL_SERVER,
    MAIL_FROM_NAME=settings.MAIL_FROM_NAME,
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True
)

async def send_invitation_email(to_email: str, invite_link: str):
    message = MessageSchema(
        subject="You're invited!",
        recipients=[to_email],
        body=f"Hello,\n\nYou have been invited. Click here to join: {invite_link}\n\nThis link will expire in 48 hours.",
        subtype="plain"
    )
    fm = FastMail(conf)
    await fm.send_message(message)


async def send_reset_password_email(to_email: str, reset_link: str):
    message = MessageSchema(
        subject="Password Reset Request",
        recipients=[to_email],
        body=f"""
        Hello,

        We received a request to reset your password. 
        Click the link below to reset it:

        {reset_link}

        If you did not request this, please ignore this email.

        This link will expire in 15 minutes.
        """,
        subtype="plain"
    )
    fm = FastMail(conf)
    await fm.send_message(message)