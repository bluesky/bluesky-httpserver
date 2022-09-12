import copy
from ._defaults import _DEFAULT_ROLES, _DEFAULT_USER_INFO


class BasicAPIAccessControl:
    """
    Additional roles can be added or the scopes assigned to the existing roles can be modified
    by passing the dictionary as a value of the parameter ``roles`` to the object constructor.
    The dictionary keys are role names and the values are dictionaries with keys ``replace``,
    ``add`` and ``remove`` pointing to lists of existing scopes. The scopes in the lists are
    completely replacing the default set of scopes, added to the default set of scopes or
    removed from the default set of scopes. If the dictionary ``roles`` contains multiple keys,
    the operations of replacing, adding and removing scopes are executed in the listed order.

    # Replace the set of scopes with the new set: 'user' can now only read status and the queue.
    {"user": {"replace": ["read:status", "read:queue"]}}

    # In addition to default scopes, 'user' can now upload and execute scripts.
    {"user": {"add": ["write:scripts"]}}

    # 'user' is assigned the default scopes except API for editing the queue.
    {"user": {"remove": ["write:queue:edit"]}}

    # Now the 'user' can execute scripts, but not edit the queue.
    {"user": {"add": ["write:scripts"], "remove": ["write:queue:edit"]}}

    # 'user' can not access any API.
    {"user": None}
    {"user": {"replace": []}}
    {"user": {"replace": None}}

    # Scopes are not changed
    {"user": {}}
    {"user": {"add": None}}
    {"user": {"remove": None}}
    """

    def __init__(self, *, roles=None):
        roles = roles or {}

        self._roles = copy.deepcopy(_DEFAULT_ROLES)

        for role, params in roles.items():
            role_scopes = self._roles.setdefault(role, default=set())
            # If 'params' is None, then the role has no access (scopes is an empty set)
            if params is None:
                params = {"scopes_replace": []}
            if "scopes_replace" in params:
                role_scopes.clear()
                role_scopes.update([_.lower() for _ in (params["scopes_replace"] or [])])
            if "scopes_add" in params:
                role_scopes.update([{_.lower() for _ in (params["scopes_add"] or [])}])
            if "scopes_remove" in params:
                for scope in [_.lower() for _ in (params["scopes_remove"] or [])]:
                    role_scopes.discard(scope)

        self._user_info = copy.deepcopy(_DEFAULT_USER_INFO)

    def _collect_scopes(self, role):
        """
        Returns an empty set if the role is not defined.
        """
        print(f"roles={self._roles}")  ##
        return self._roles.get(role, set())

    def _collect_user_info(self, username):
        """
        Returns an empty dictionary if user data is found.
        """
        print(f"user_info={self._user_info}")  ##
        return self._user_info.get(username, {})

    def get_role_scopes(self, roles):
        """
        'roles' is a role name (string) or a list of roles (list of strings).
        Returns a set of scopes.
        """
        if isinstance(roles, str):
            scopes = self._collect_scopes(roles)
        else:
            scopes = set().union(*[self._collect_scopes(_) for _ in roles])
        return scopes

    def get_user_roles(self, username):
        """
        Returns a set of roles for the user
        """
        principal_info = self._collect_user_info(username)
        print(f"principal_info={principal_info!r}")  ##
        roles = principal_info.get("roles", [])
        if not isinstance(roles, str):
            roles = set(roles)
        return roles

    def get_displayed_user_name(self, username):
        user_info = self._collect_user_info(username)
        mail = user_info.get("mail", None) or user_info.get("email", None)
        displayed_name = user_info.get("displayed_name", None)
        if not mail and not displayed_name:
            return username
        elif not displayed_name:
            return f"{username} <{mail}>"
        elif not mail:
            return f'{username} "{displayed_name}"'
        else:
            return f'{username} "{displayed_name} <{mail}>"'

    def get_user_info(self, username):
        roles = self.get_user_roles(username)
        print(f"roles={roles!r}")  ##
        scopes = self.get_role_scopes(roles)
        print(f"scopes={scopes!r}")  ##
        displayed_name = self.get_displayed_user_name(username)
        print(f"displayed_name={displayed_name!r}")  ##
        return {"roles": roles, "scopes": scopes, "displayed_name": displayed_name}


class DictionaryAPIAccessControl(BasicAPIAccessControl):
    """
    ``users`` is a dictionary with the following keys: ``roles`` - a role name (str)
    or a list of roles (list of str), ``displayed_name`` - displayed name, e.g. 'John Doe' (str, optional),
    ``mail`` or ``email`` - email (str, optional). If the list of roles is missing or empty, then
    the user has no access to any API.
    """

    def __init__(self, *, roles=None, users=None):
        super().__init__(roles=roles)
        users = users or {}
        user_info = copy.deepcopy(users)
        for v in user_info.values():
            v.setdefault("roles", [])
            if isinstance(v["roles"], str):
                v["roles"] = v["roles"].lower()
            else:
                v["roles"] = [_.lower() for _ in v["roles"]]
        self._user_info.update(user_info)
