class EchoError(Exception):
    """Base error with a user-facing message."""


class SourceError(EchoError):
    pass


class ConfigError(EchoError):
    pass


class LLMError(EchoError):
    pass
