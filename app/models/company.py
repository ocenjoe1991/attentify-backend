from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime
from bson import ObjectId
from app.utils.bson import PyObjectId

class CompanyBase(BaseModel):
    name: str
    site_url: str
    email: EmailStr

class CompanyCreate(CompanyBase):
    pass

class CompanyInDB(CompanyBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    created_by: PyObjectId
    created_at: datetime

    class Config:
        json_encoders = {ObjectId: str}
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

class SimpleCompanyOut(BaseModel):
    id: str
    name: str

    class Config:
        json_encoders = {ObjectId: str}
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

class UpdateCompanyRequest(BaseModel):
    company_id: str = Field(..., description="MongoDB ObjectId of the company")
    name: Optional[str] = None
    site_url: Optional[str] = None
    email: Optional[EmailStr] = None