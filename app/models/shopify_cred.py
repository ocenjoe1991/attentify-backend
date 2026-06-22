from typing import Optional
from bson import ObjectId
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class ShopifyCredBase(BaseModel):
    shop_url: str
    access_token: str
    status: Optional[str] = "connected"
    user_id: Optional[ObjectId] = Field(default=None, alias="user_id")
    company_id: Optional[ObjectId] = Field(default=None, alias="company_id"),
    webhook_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        allow_population_by_field_name = True
        json_encoders = {
            ObjectId: str
        }
