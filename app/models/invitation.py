# models/invitation.py
from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from typing import Literal
from bson import ObjectId
from app.utils.bson import PyObjectId

class InvitationBase(BaseModel):
    email: EmailStr
    company_id: PyObjectId
    role: Literal["company_owner", "store_owner", "agent", "readonly"]

class InvitationDetails(BaseModel):
    email: str
    company_id: str
    role: str
    expires_at: datetime

class AcceptInvitationRequest(BaseModel):
    token: str = Field(..., description="Invitation token received via email")

class InvitationInDB(InvitationBase):
    id: PyObjectId
    token: str
    invited_at: datetime
    status: Literal["pending", "accepted", "expired", "cancelled"] = "pending"

    class Config:
        json_encoders = {ObjectId: str}
