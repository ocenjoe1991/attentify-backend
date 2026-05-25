from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal, List
from datetime import datetime
from bson import ObjectId
from app.utils.bson import PyObjectId

class UserBase(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_login: Optional[datetime] = None
    auth_provider: Optional[str] = None

class UserCreate(UserBase):
    password: Optional[str] = None
    invitation_token: Optional[str] = None

class AdminUserCreate(UserCreate):
    role: Optional[Literal["admin", "company_owner", "store_owner", "agent", "readonly"]] = "readonly"
    status: Optional[Literal["active", "invited", "suspended"]] = "invited"
    team_id: Optional[str] = None

class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "company_owner", "store_owner", "agent", "readonly"]] = None
    status: Optional[Literal["active", "invited", "suspended"]] = None
    team_id: Optional[str] = None

class UserInDB(UserBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    hashed_password: str

    class Config:
        json_encoders = {ObjectId: str}
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

class UserPublic(BaseModel):
    id: PyObjectId = Field(alias="_id")
    email: str
    first_name: str
    last_name: str
    role: Optional[str] = None
    status: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    company_id: Optional[str] = None
    membership_id: Optional[str] = None
    last_login: Optional[datetime] = None

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
