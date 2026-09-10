"""Domain failures that callers must handle explicitly."""


class GenerationUnavailableError(RuntimeError):
    """All configured answer-generation providers failed."""


class IndexBuildError(RuntimeError):
    """An index candidate failed before it became active."""
