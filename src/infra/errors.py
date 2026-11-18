"""Bounded failure metadata without exception messages, arguments or locals."""

from pathlib import Path


class LocalModelUnavailable(RuntimeError):
    """A local model could not load, or is in its retry cooldown."""


def tool_error_code(error: Exception) -> str:
    if isinstance(error, LocalModelUnavailable):
        return "local_model_unavailable"
    if isinstance(error, TimeoutError):
        return "tool_timeout"
    if isinstance(error, ConnectionError):
        return "tool_connection_failed"
    if isinstance(error, OSError):
        return "tool_io_failed"
    return "tool_execution_failed"


def exception_details(error: Exception) -> dict:
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name, "function": code.co_name,
                       "line": traceback.tb_lineno})
        traceback = traceback.tb_next
    cause = error.__cause__
    return {
        "exception_type": type(error).__name__,
        "cause_type": type(cause).__name__ if cause else None,
        "cause_errno": getattr(cause, "errno", None),
        "cause_winerror": getattr(cause, "winerror", None),
        "errno": getattr(error, "errno", None),
        "winerror": getattr(error, "winerror", None),
        "frames": frames[-6:],
    }
