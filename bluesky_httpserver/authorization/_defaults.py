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
    "write:manager:stop",
    "write:testing",
    "user:apikeys",
    "admin:apikeys",
    "admin:read:principals",
    "admin:metrics",
}

_DEFAULT_SCOPES_ADMIN = {
    "read:status",
    "user:apikeys",
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
    "user:apikeys",
}

_DEFAULT_SCOPES_ADVANCED = {
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

# =============================================================================================
#                       DEFAULT USERS FOR SUPPORT OF ANONYMOUS ACCESS

# Unique provider name used to distinguish anonymous users, which are not authenticated
_DEFAULT_ANONYMOUS_PROVIDER_NAME = "__anonymous__"

# User authorized with single-user API key
_DEFAULT_USERNAME_SINGLE_USER = "UNAUTHENTICATED_SINGLE_USER"
_DEFAULT_ROLE_SINGLE_USER = "unauthenticated_single_user"
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
    "write:manager:stop",
    "write:testing",
    "user:apikeys",
}

# Unauthenticated user
_DEFAULT_USERNAME_PUBLIC = "UNAUTHENTICATED_PUBLIC"
_DEFAULT_ROLE_PUBLIC = "unauthenticated_public"
_DEFAULT_SCOPES_PUBLIC = {
    "read:status",
}

_DEFAULT_USER_INFO = {
    _DEFAULT_USERNAME_SINGLE_USER: {"roles": _DEFAULT_ROLE_SINGLE_USER},
    _DEFAULT_USERNAME_PUBLIC: {"roles": _DEFAULT_ROLE_PUBLIC},
}

_DEFAULT_ROLE_ADMIN = "admin"
_DEFAULT_ROLE_EXPERT = "expert"
_DEFAULT_ROLE_ADVANCED = "advanced"
_DEFAULT_ROLE_USER = "user"
_DEFAULT_ROLE_OBSERVER = "observer"


_DEFAULT_ROLES = {
    _DEFAULT_ROLE_ADMIN: _DEFAULT_SCOPES_ADMIN,
    _DEFAULT_ROLE_EXPERT: _DEFAULT_SCOPES_EXPERT,
    _DEFAULT_ROLE_ADVANCED: _DEFAULT_SCOPES_ADVANCED,
    _DEFAULT_ROLE_USER: _DEFAULT_SCOPES_USER,
    _DEFAULT_ROLE_OBSERVER: _DEFAULT_SCOPES_OBSERVER,
    _DEFAULT_ROLE_SINGLE_USER: _DEFAULT_SCOPES_SINGLE_USER,
    _DEFAULT_ROLE_PUBLIC: _DEFAULT_SCOPES_PUBLIC,
}

# =====================================================================================
#                     ACCESS TO RESOURCES (PLANS AND DEVICES)

_DEFAULT_RESOURCE_ACCESS_GROUP = "primary"
