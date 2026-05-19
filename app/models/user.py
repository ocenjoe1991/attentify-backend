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
    auth_provider: Optional[str]

class UserCreate(UserBase):
    password: str
    invitation_token: Optional[str] = None

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
    last_login: Optional[datetime] = None

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


