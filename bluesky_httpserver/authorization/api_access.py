import copy
from collections.abc import Iterable
import jsonschema
import yaml

from ._defaults import _DEFAULT_ROLES, _DEFAULT_USER_INFO
from ..config_schemas.loading import ConfigError

_schema_BasicAPIAccessControl = """
$schema": http://json-schema.org/draft-07/schema#
type: object
additionalProperties: false
properties:
  roles:
    oneOf:
      - type: object
        additionalProperties: false
        patternProperties:
          "^[a-zA-Z_][0-9a-zA-Z_]*$":
            oneOf:
              - type: object
                additionalProperties: false
                properties:
                  scopes_set:
                    $ref: '#/components/schemas/scope_list'
                  scopes_add:
                    $ref: '#/components/schemas/scope_list'
                  scopes_remove:
                    $ref: '#/components/schemas/scope_list'
              - type: "null"
      - type: "null"
components:
  schemas:
    scope_list:
      oneOf:
        - type: array
          items:
            type: string
            pattern: "^[0-9a-zA-Z:_]+$"
        - type: string
          pattern: "^[0-9a-zA-Z:_]+$"
        - type: "null"
"""


class BasicAPIAccessControl:
    """
    Basic API access policy. The policy is used by HTTP server
    by default. The basic policy supports two default users: ``UNAUTHENTICATED_SINGLE_USER``
    (user authorized with single-user API key) and ``UNAUTHENTICATED_PUBLIC`` (unauthorized user,
    sending no token or API key with the request). The default users are assigned to
    ``unauthenticated_single_user`` and ``unauthenticated_public`` roles respectively.
    In additon, five roles are defined by the policy and available to subclassed policies:
    ``admin``, ``expert``, ``advanced``, ``user`` and ``observer``.

    Each of the seven roles is assigned a reasonable set of default scopes, which may be
    customized for practical deployments. The parameter ``roles`` allows to replace or modify
    sets of scopes assigned to default roles or add new custom roles recognized by policies.
    Note, that the basic API access policy support only unauthenticated access and uses
    only two roles. The other default roles and custom roles are intended for use in subclasses
    that define more sophysticated policies.

    The optional parameter ``roles`` accepts a dictionary, which maps role names to operations
    on the respective sets of scopes. The operations include ``scopes_set`` (replaces the existing
    scopes by the new scopes or sets scopes for a new custom role), ``scopes_add`` (adds
    scopes to the existing scopes) and ``scopes_remove`` (remove scopes from the set).
    Multiple operations for a given role are executed in the following order: ``scopes_set``,
    ``scopes_add`` followed by ``scopes_remove``.

    The following examples illustrate how modify API access for the default ``user`` role.
    Access to API may be disabled by mapping the role name to ``None`` or setting scopes to the
    empty list::

      # 'user' can not access any API.
      {"user": None}
      {"user": {"scopes_set": []}}
      {"user": {"scopes_set": None}}

    In the following examples, the scopes are not changed, since no operations are specified or
    the operations do not modify the scopes::

      # Scopes are not changed
      {"user": {}}
      {"user": {"scopes_add": []}}
      {"user": {"scopes_add": None}}
      {"user": {"scopes_remove": []}}
      {"user": {"scopes_remove": None}}
      {"user": {"scopes_add": [], "scopes_remove": []}}
      {"user": {"scopes_add": None, "scopes_remove": None}}

    The scopes are replaces by specifying a list of scopes with ``scopes_set``::

      # Replace the scopes with the new set: 'user' can now only read status and the queue.
      {"user": {"scopes_set": ["read:status", "read:queue"]}}

      # Replace the scopes: 'user' can now only read status.
      {"user": {"scopes_set": ["read:status"]}}
      # A single scope may be specified as a string (more convenient in YML config file).
      {"user": {"scopes_set": "read:status"}}

    Additional scopes can be added to the default scopes::

      # In addition to default scopes, 'user' can now upload and execute scripts.
      {"user": {"scopes_add": ["write:scripts"]}}
      {"user": {"scopes_add": "write:scripts"}}

    Scopes can be removed from the default scopes::

      # 'user' is assigned the default scopes except API for editing the queue.
      {"user": {"scopes_remove": ["write:queue:edit"]}}
      {"user": {"scopes_remove": "write:queue:edit"}}

    Multiple operations may be specified::

      # Now the 'user' can execute scripts, but not edit the queue.
      {"user": {"scopes_add": ["write:scripts"], "remove": ["write:queue:edit"]}}
      {"user": {"scopes_add": "write:scripts", "remove": "write:queue:edit"}}

    In practical deployments, the policy arguments are defined in config YML files.
    The following is an example of configuration that modifies the scopes for
    the ``user`` role and creates a new ``test_role``::

      api_access:
        policy: bluesky_httpserver.authorization:BasicAPIAccessControl
        args:
          roles:
            user:
              scopes_add: write:scripts
              scopes_remove:
                - write:queue:edit
                - read:queue:edit
            test_role:
              scopes_add:
                - read:status
                - read:queue
                - read:history
                - read:resources
                - read:config
                - read:monitor
                - read:console
                - read:lock
                - read:testing

    Parameters
    ----------
    roles: dict, None
        The dictionary that maps role names to operations that modify assigned role scopes.
        If ``None``, then the default roles with default unmodified scopes are used.

    """

    def __init__(self, *, roles=None):
        try:
            config = {"roles": roles}
            schema = yaml.safe_load(_schema_BasicAPIAccessControl)
            jsonschema.validate(instance=config, schema=schema)
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

    def is_user_known(self, username):
        """
        Performs quick check whether the user is known. In many cases it does not make sense to
        perform any further authorization steps if the user is unknown. If the user is known, but
        not assigned to any groups or assigned to groups with empty scopes, then the user still
        can not access any API.

        Parameters
        ----------
        username: str
            User name

        Returns
        -------
        boolean
            Indicates if the user is known (``True``) or not (``False``).
        """
        return self._is_user_known(username)

    def get_user_roles(self, username):
        """
        Returns a set of roles assigned to the user.

        Parameters
        ----------
        username: str
            User name

        Returns
        -------
        set(str)
            A set of roles assigned to the user. The set of roles is empty if the user is not found.
        """
        principal_info = self._collect_user_info(username)
        roles = principal_info.get("roles", [])
        if isinstance(roles, str):
            roles = [roles]
        return set(roles)

    def get_user_scopes(self, username):
        """
        Returns a set of scopes assigned to the user. The scopes are based on the user roles.

        Parameters
        ----------
        username: str
            User name

        Returns
        -------
        set(str)
            A set of scopes assigned to the user. The set of scopes is empty if the user is not found.
        """
        roles = self.get_user_roles(username)
        return self._collect_role_scopes(roles)

    def get_displayed_user_name(self, username):
        """
        Returns the displayed user name for the user. The displayed user name is assembled from
        ``username``, full 'displayed' user name and user's email. The formatting depends on
        the available data, i.e. if no additional data is available, then ``username`` is returned.
        If the user is not found, then ``username`` is returned. The following output is possible
        for the user *'jdoe'*::

          jdoe
          jdoe <jdoe@gmail.com>
          jdoe "John Doe"
          jdoe "John Doe <jdoe@gmail.com>"

        Parameters
        ----------
        username: str
            User name

        Returns
        -------
        str
            Formatted displayed user name.
        """
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

    def get_user_info(self, username):
        """
        Returns complete user information, including a set of roles, set of scopes and displayed user name.
        This operation is more efficient that getting those items one by one.

        Parameters
        ----------
        username: str
            User name

        Returns
        -------
        dict
            The dictionary with full user information. The keys: ``roles`` (see ``get_user_roles()``),
            ``scopes`` (see ``get_user_scopes()``) and ``displayed_name`` (see ``get_displayed_user_name()``).
        """
        roles = self.get_user_roles(username)
        scopes = self._collect_role_scopes(roles)
        displayed_name = self.get_displayed_user_name(username)
        return {"roles": roles, "scopes": scopes, "displayed_name": displayed_name}


