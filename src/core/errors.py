"""Domain failures that callers must handle explicitly."""


class GenerationUnavailableError(RuntimeError):
    """All configured answer-generation providers failed."""
