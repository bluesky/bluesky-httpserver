import os
from pathlib import Path


class ConfigError(ValueError):
    pass


def load_schema_from_yml(file_name):
    "Load the schema for service-side configuration."
    import yaml

    here = Path(__file__).parent.absolute()
    schema_path = os.path.join(here, file_name)
    with open(schema_path, "r") as file:
        return yaml.safe_load(file)
