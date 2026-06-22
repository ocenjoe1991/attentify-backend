"""Shared enumerations used across the application."""

from enum import Enum


class TicketStatus(str, Enum):
    OPEN = "Open"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "In Progress"
    PENDING = "Pending"
    RESOLVED = "Resolved"
    ESCALATED = "Escalated"
    AWAITING_APPROVAL = "Awaiting Approval"
    CANCELED = "Canceled"


class UserRole(str, Enum):
    ADMIN = "admin"
    COMPANY_OWNER = "company_owner"
    STORE_OWNER = "store_owner"
    AGENT = "agent"
    READONLY = "readonly"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"


class MessageChannel(str, Enum):
    CHAT = "chat"
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"


class OrderMatchStatus(str, Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    NOT_ORDER = "not_order"
    UNKNOWN = "unknown"
    POSSIBLE = "possible"


class InvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
