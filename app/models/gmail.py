from pydantic import BaseModel, EmailStr, Field, GetCoreSchemaHandler
from typing import Optional, Any
from bson import ObjectId
from datetime import datetime
from pydantic_core import core_schema
from app.utils.bson import PyObjectId

class GmailAccountBase(BaseModel):
    user_id: PyObjectId = Field(...)
    company_id: PyObjectId = Field(...)
    email: EmailStr
    access_token: str
    refresh_token: str
    token_type: Optional[str] = "Bearer"
    expires_at: datetime
    client_id: str
    client_secret: str
    status: Optional[str] = "connected"
    scope: Optional[str] = None
    token_issued_at: Optional[datetime] = None
    is_primary: Optional[bool] = False
    provider: Optional[str] = "google"
    history_id: Optional[str]
    store_id: Optional[PyObjectId] = Field(...)
    model_config = {
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }

class GmailAccountCreate(GmailAccountBase):
    id: str
    pass

class GmailAccountUpdate(BaseModel):
    access_token: Optional[str]
    refresh_token: Optional[str]
    token_type: Optional[str]
    expires_at: Optional[datetime]
    status: Optional[str]

class GmailAccountInDB(GmailAccountBase):
    id: str
    user_id: str
