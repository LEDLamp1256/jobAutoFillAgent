"""Narrow trusted-URL handoff from a task to its managed browser."""

from __future__ import annotations

from urllib.parse import urlsplit

from .batch_domain import ApplicationTask, JobListing
from .dedupe import canonicalize_url
from .domain import ApplicationObservation
from .failure_diagnostics import BrowserStageError
from .managed_runtime import MCPManagedWindows


def application_url(listing: JobListing) -> str:
    url = listing.application_url
    if url is None:
        raise ValueError("application URL is required")
    canonicalize_url(url)
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" and not (parts.scheme.lower() == "http" and
            parts.hostname in {"localhost", "127.0.0.1"}):
        raise ValueError("application URL must use HTTPS or loopback HTTP")
    return url


class ApplicationLauncher:
    def __init__(self, windows: MCPManagedWindows):
        self.windows = windows

    def open(self, task: ApplicationTask, listing: JobListing,
             window_id: str) -> ApplicationObservation:
        if self.windows.window_for_task(task.id) != window_id:
            raise LookupError("task/window association is unavailable")
        url = application_url(listing)
        try:
            browser = self.windows.browser_for(window_id)
        except Exception as exc:
            raise BrowserStageError("locate_browser", exc) from None
        if self.windows.opened_url(window_id) is None:
            try:
                self.windows.run_browser(window_id, browser.navigate(url))
            except Exception as exc:
                raise BrowserStageError("navigate", exc) from None
            try:
                self.windows.mark_opened(window_id, url)
            except Exception as exc:
                raise BrowserStageError("managed_page_marker", exc) from None
            # Establishing the process-local page marker changes browser state.
            # The worker receives only an observation made afterward.
            try:
                return self.windows.run_browser(window_id, browser.observe())
            except Exception as exc:
                raise BrowserStageError("observe_snapshot", exc) from None
        # Resume/reconstruction never replays old page refs or navigates over
        # a human's in-progress page. Observe the live window again.
        try:
            return self.windows.run_browser(window_id, browser.observe())
        except Exception as exc:
            raise BrowserStageError("observe_snapshot", exc) from None
