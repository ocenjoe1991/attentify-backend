from fastapi import HTTPException, status

ROLE_COMPANY_OWNER = "company_owner"
ROLE_STORE_OWNER = "store_owner"
ROLE_AGENT = "agent"
ROLE_READONLY = "readonly"
ROLE_ADMIN = "admin"

OWNER_ROLES = {ROLE_COMPANY_OWNER, ROLE_STORE_OWNER}
MANAGER_ROLES = {ROLE_ADMIN, ROLE_COMPANY_OWNER}

PERMISSION_PERMANENT_DELETE_TICKET = "permanent_delete_ticket"
PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL = "resolve_ticket_without_owner_approval"
PERMISSION_REFUND_WITHOUT_OWNER_APPROVAL = "process_refund_without_owner_approval"
PERMISSION_CANCELLATION_WITHOUT_OWNER_APPROVAL = "process_cancellation_without_owner_approval"

VALID_CUSTOM_PERMISSIONS = {
    PERMISSION_PERMANENT_DELETE_TICKET,
    PERMISSION_RESOLVE_WITHOUT_OWNER_APPROVAL,
    PERMISSION_REFUND_WITHOUT_OWNER_APPROVAL,
    PERMISSION_CANCELLATION_WITHOUT_OWNER_APPROVAL,
}

ROLE_PERMISSIONS = {
    ROLE_ADMIN: {
        "manage_members",
        "manage_stores",
        "manage_tickets",
        "manage_system_configuration",
        "view_reports",
    },
    ROLE_COMPANY_OWNER: {
        "manage_members",
        "manage_stores",
        "manage_tickets",
        "manage_system_configuration",
        "view_reports",
    },
    ROLE_STORE_OWNER: {
        "manage_stores",
        "manage_store_tickets",
        "comment_on_tickets",
        "view_agent_activity",
        "view_store_reports",
    },
    ROLE_AGENT: {
        "handle_assigned_tickets",
        "escalate_ticket",
        "view_basic_reports",
    },
    ROLE_READONLY: {
        "view_tickets",
    },
}


def normalize_custom_permissions(permissions):
    if permissions is None:
        return []
    if not isinstance(permissions, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Custom permissions must be a list",
        )

    cleaned_permissions = []
    for permission in permissions:
        if permission not in VALID_CUSTOM_PERMISSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid custom permission",
            )
        if permission not in cleaned_permissions:
            cleaned_permissions.append(permission)
    return cleaned_permissions


def has_custom_permission(membership: dict | None, permission: str) -> bool:
    return permission in (membership or {}).get("custom_permissions", [])


def role_has_permission(role: str | None, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role or "", set())


def membership_has_role_permission(membership: dict | None, permission: str) -> bool:
    return role_has_permission((membership or {}).get("role"), permission)


def require_role_permission(membership: dict | None, permission: str):
    if not membership_has_role_permission(membership, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role permissions",
        )


def has_owner_approval_bypass(membership: dict | None, permission: str) -> bool:
    role = (membership or {}).get("role")
    return role == ROLE_COMPANY_OWNER or has_custom_permission(membership, permission)


def can_permanently_delete_ticket(membership: dict | None) -> bool:
    role = (membership or {}).get("role")
    return role in OWNER_ROLES or has_custom_permission(
        membership,
        PERMISSION_PERMANENT_DELETE_TICKET,
    )
