"""Bounded, nonsecret browser failure descriptions for durable task history."""

from __future__ import annotations

import concurrent.futures

from .batch_domain import FailureDiagnostic


class BrowserStageError(Exception):
    def __init__(self, stage: str, cause: Exception):
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


def safe_failure(stage: str, error: Exception | None = None, *, mode: str = "run") -> FailureDiagnostic:
    """Never copy exception text: browser exceptions may contain form values or refs."""
    if isinstance(error, BrowserStageError):
        stage, error = error.stage, error.cause
    if mode == "recovery" and not stage.startswith("recovery_"):
        stage = "recovery_" + stage
    if error is None:
        category, detail = "controller_failure", "Browser action could not be verified"
    elif isinstance(error, (TimeoutError, concurrent.futures.TimeoutError)) or (
            isinstance(error, RuntimeError) and str(error) == "managed browser operation timed out"):
        category, detail = "timeout", "Managed browser operation timed out"
    elif isinstance(error, RuntimeError) and str(error) == "managed browser capacity is full":
        category, detail = "capacity", "Managed browser capacity is full"
    elif isinstance(error, (LookupError, ConnectionError)):
        category, detail = "browser_unavailable", "Managed browser or page was unavailable"
    elif isinstance(error, PermissionError):
        category, detail = "target_unavailable", "Browser action target was unavailable"
    elif isinstance(error, ValueError):
        category, detail = "invalid_state", "Browser workflow rejected an invalid state"
    else:
        category, detail = "unexpected_exception", "Unexpected browser or controller error; private detail withheld"
    return FailureDiagnostic(stage[:40], category, detail[:120], mode)


_RECOVERABLE_PAGE_STAGES = frozenset({
    "observe_snapshot", "locate_control", "fill", "select_option",
    "select_option_search", "verify", "toggle", "upload", "page_advance",
})


def is_recoverable_page_timeout(diagnostic: FailureDiagnostic) -> bool:
    """A bounded current-page timeout leaves page state uncertain but may leave it usable."""
    return (diagnostic.mode == "run" and diagnostic.category == "timeout" and
            diagnostic.stage in _RECOVERABLE_PAGE_STAGES)
