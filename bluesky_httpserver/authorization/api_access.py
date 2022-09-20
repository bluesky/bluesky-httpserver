import copy
from collections.abc import Iterable
import jsonschema

from ._defaults import _DEFAULT_ROLES, _DEFAULT_USER_INFO
from ..config_schemas.loading import load_schema_from_yml, ConfigError


class BasicAPIAccessControl:
    """
    Additional roles can be added or the scopes assigned to the existing roles can be modified
    by passing the dictionary as a value of the parameter ``roles`` to the object constructor.
    The dictionary keys are role names and the values are dictionaries with keys ``set``,
    ``add`` and ``remove`` pointing to lists of existing scopes. The scopes in the lists are
    completely replacing the default scopes (``set``), added to the default set of scopes (``add``)
    or removed from the default set of scopes (``remove``). If the dictionary ``roles`` contains
    multiple keys, the operations of replacing, adding and removing scopes are executed in the listed order.

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
    {"user": {"set": []}}
    {"user": {"set": None}}

    # Scopes are not changed
    {"user": {}}
    {"user": {"add": None}}
    {"user": {"remove": None}}
    """

    def __init__(self, *, roles=None):
        try:
            config = {"roles": roles}
            schema_file_name = "basic_api_access_control_config.yml"
            jsonschema.validate(instance=config, schema=load_schema_from_yml(schema_file_name))
        except jsonschema.ValidationError as err:
            msg = err.args[0]
            raise ConfigError(f"ValidationError while validating parameters BasicAPIAccessControl: {msg}") from err

        roles = roles or {}
        self._roles = copy.deepcopy(_DEFAULT_ROLES)

        for role, params in roles.items():
            role_scopes = self._roles.setdefault(role, set())
            # If 'params' is None, then the role has no access (scopes is an empty set)
            if params is None:
                params = {"scopes_set": []}
            if "scopes_set" in params:
                role_scopes.clear()
                role_scopes.update(self._create_scope_list(params["scopes_set"]))
            if "scopes_add" in params:
                role_scopes.update(self._create_scope_list(params["scopes_add"]))
            if "scopes_remove" in params:
                scopes_list = self._create_scope_list(params["scopes_remove"])
                for scope in scopes_list:
                    role_scopes.discard(scope)

        self._user_info = copy.deepcopy(_DEFAULT_USER_INFO)

    def _create_scope_list(self, scopes):
        if isinstance(scopes, str):
            return [scopes.lower()]
        elif isinstance(scopes, Iterable):
            return [_.lower() for _ in scopes]
        elif not scopes:
            return []
        else:
            raise TypeError(f"Unsupported type of scope list: scopes = {scopes!r}")

    def _is_user_known(self, username):
        return username in self._user_info

    def _collect_scopes(self, role):
        """
        Returns an empty set if the role is not defined.
        """
        return self._roles.get(role, set())

    def _collect_user_info(self, username):
        """
        Returns an empty dictionary if user data is found.
        """
        return self._user_info.get(username, {})

    def _collect_role_scopes(self, roles):
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
        roles = principal_info.get("roles", [])
        if isinstance(roles, str):
            roles = [roles]
        return set(roles)

    def get_user_scopes(self, username):
        """
        Returns a set of scopes for the user
        """
        roles = self.get_user_roles(username)
        return self._collect_role_scopes(roles)

    def get_displayed_user_name(self, username):
        user_info = self._collect_user_info(username)
        mail = user_info.get("mail", None)
        displayed_name = user_info.get("displayed_name", None)
        if not mail and not displayed_name:
            return username
        elif not displayed_name:
            return f"{username} <{mail}>"
        elif not mail:
            return f'{username} "{displayed_name}"'
        else:
            return f'{username} "{displayed_name} <{mail}>"'

    def authorize(self, username):
        return self._is_user_known(username)

    def get_user_info(self, username):
        roles = self.get_user_roles(username)
        scopes = self._collect_role_scopes(roles)
        displayed_name = self.get_displayed_user_name(username)
        return {"roles": roles, "scopes": scopes, "displayed_name": displayed_name}


class DictionaryAPIAccessControl(BasicAPIAccessControl):
    """
    ``users`` is a dictionary with the following keys: ``roles`` - a role name (str)
    or a list of roles (list of str), ``displayed_name`` - displayed name, e.g. 'John Doe' (str, optional),
    ``mail`` - email (str, optional). If the list of roles is missing or empty, then
    the user has no access to any API.
    """

    def __init__(self, *, roles=None, users=None):
        super().__init__(roles=roles)

        try:
            config = {"roles": roles, "users": users}
            schema_file_name = "dictionary_api_access_control_config.yml"
            jsonschema.validate(instance=config, schema=load_schema_from_yml(schema_file_name))
        except jsonschema.ValidationError as err:
            msg = err.args[0]
            raise ConfigError(f"ValidationError while validating parameters BasicAPIAccessControl: {msg}") from err

        users = users or {}
        user_info = copy.deepcopy(users)
        for k in user_info:
            if user_info[k] is None:
                user_info[k] = {}
            else:
                user_info[k] = dict(user_info[k])
        for v in user_info.values():
            v.setdefault("roles", [])
            if v["roles"] is None:
                v["roles"] = []
            if isinstance(v["roles"], str):
                v["roles"] = v["roles"].lower()
            else:
                v["roles"] = [_.lower() for _ in v["roles"]]
        self._user_info.update(user_info)
