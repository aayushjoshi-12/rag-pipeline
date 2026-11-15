"""The fixed permission catalogue and the built-in roles.

Permissions are defined in code (a route can only check a permission that exists); roles are
data: admins compose them from this catalogue.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    DOCUMENTS_DELETE = "documents:delete"
    JOBS_READ = "jobs:read"
    CHAT_USE = "chat:use"
    SEARCH_USE = "search:use"
    TRACES_READ = "traces:read"
    USERS_MANAGE = "users:manage"
    ROLES_MANAGE = "roles:manage"
    APIKEYS_MANAGE = "apikeys:manage"
    CACHE_MANAGE = "cache:manage"


DESCRIPTIONS: dict[Permission, str] = {
    Permission.DOCUMENTS_READ: "List and view documents and their chunks",
    Permission.DOCUMENTS_WRITE: "Upload and replace documents",
    Permission.DOCUMENTS_DELETE: "Delete documents",
    Permission.JOBS_READ: "View ingest job status",
    Permission.CHAT_USE: "Ask questions (chat)",
    Permission.SEARCH_USE: "Search documents and get ranked passages (no LLM)",
    Permission.TRACES_READ: "View request traces and usage statistics",
    Permission.USERS_MANAGE: "Create, edit and deactivate users",
    Permission.ROLES_MANAGE: "Create, edit and delete roles",
    Permission.APIKEYS_MANAGE: "Create, list and revoke API keys",
    Permission.CACHE_MANAGE: "Purge the query cache",
}

ADMIN_ROLE = "admin"
USER_ROLE = "user"
VIEWER_ROLE = "viewer"

#: name -> (description, permissions). ``admin`` is always re-synced to the full catalogue.
BUILTIN_ROLES: dict[str, tuple[str, frozenset[Permission]]] = {
    ADMIN_ROLE: ("Full access", frozenset(Permission)),
    USER_ROLE: (
        "Upload documents and chat",
        frozenset(
            {
                Permission.DOCUMENTS_READ,
                Permission.DOCUMENTS_WRITE,
                Permission.JOBS_READ,
                Permission.CHAT_USE,
                Permission.SEARCH_USE,
            }
        ),
    ),
    VIEWER_ROLE: (
        "Read documents and chat",
        frozenset(
            {
                Permission.DOCUMENTS_READ,
                Permission.JOBS_READ,
                Permission.CHAT_USE,
                Permission.SEARCH_USE,
            }
        ),
    ),
}


#: Never granted to an API key principal even if its role has them: a leaked machine credential must not
#: be able to mint keys, users or roles (privilege persistence / escalation). Humans manage; keys consume.
KEY_FORBIDDEN: frozenset[str] = frozenset(
    {Permission.USERS_MANAGE.value, Permission.ROLES_MANAGE.value, Permission.APIKEYS_MANAGE.value}
)


def is_valid_permission(value: str) -> bool:
    return value in {p.value for p in Permission}
