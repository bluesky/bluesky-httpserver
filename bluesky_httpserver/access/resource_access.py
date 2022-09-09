class FixedResourceAccessControl:
    def __init__(self, *, default_group):
        self._default_group = default_group

    def get_principal_group(self, principal):
        return self._default_group
