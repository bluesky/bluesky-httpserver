class SERVER_RESOURCES:
    _RM = None
    _custom_code_modules = []
    _console_output_loader = None

    def __init__(self):
        raise RuntimeError("SERVER_RESOURCES class should not be instantiated")

    @classmethod
    def set_RM(cls, RM):
        cls._RM = RM

    @classmethod
    @property
    def RM(cls):
        return cls._RM

    @classmethod
    def set_custom_code_modules(cls, custom_code_modules):
        cls._custom_code_modules = custom_code_modules

    @classmethod
    @property
    def custom_code_modules(cls):
        return cls._custom_code_modules

    @classmethod
    def set_console_output_loader(cls, console_output_loader):
        cls._console_output_loader = console_output_loader

    @classmethod
    @property
    def console_output_loader(cls):
        return cls._console_output_loader
