"""Narrow trusted-URL handoff from a task to its managed browser."""

from __future__ import annotations

from urllib.parse import urlsplit

from .batch_domain import ApplicationTask, JobListing
from .dedupe import canonicalize_url
from .domain import ApplicationObservation
from .managed_runtime import MCPManagedWindows


def application_url(listing: JobListing) -> str:
    url = canonicalize_url(listing.application_url)
    if url is None:
        raise ValueError("application URL is required")
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and
                                         parts.hostname in {"localhost", "127.0.0.1"}):
        raise ValueError("application URL must use HTTPS or local loopback HTTP")
    return url


class ApplicationLauncher:
    def __init__(self, windows: MCPManagedWindows):
        self.windows = windows

    def open(self, task: ApplicationTask, listing: JobListing,
             window_id: str) -> ApplicationObservation:
        if self.windows.window_for_task(task.id) != window_id:
            raise LookupError("task/window association is unavailable")
        url = application_url(listing)
        browser = self.windows.browser_for(window_id)
        if self.windows.opened_url(window_id) is None:
            self.windows.run_browser(window_id, browser.navigate(url))
            self.windows.mark_opened(window_id, url)
            # Establishing the process-local page marker changes browser state.
            # The worker receives only an observation made afterward.
            return self.windows.run_browser(window_id, browser.observe())
        # Resume/reconstruction never replays old page refs or navigates over
        # a human's in-progress page. Observe the live window again.
        return self.windows.run_browser(window_id, browser.observe())
