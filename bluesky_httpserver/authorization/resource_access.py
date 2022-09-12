from ._defaults import _DEFAULT_RESOURCE_ACCESS_GROUP


class DefaultResourceAccessControl:
    def __init__(self, *, default_group=_DEFAULT_RESOURCE_ACCESS_GROUP):
        self._default_group = default_group

    def get_principal_group(self, username):
        return self._default_group
