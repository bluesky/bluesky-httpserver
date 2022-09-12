_DEFAULT_SCOPES_FULL_LIST = {
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
}

_DEFAULT_SCOPES_ADMIN = {
    "read:status",
    "admin:apikeys",
    "admin:read:principals",
    "admin:metrics",
}

_DEFAULT_SCOPES_EXPERT = {
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
}

_DEFAULT_SCOPES_USER = {
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
}

_DEFAULT_SCOPES_OBSERVER = {
    "read:status",
    "read:queue",
    "read:history",
    "read:resources",
    "read:config",
    "read:monitor",
    "read:console",
    "read:lock",
    "read:testing",
}

# User authorized with single-user API key
_DEFAULT_USERNAME_SINGLE_USER = "UNAUTHENTICATED_SINGLE_USER"
_DEFAULT_ROLE_SINGLE_USER = "unauthenticated_single_user_role"
_DEFAULT_SCOPES_SINGLE_USER = {
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
}

# Unauthenticated user
_DEFAULT_USERNAME_PUBLIC = "UNAUTHENTICATED_PUBLIC"
_DEFAULT_ROLE_PUBLIC = "unauthenticated_anonymous_role"
_DEFAULT_SCOPES_PUBLIC = {
    "read:status",
}

_DEFAULT_USER_INFO = {
    _DEFAULT_USERNAME_SINGLE_USER: {"roles": _DEFAULT_ROLE_SINGLE_USER},
    _DEFAULT_USERNAME_PUBLIC: {"roles": _DEFAULT_ROLE_PUBLIC},
}

_DEFAULT_ROLES = {
    "admin": _DEFAULT_SCOPES_ADMIN,
    "expert": _DEFAULT_SCOPES_EXPERT,
    "user": _DEFAULT_SCOPES_USER,
    "observer": _DEFAULT_SCOPES_OBSERVER,
    _DEFAULT_ROLE_SINGLE_USER: _DEFAULT_SCOPES_SINGLE_USER,
    _DEFAULT_ROLE_PUBLIC: _DEFAULT_SCOPES_PUBLIC,
}

_DEFAULT_RESOURCE_ACCESS_GROUP = "admin"
