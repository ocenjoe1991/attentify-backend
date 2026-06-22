"""Twilio service helpers for SMS and voice operations."""

import os
from twilio.rest import Client as TwilioClient


def get_twilio_client() -> TwilioClient:
    """Return an authenticated Twilio REST client."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token:
        raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


async def send_sms(to_number: str, body: str) -> dict:
    """Send an SMS message via Twilio."""
    client = get_twilio_client()
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "")
    if not from_number:
        raise ValueError("TWILIO_PHONE_NUMBER must be set")

    message = client.messages.create(
        body=body,
        from_=from_number,
        to=to_number,
    )
    return {
        "sid": message.sid,
        "status": message.status,
        "to": message.to,
        "from": message.from_,
    }
