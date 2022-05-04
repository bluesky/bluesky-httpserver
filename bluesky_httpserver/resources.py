class _ServerResources:
    def __init__(self):
        self._RM = None
        self._custom_code_modules = []
        self._console_output_loader = None

    def set_RM(self, RM):
        self._RM = RM

    @property
    def RM(self):
        return self._RM

    @RM.setter
    def RM(self, _):
        raise RuntimeError("Attempting to set read-only property 'RM'")

    def set_custom_code_modules(self, custom_code_modules):
        self._custom_code_modules = custom_code_modules

    @property
    def custom_code_modules(self):
        return self._custom_code_modules

    @custom_code_modules.setter
    def custom_code_modules(self, _):
        raise RuntimeError("Attempting to set read-only property 'custom_code_modules'")

    def set_console_output_loader(self, console_output_loader):
        self._console_output_loader = console_output_loader

    @property
    def console_output_loader(self):
        return self._console_output_loader

    @console_output_loader.setter
    def console_output_loader(self, _):
        raise RuntimeError("Attempting to set read-only property 'console_output_loader'")


SERVER_RESOURCES = _ServerResources()
