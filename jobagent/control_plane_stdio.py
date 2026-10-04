"""One JSON request line / one JSON response line over trusted parent stdio."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import sys
import time
from typing import TextIO

from .control_plane import ControlPlaneError, LocalControlPlane
from .batch_domain import RunStatus, TaskStatus
from .persistence import BatchStore
from .scheduler import ApplicationScheduler
from .application_launcher import ApplicationLauncher
from .managed_runtime import MCPManagedWindows
from .mcp_browser import MCPServerCommand
from .runtime_worker import LaunchAndLoginWorker, LocalLoginConfiguration


_PARAMETERS = {
    "start_application_url": (frozenset({"url"}), frozenset()),
    "list_runs": (frozenset(), frozenset()),
    "get_run": (frozenset({"run_id"}), frozenset()),
    "list_applications": (frozenset(), frozenset({"run_id"})),
    "get_application": (frozenset({"task_id"}), frozenset()),
    "list_attention_required": (frozenset(), frozenset({"run_id"})),
    "get_application_report": (frozenset({"task_id"}), frozenset()),
    "diagnose_current_fields": (frozenset({"task_id"}), frozenset()),
    "get_narrative_entries": (frozenset({"task_id"}), frozenset()),
    "resume_application": (frozenset({"task_id"}), frozenset()),
    "resume_and_reconcile": (frozenset({"task_id"}), frozenset()),
    "open_application": (frozenset({"task_id"}), frozenset()),
    "recover_application": (frozenset({"task_id"}), frozenset()),
    "archive_application": (frozenset({"task_id"}), frozenset()),
    "bring_window_to_front": (frozenset({"task_id"}), frozenset()),
    "review_report_entry": (frozenset({"entry_id"}), frozenset()),
    "resolve_field": (frozenset({"entry_id"}), frozenset()),
    "undo_field_resolution": (frozenset({"entry_id"}), frozenset()),
    "mark_final_review_checked": (frozenset({"task_id"}), frozenset()),
    "record_submission": (frozenset({"task_id"}), frozenset()),
    "replace_narrative": (frozenset({"entry_id", "text"}), frozenset()),
}


def _bad_request(message: str) -> ControlPlaneError:
    return ControlPlaneError("BAD_REQUEST", message)


def _validate(request: object) -> tuple[str, str, dict]:
    if not isinstance(request, dict) or set(request) != {"id", "method", "params"}:
        raise _bad_request("request must contain id, method, and params only")
    request_id, method, params = request["id"], request["method"], request["params"]
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise _bad_request("id must be a nonempty string of at most 128 characters")
    if not isinstance(method, str) or method not in _PARAMETERS:
        raise _bad_request("unknown method")
    if not isinstance(params, dict):
        raise _bad_request("params must be an object")
    required, optional = _PARAMETERS[method]
    if not required <= params.keys() or not params.keys() <= required | optional:
        raise _bad_request("invalid parameters")
    for key, value in params.items():
        if not isinstance(value, str) or not value.strip():
            raise _bad_request(f"{key} must be a nonempty string")
        if len(value) > (10000 if key == "text" else 2048 if key == "url" else 128):
            raise _bad_request(f"{key} exceeds maximum length")
    return request_id, method, params


def dispatch(request: object, control: LocalControlPlane) -> dict:
    candidate_id = request.get("id") if isinstance(request, dict) else None
    request_id = candidate_id if isinstance(candidate_id, str) and 0 < len(candidate_id) <= 128 else None
    try:
        request_id, method, params = _validate(request)
        # Explicit allowlist. No caller-selected actor/action, reflection, or generic Submit.
        operations = {
            "start_application_url": control.start_application_url,
            "list_runs": control.list_runs,
            "get_run": control.get_run,
            "list_applications": control.list_applications,
            "get_application": control.get_application,
            "list_attention_required": control.list_attention_required,
            "get_application_report": control.get_application_report,
            "diagnose_current_fields": control.diagnose_current_fields,
            "get_narrative_entries": control.get_narrative_entries,
            "resume_application": control.resume_application,
            "resume_and_reconcile": control.resume_and_reconcile,
            "open_application": control.open_application,
            "recover_application": control.recover_application,
            "archive_application": control.archive_application,
            "bring_window_to_front": control.bring_window_to_front,
            "review_report_entry": control.review_report_entry,
            "resolve_field": control.resolve_field,
            "undo_field_resolution": control.undo_field_resolution,
            "mark_final_review_checked": control.mark_final_review_checked,
            "record_submission": control.record_submission,
            "replace_narrative": control.replace_narrative,
        }
        return {"id": request_id, "ok": True, "result": operations[method](**params)}
    except ControlPlaneError as error:
        code, message = error.code, error.message
    except KeyError:
        code, message = "NOT_FOUND", "record not found"
    except LookupError:
        code, message = "WINDOW_UNAVAILABLE", "managed window is unavailable"
    except (ValueError, PermissionError):
        code, message = "INVALID_TRANSITION", "operation is not allowed in the current state"
    except Exception:
        code, message = "INTERNAL_ERROR", "local backend error"
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def handle_line(line: str, control: LocalControlPlane) -> str:
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        request = None
    return json.dumps(dispatch(request, control), ensure_ascii=False, separators=(",", ":"))


def serve(input_stream: TextIO, output_stream: TextIO, control: LocalControlPlane) -> None:
    for line in input_stream:
        output_stream.write(handle_line(line, control) + "\n")
        output_stream.flush()


def progress_running_batch(store: BatchStore, scheduler: ApplicationScheduler,
                           completed_passes: set[tuple[str, str | None]]) -> None:
    """Idle automation tick, authorized only by an existing durable RUNNING run."""
    for run in store.list_runs():
        if run.status is not RunStatus.RUNNING:
            continue
        active = [task for task in scheduler.tasks(run.id) if task.status in {
            TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING,
            TaskStatus.FILLING, TaskStatus.ADVANCING}]
        if active and (active[0].id, active[0].resume_requested_at) in completed_passes:
            continue
        step = scheduler.step(run.id)
        if (step and step.outcome.kind.value == "progress" and
                step.outcome.page_or_step != "page_advanced"):
            completed_passes.add((step.task.id, step.task.resume_requested_at))
        if step:
            # One worker invocation per idle tick across all runs.
            return


class _UnavailableWindows:
    """Safe fallback when no browser runtime was configured."""

    def open_count(self) -> int:
        return 0

    def window_for_task(self, task_id: str) -> None:
        return None

    def exists(self, window_id: str) -> bool:
        return False

    def allocate(self, task_id: str) -> str:
        raise RuntimeError("window runtime is not configured")

    def release(self, window_id: str) -> None:
        raise RuntimeError("window runtime is not configured")

    def bring_to_front(self, window_id: str) -> None:
        raise RuntimeError("window runtime is not configured")


class _UnavailableWorker:
    def work_until_yield(self, request: object) -> None:
        raise RuntimeError("application worker is not configured")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Job Agent control plane over JSON lines")
    parser.add_argument("--db", required=True, help="local SQLite database path")
    parser.add_argument("--config", default="config.json", help="gitignored local configuration path")
    parser.add_argument("--mcp-cli", default=os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI"),
                        help="installed Playwright MCP CLI path")
    parser.add_argument("--max-open-applications", type=int, default=3)
    args = parser.parse_args()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    try:
        _run_backend(args)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _run_backend(args: argparse.Namespace) -> None:
    with BatchStore(args.db) as store:
        if not args.mcp_cli:
            scheduler = ApplicationScheduler(store, _UnavailableWorker(), _UnavailableWindows(),
                                             max_open_applications=args.max_open_applications)
            serve(sys.stdin, sys.stdout, LocalControlPlane(store, scheduler))
            return
        cli = Path(args.mcp_cli).expanduser().resolve()
        if not cli.is_file() or not shutil.which("node"):
            raise ValueError("Playwright MCP CLI or Node is unavailable")
        command = lambda: MCPServerCommand(shutil.which("node") or "node", (
            str(cli), "--isolated", "--no-webmcp", "--browser", "chrome", "--codegen", "none"))
        windows = MCPManagedWindows(command)
        try:
            worker = LaunchAndLoginWorker(store, ApplicationLauncher(windows),
                                          LocalLoginConfiguration(args.config))
            scheduler = ApplicationScheduler(store, worker, windows,
                                             max_open_applications=args.max_open_applications)
            control = LocalControlPlane(store, scheduler)
            selector = selectors.DefaultSelector()
            selector.register(sys.stdin, selectors.EVENT_READ)
            completed_passes: set[tuple[str, str | None]] = set()
            next_tick = time.monotonic() + 1
            try:
                while True:
                    if time.monotonic() >= next_tick:
                        progress_running_batch(store, scheduler, completed_passes)
                        next_tick = time.monotonic() + 1
                    events = selector.select(timeout=max(0, next_tick - time.monotonic()))
                    if events:
                        line = sys.stdin.readline()
                        if not line:
                            break
                        sys.stdout.write(handle_line(line, control) + "\n")
                        sys.stdout.flush()
            finally:
                selector.close()
        finally:
            windows.close()


if __name__ == "__main__":
    main()
