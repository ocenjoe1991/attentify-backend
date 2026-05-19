from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from bson import ObjectId
from app.utils.bson import PyObjectId
from app.models.user import UserPublic
from app.models.company import CompanyInDB

class MembershipBase(BaseModel):
    role: Literal["company_owner", "store_owner", "agent", "readonly"]
    status: Literal["active", "invited", "suspended"] = "active"

class MembershipCreate(MembershipBase):
    user_id: PyObjectId
    company_id: PyObjectId


class MembershipInDB(MembershipCreate):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    joined_at: datetime
    last_used_at: datetime

    class Config:
        json_encoders = {ObjectId: str}
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

class MembershipPublic(MembershipInDB):
    user: Optional[UserPublic]
    company: Optional[CompanyInDB]

class UpdateMembershipRequest(BaseModel):
    membership_id: str = Field(..., description="MongoDB ObjectId of the membership")
    role: Optional[Literal["company_owner", "store_owner", "agent", "readonly"]] = None
    status: Optional[Literal["active", "invited", "suspended"]] = None