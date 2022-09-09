_DEFAULT_SCOPES_FULL_LIST = [
    "read:status",
    "read:queue",
    "read:history",
    "read:resources",
    "read:config",
    "read:monitor",
    "read:console",
    "read:lock",
    "read:testing",
    "write:queue:edit",
    "write:queue:control",
    "write:manager:control",
    "write:plan:control",
    "write:execute",
    "write:history:edit",
    "write:permissions",
    "write:scripts",
    "write:config",
    "write:lock",
    "write:manager_stop",
    "write:testing",
    "admin:apikeys",
    "admin:read:principals",
    "admin:metrics",
]

_DEFAULT_SCOPES_ADMIN = [
    "read:status",
    "admin:apikeys",
    "admin:read:principals",
    "admin:metrics",
]

_DEFAULT_SCOPES_EXPERT = [
    "read:status",
    "read:queue",
    "read:history",
    "read:resources",
    "read:config",
    "read:monitor",
    "read:console",
    "read:lock",
    "read:testing",
    "write:queue:edit",
    "write:queue:control",
    "write:manager:control",
    "write:plan:control",
    "write:execute",
    "write:history:edit",
    "write:permissions",
    "write:scripts",
    "write:config",
    "write:lock",
]

_DEFAULT_SCOPES_USER = [
    "read:status",
    "read:queue",
    "read:history",
    "read:resources",
    "read:config",
    "read:monitor",
    "read:console",
    "read:lock",
    "read:testing",
    "write:queue:edit",
    "write:queue:control",
    "write:manager:control",
    "write:plan:control",
    "write:execute",
    "write:history:edit",
]

_DEFAULT_SCOPES_OBSERVER = [
    "read:status",
    "read:queue",
    "read:history",
    "read:resources",
    "read:config",
    "read:monitor",
    "read:console",
    "read:lock",
    "read:testing",
]

_DEFAULT_SCOPES_UNAUTHENTICATED_ADMIN = ("read:status",)
_DEFAULT_SCOPES_UNAUTHENTICATED_USER = _DEFAULT_SCOPES_FULL_LIST

_DEFAULT_ROLES = {
    "admin": _DEFAULT_SCOPES_ADMIN,
    "expert": _DEFAULT_SCOPES_EXPERT,
    "user": _DEFAULT_SCOPES_USER,
    "observer": _DEFAULT_SCOPES_OBSERVER,
    "unauthenticated_admin": _DEFAULT_SCOPES_UNAUTHENTICATED_ADMIN,
    "unauthenticated_user": _DEFAULT_SCOPES_UNAUTHENTICATED_USER,
}
