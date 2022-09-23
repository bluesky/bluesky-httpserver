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

    def get_resource_group(self, username):
        return self._default_group