_schema_DictionaryAPIAccessControl = """
$schema": http://json-schema.org/draft-07/schema#
type: object
additionalProperties: false
properties:
  roles:  # Detailed validation is performed elsewhere
    description: The value is passed to BasicAPIAccessControl object
  users:
    oneOf:
      - type: object
        additionalProperties: false
        patternProperties:
          "^[0-9a-zA-Z_]+$":
            oneOf:
              - type: object
                additionalProperties: false
                properties:
                  roles:
                    oneOf:
                      - type: array
                        items:
                          type: string
                          pattern: "^[a-zA-Z_][0-9a-zA-Z_]*$"
                      - type: string
                        pattern: "^[a-zA-Z_][0-9a-zA-Z_]*$"
                      - type: "null"
                  displayed_name:
                    oneOf:
                      - type: string
                        pattern: "^.+$"
                      - type: "null"
                  mail:
                    oneOf:
                      - type: string
                        pattern: "^.+@.+$"
                      - type: "null"
              - type: "null"
      - type: "null"
"""


class DictionaryAPIAccessControl(BasicAPIAccessControl):
    """
    Dictionary-based API access policy.
    Simple extension of ``BasicAPIAccessControl`` that provides an option to provide user information,
    including assigned roles, displayed name and email. The policy is primarily intended for use in demos
    and testing. Production deployments are expected to use more secure authorization policies.

    User information is passed using ``users`` parameter, which accepts a dictionary. If the parameter
    is ``None``, then no user information is passed to the policy and no users are allowed to access any API.
    The dictionary maps usernames to user information dictionaries, containing roles, displayed names (optional)
    and emails (optional). The policy arguments are specified as part of config YML files as illustrated
    in the following examples::

        # No users are allowed to access any API.
        api_access:
          policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
          args:
              users: None

        # User 'bob' is defined, but he is not allowed to use any API.
        api_access:
          policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
          args:
            users:
              bob: None

        # User 'bob' is assigned to 'admin' and 'expert' groups, 'jdoe' is assigned to the 'advanced' group.
        # Note: a single role may be represented as a list or a string.
        api_access:
          policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
          args:
            users:
              bob:
                roles:
                  - admin
                  - expert
                mail: bob@gmail.com
              jdoe:
                roles: advanced
                dislayed_name: Doe, John
                mail: jdoe@gmail.com

    The policy arguments may also include ``roles`` parameter, which is handled by ``BasicAPIAccessControl``.
    See docstring for ``BasicAPIAccessControl`` for more detailed information.

    Parameters
    ----------
    roles: dict or None
        The dictionary configuration parameters that modifies the default or create new roles. The parameter
        is passed to ``BasicAPIAccessControl``.
    users: dict or None
        The dictionary that maps user name to user information.
    """

    def __init__(self, *, roles=None, users=None):
        super().__init__(roles=roles)

        try:
            config = {"roles": roles, "users": users}
            schema = yaml.safe_load(_schema_DictionaryAPIAccessControl)
            jsonschema.validate(instance=config, schema=schema)
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
