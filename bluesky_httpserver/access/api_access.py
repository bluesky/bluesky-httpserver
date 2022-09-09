import copy
from ._defaults import _DEFAULT_ROLES


class DictionaryAPIAccessControl:
    def __init__(self, *, users):
        self._roles = copy.deepcopy(_DEFAULT_ROLES)
        self._user_info = copy.deepcopy(users)

    def _get_scopes(self, role):
        if "role" in self._roles:
            return self._roles["role"]
        else:
            raise KeyError(f"Failed to find the scopes for unknown role {role!r}.")

    def _get_principal_info(self, principal):
        if principal in self._user_info:
            return self._user_info[principal]
        else:
            raise KeyError(f"User {principal!r} is not found.")

    def get_role_scopes(self, roles):
        """
        'roles' is a role name (string) or a list of roles (list of strings).
        Returns a set of scopes.
        """
        if isinstance(roles, str):
            roles = [roles]
        scopes = set().union(*[self._get_scopes(_) for _ in roles])
        return scopes

    def get_principal_roles(self, principal):
        """
        Returns a list of roles for the principal
        """
        principal_info = self._get_principal_info(principal)

        if "roles" in principal_info:
            return principal_info["roles"]
        else:
            raise KeyError(f"No roles are specified for user {principal!r}.")

    def get_principal_displayed_name(self, principal):
        principal_info = self._get_principal_info(principal)
        mail = principal_info.get("mail", None) | principal_info.get("email", None)
        displayed_name = principal_info.get("displayed_name", None)
        if not mail and not displayed_name:
            return principal
        elif not displayed_name:
            return f"{principal} <{mail}>"
        elif not mail:
            return f'{principal} "{displayed_name}"'
        else:
            return f'{principal} "{displayed_name} <{mail}>"'
