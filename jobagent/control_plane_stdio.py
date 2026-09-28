"""One JSON request line / one JSON response line over trusted parent stdio."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from .control_plane import ControlPlaneError, LocalControlPlane
from .persistence import BatchStore
from .scheduler import ApplicationScheduler


_PARAMETERS = {
    "list_runs": (frozenset(), frozenset()),
    "get_run": (frozenset({"run_id"}), frozenset()),
    "list_applications": (frozenset(), frozenset({"run_id"})),
    "get_application": (frozenset({"task_id"}), frozenset()),
    "list_attention_required": (frozenset(), frozenset({"run_id"})),
    "get_application_report": (frozenset({"task_id"}), frozenset()),
    "get_narrative_entries": (frozenset({"task_id"}), frozenset()),
    "resume_application": (frozenset({"task_id"}), frozenset()),
    "bring_window_to_front": (frozenset({"task_id"}), frozenset()),
    "review_report_entry": (frozenset({"entry_id"}), frozenset()),
    "mark_final_review_checked": (frozenset({"task_id"}), frozenset()),
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
        if len(value) > (10000 if key == "text" else 128):
            raise _bad_request(f"{key} exceeds maximum length")
    return request_id, method, params


def dispatch(request: object, control: LocalControlPlane) -> dict:
    candidate_id = request.get("id") if isinstance(request, dict) else None
    request_id = candidate_id if isinstance(candidate_id, str) and 0 < len(candidate_id) <= 128 else None
    try:
        request_id, method, params = _validate(request)
        # Explicit allowlist. No caller-selected actor/action, reflection, or generic Submit.
        operations = {
            "list_runs": control.list_runs,
            "get_run": control.get_run,
            "list_applications": control.list_applications,
            "get_application": control.get_application,
            "list_attention_required": control.list_attention_required,
            "get_application_report": control.get_application_report,
            "get_narrative_entries": control.get_narrative_entries,
            "resume_application": control.resume_application,
            "bring_window_to_front": control.bring_window_to_front,
            "review_report_entry": control.review_report_entry,
            "mark_final_review_checked": control.mark_final_review_checked,
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


class _UnavailableWindows:
    """Pre-window-integration runtime: persisted IDs never imply live windows."""

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
    args = parser.parse_args()
    with BatchStore(args.db) as store:
        scheduler = ApplicationScheduler(store, _UnavailableWorker(), _UnavailableWindows(),
                                         max_open_applications=1)
        serve(sys.stdin, sys.stdout, LocalControlPlane(store, scheduler))


if __name__ == "__main__":
    main()
