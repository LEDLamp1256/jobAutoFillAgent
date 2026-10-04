"""Live, process-local managed windows backed by isolated Playwright MCP sessions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .mcp_browser import MCPServerCommand, PlaywrightMCPAdapter


@dataclass
class _Window:
    task_id: str
    browser: PlaywrightMCPAdapter
    session: _Session
    artifacts: tempfile.TemporaryDirectory
    opened_url: str | None = None
    page_token: str | None = None
    available: bool = True
    closed: bool = False


class _Session:
    """All calls for one MCP adapter run in the task that entered its context."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.owner = asyncio.run_coroutine_threadsafe(self._serve(), self.loop)

    async def _serve(self) -> None:
        # AnyIO's MCP context must be entered, used, and exited in one task.
        while True:
            coroutine, result, settled = await self.queue.get()
            if coroutine is None:
                result.set_result(None)
                settled.set()
                return
            operation = asyncio.create_task(coroutine)
            result.add_done_callback(lambda done, active=operation: self.loop.call_soon_threadsafe(active.cancel)
                                     if done.cancelled() else None)
            try:
                value = await operation
            except BaseException as error:
                if not result.done():
                    result.set_exception(error)
            else:
                if not result.done():
                    result.set_result(value)
            finally:
                settled.set()

    def run(self, coroutine, *, timeout: float = 45):
        future = concurrent.futures.Future()
        settled = threading.Event()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (coroutine, future, settled))
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            if not settled.wait(2):
                raise RuntimeError("managed browser cancellation did not settle") from None
            raise RuntimeError("managed browser operation timed out") from None

    def close(self) -> None:
        try:
            self.run(None)
            self.owner.result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()


class MCPManagedWindows:
    """One isolated headed MCP browser per task; IDs have no meaning after restart."""

    def __init__(self, command: Callable[[], MCPServerCommand],
                 adapter_factory: Callable[[MCPServerCommand], PlaywrightMCPAdapter] = PlaywrightMCPAdapter):
        self._command = command
        self._adapter_factory = adapter_factory
        self._windows: dict[str, _Window] = {}
        self._by_task: dict[str, str] = {}

    def open_count(self) -> int:
        # An ambiguous session still consumes a managed browser slot. Counting
        # only validated tabs would silently make room for more windows.
        return len(self._windows)

    def window_for_task(self, task_id: str) -> str | None:
        return self._by_task.get(task_id)

    def passive_available(self, window_id: str) -> bool:
        """Advisory process-local state; no MCP query during a control-plane read."""
        window = self._windows.get(window_id)
        return bool(window and window.available and window.page_token)

    @staticmethod
    def _one_tab(tabs: str) -> bool:
        # MCP exposes mutable indices, not stable tab IDs. Any unexpected tab
        # makes the association ambiguous; never guess which tab is managed.
        return re.findall(r"(?m)^\s*-\s*(\d+):", tabs) == ["0"]

    def exists(self, window_id: str) -> bool:
        window = self._windows.get(window_id)
        if window is None or not window.available:
            return False
        try:
            tabs = window.session.run(window.browser.managed_tabs())
            if not self._one_tab(tabs):
                window.available = False
                window.closed = not re.findall(r"(?m)^\s*-\s*(\d+):", tabs)
                return False
            if (window.page_token is not None and not
                    window.session.run(window.browser.managed_page_token_matches(window.page_token))):
                window.available = False
                window.closed = "about:blank" in tabs.casefold()
                return False
            return True
        except Exception as exc:
            window.available = False
            # A dead page/server may be reopened from the durable URL. Other
            # failures remain ambiguous and require human inspection.
            detail = str(exc).casefold()
            window.closed = any(token in detail for token in (
                "browser has been closed", "target page, context or browser has been closed",
                "connection closed", "transport closed"))
            return False

    def recoverable_closed(self, window_id: str) -> bool:
        window = self._windows.get(window_id)
        return bool(window and window.closed)

    def allocate(self, task_id: str) -> str:
        existing = self._by_task.get(task_id)
        if existing and self.exists(existing):
            return existing
        if existing:
            self.release(existing)
        artifacts = tempfile.TemporaryDirectory(prefix="jobagent-v2-mcp-")
        directory = Path(artifacts.name).resolve()
        if directory.is_relative_to(Path(__file__).resolve().parents[1]):
            artifacts.cleanup()
            raise RuntimeError("MCP temporary directory must be outside the repository")
        try:
            command = self._command()
            # The pinned MCP defaults to writing page-*.yml and other output
            # beneath cwd. Explicitly bind both its workspace and output root
            # to this session's owned temporary directory.
            command = replace(command, cwd=directory,
                              args=(*command.args, "--output-dir", str(directory)))
            browser = self._adapter_factory(command)
            session = _Session()
        except BaseException:
            artifacts.cleanup()
            raise
        try:
            session.run(browser.__aenter__())
            tabs = session.run(browser.managed_tabs())
            if not self._one_tab(tabs):
                raise RuntimeError("managed browser does not have one unambiguous tab")
        except Exception:
            try:
                session.run(browser.close())
            except Exception:
                pass
            try:
                session.close()
            finally:
                artifacts.cleanup()
            raise
        window_id = uuid.uuid4().hex
        self._windows[window_id] = _Window(task_id, browser, session, artifacts)
        self._by_task[task_id] = window_id
        return window_id

    def release(self, window_id: str) -> None:
        window = self._windows.pop(window_id, None)
        if window is None:
            return
        self._by_task.pop(window.task_id, None)
        failures = 0
        try:
            window.session.run(window.browser.close())
        except Exception:
            failures += 1
        try:
            window.session.close()
        except Exception:
            failures += 1
        try:
            window.artifacts.cleanup()
        except Exception:
            failures += 1
        if failures:
            raise RuntimeError(f"managed session cleanup failed ({failures} operation(s))") from None

    def bring_to_front(self, window_id: str) -> None:
        if not self.exists(window_id):
            raise LookupError("managed window is unavailable")
        try:
            window = self._windows[window_id]
            window.session.run(window.browser.bring_managed_tab_to_front())
        except Exception:
            raise LookupError("managed window is unavailable") from None

    def browser_for(self, window_id: str) -> PlaywrightMCPAdapter:
        if not self.exists(window_id):
            raise LookupError("managed window is unavailable")
        return self._windows[window_id].browser

    def run_browser(self, window_id: str, coroutine):
        return self._windows[window_id].session.run(coroutine)

    def opened_url(self, window_id: str) -> str | None:
        return self._windows[window_id].opened_url

    def mark_opened(self, window_id: str, url: str) -> None:
        window = self._windows[window_id]
        if not self._one_tab(window.session.run(window.browser.managed_tabs())):
            raise LookupError("managed page is unavailable")
        token = uuid.uuid4().hex
        window.session.run(window.browser.set_managed_page_token(token))
        window.page_token = token
        window.opened_url = url

    def close(self) -> None:
        failures = 0
        for window_id in tuple(self._windows):
            try:
                self.release(window_id)
            except Exception:
                failures += 1
        if failures:
            raise RuntimeError(f"managed runtime cleanup failed ({failures} session(s))") from None
