from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Dict, Any
from datetime import datetime
from bson import ObjectId
from app.utils.bson import PyObjectId

class ChatEntry(BaseModel):
    sender: str  # Now stores the actual sender (email, phone number, etc.)
    recipient: Optional[str] = None  # For email/SMS
    content: str
    title: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    channel: Optional[Literal["chat", "sms", "email", "voice"]] = None
    message_type: Optional[Literal["text", "html", "file", "voice", "system"]] = "text"
    metadata: Optional[Dict[str, Any]] = {}

class Comment(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId = Field(..., description="ID of the user who wrote the comment")
    content: str = Field(..., description="Comment text")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    status: Literal["Pending", "Resolved", "Awaiting Approval"] = "Pending"
    edited: Optional[bool] = False

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}

class Message(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId = Field(...)
    company_id: PyObjectId = Field(...)
    # Conversation/thread grouping key, e.g., Gmail threadId, SMS conversation ID, chat session ID
    thread_id: Optional[str] = None
    ticket: Optional[str] = None

    # Participants
    participants: List[str] = []  # All unique senders/recipients in the thread

    # For compatibility with old logic, keep these if needed:
    client: Optional[str] = None  # e.g. email or phone for chat initiator
    agent: Optional[str] = None
    session_id: Optional[str] = None

    started_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    status: Literal["Open", "Closed", "Pending", "Resolved", "Escalated", "Awaiting Approval", "Cancelled"] = "Open"
    trashed: bool = False
    channel: Literal["chat", "sms", "email", "voice"]
    title: Optional[str] = None

    messages: List[ChatEntry] = []
    ai_summary: Optional[str] = None
    tags: Optional[List[str]] = []
    resolved_by_ai: bool = False

    assigned_member_id: Optional[PyObjectId] = None  # ID of the team member assigned to this message

    comments: List[Comment] = []  # Comments on the message thread
    
    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}