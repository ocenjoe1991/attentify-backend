# token_utils.py
from itsdangerous import URLSafeTimedSerializer, BadData
from app.core.config import settings
from fastapi import HTTPException

serializer = URLSafeTimedSerializer(settings.SECRET_KEY)

def create_invitation_token(email: str, company_id: str, role: str):
    return serializer.dumps({"email": email, "company_id": company_id, "role": role})

def verify_invitation_token(token: str, max_age: int = 172800):
    try:
        return serializer.loads(token, max_age=max_age)
    except BadData:
        raise HTTPException(status_code=400, detail="Invalid or malformed invitation token")