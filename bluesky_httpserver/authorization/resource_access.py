import jsonschema
import yaml

from ..config_schemas.loading import ConfigError
from ._defaults import _DEFAULT_RESOURCE_ACCESS_GROUP

_schema_DefaultResourceAccessControl = """
$schema": http://json-schema.org/draft-07/schema#
type: object
additionalProperties: false
properties:
  default_group:
    oneOf:
      - type: string
        pattern: "^[a-zA-Z_][0-9a-zA-Z_]*$"
      - type: "null"
"""


class DefaultResourceAccessControl:
    """
    Default resource access policy.
    The resource access policy associates users with user groups. The groups
    define the resources, such as plans and devices users can access. The
    default policy assumes that all uses belong to a singe group (default user group).
    The name of the group is returned by ``get_resource_name()`` method for any
    submitted ``username``. The hard-coded name for the default user group can be
    modified by passing the parameter ``default_group`` to the class constructor.
    The arguments of the class constructor are specified in the configuration
    file as shown in the example below.

    Parameters
    ----------
    default_group: str
        The name of the group returned by the access manager by default.

    Examples
    --------
    Configure ``DefaultResourceAccessControl`` policy to use different group
    name for all the users. The new default group name is ``test_user``.

    .. code-block::

        resource_access:
          policy: bluesky_httpserver.authorization:DefaultResourceAccessControl
          args:
            default_group: test_user
    """

    def __init__(self, *, default_group=None):
        try:
            config = {"default_group": default_group}
            schema = yaml.safe_load(_schema_DefaultResourceAccessControl)
            jsonschema.validate(instance=config, schema=schema)
        except jsonschema.ValidationError as err:
            msg = err.args[0]
            raise ConfigError(
                f"ValidationError while validating parameters DefaultResourceAccessControl: {msg}"
            ) from err

        default_group = default_group or _DEFAULT_RESOURCE_ACCESS_GROUP
        self._default_group = default_group

    def get_resource_group(self, username, group):
        """
        Returns the name of the user group based on the user name.

        Parameters
        ----------
        username: str
            User name.

        Returns
        -------
        str
            Name of the user group.
        """
        return self._default_group


class SingleGroupResourceAccessControl(DefaultResourceAccessControl):
    """
    Single group resource access policy.
    The resource access policy associates users with its correspondent first user group. 
    The groups define the resources, such as plans and devices users can access. The
    single group policy assumes that one user belong to a single group or if they are 
    unauthenticated or have authenticated with a single-user API key, it uses the default 
    user group.
    The arguments of the class constructor are the same as the one specified in the 
    DefaultResourceAccessControl configuration ile as shown in the example below.

    Parameters
    ----------
    default_group: str
        The name of the group returned by the access manager by default.

    Examples
    --------
    Configure ``SingleGroupResourceAccessControl`` policy. The default group name is 
    ``test_user``.

    .. code-block::

        resource_access:
          policy: bluesky_httpserver.authorization:SingleGroupResourceAccessControl
          args:
            default_group: test_user
    """

    def get_resource_group(self, username, group):
        """
        Returns the name of the user group based on the user name.

        Parameters
        ----------
        username: str
            User name.

        Returns
        -------
        str
            Name of the user group.
        """
        if isinstance(group, list):
            if group[0] in ["unauthenticated_public", "unauthenticated_single_user"]:
                return self.get_resource_group(username, group)
            return group[0]
        return group
