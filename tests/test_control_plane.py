"""Offline trusted-control-plane and JSON-line transport tests."""

import io
import json
import argparse
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobagent.batch_domain import (
    Blocker, HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance, ReportKind, ReviewState, RunStatus,
    TaskStatus, Verification,
)
from jobagent.application_worker import WorkerOutcome, WorkerYield
from jobagent.control_plane import LocalControlPlane
from jobagent.control_plane_stdio import _run_backend, dispatch, handle_line, progress_running_batch, serve
from jobagent.dedupe import ListingInput
from jobagent.persistence import BatchStore
from jobagent.scheduler import ApplicationScheduler


class Windows:
    def __init__(self):
        self.by_task = {}
        self.front = []

    def open_count(self):
        return len(self.by_task)

    def window_for_task(self, task_id):
        return self.by_task.get(task_id)

    def exists(self, window_id):
        return window_id in self.by_task.values()

    def allocate(self, task_id):
        window_id = f"window-{len(self.by_task) + 1}"
        self.by_task[task_id] = window_id
        return window_id

    def release(self, window_id):
        del self.by_task[next(task for task, value in self.by_task.items() if value == window_id)]

    def bring_to_front(self, window_id):
        self.front.append(window_id)


class ProgressWorker:
    def __init__(self):
        self.calls = []

    def work_until_yield(self, request):
        self.calls.append(request.task.id)
        return WorkerOutcome(WorkerYield.PROGRESS)


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "batch.sqlite3"
        self.store = BatchStore(self.path)
        self.run = self.store.create_run(("board",), 3)
        self.windows = Windows()
        self.control = self.make_control()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_control(self):
        scheduler = ApplicationScheduler(self.store, object(), self.windows,
                                         max_open_applications=3)
        return LocalControlPlane(self.store, scheduler)

    def queue(self, name):
        listing, _ = self.store.register_listing(ListingInput(
            "board", name, "Engineer", f"https://jobs.test/{name}", name))
        return self.store.queue_task(self.run.id, listing.id)

    def pause(self, task):
        window_id = self.windows.allocate(task.id)
        self.store.start(task.id, browser_session_id=window_id)
        return self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")

    def request(self, method, params=None, request_id="1"):
        return dispatch({"id": request_id, "method": method, "params": params or {}}, self.control)

    def test_queries_attention_resume_and_trusted_authorization(self):
        a = self.queue("Acme")
        self.pause(a)
        self.store.add_report_entry(
            a.id, page_or_step="page 1", visible_label="Work authorization",
            semantic_key="employment.us_authorized", kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="candidate_fact_missing")
        self.assertEqual(self.request("list_runs")["result"][0]["run_id"], self.run.id)
        self.assertEqual(self.request("get_run", {"run_id": self.run.id})["result"]["queued_count"], 1)
        self.assertEqual(self.request("list_applications", {"run_id": self.run.id})["result"][0]["task_id"], a.id)
        detail = self.request("get_application", {"task_id": a.id})["result"]
        self.assertEqual((detail["company"], detail["status"], detail["blocker"]),
                         ("Acme", "human_paused", "needs_answer"))
        self.assertEqual(detail["pending_review_count"], 1)
        self.assertTrue(detail["window_available"])
        self.assertTrue(detail["resume_available"])
        self.assertEqual(self.request("list_attention_required")["result"][0]["task_id"], a.id)
        self.assertEqual(len(self.request("get_application_report", {"task_id": a.id})["result"]["entries"]), 1)
        self.assertEqual(self.windows.front, [])
        spoof = self.request("resume_application", {"task_id": a.id, "actor": "local_owner"})
        self.assertEqual(spoof["error"]["code"], "BAD_REQUEST")
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.HUMAN_PAUSED)
        resumed = self.request("resume_application", {"task_id": a.id})
        self.assertEqual((resumed["result"]["status"], resumed["result"]["ownership"]),
                         ("queued", "automation_owned"))
        self.assertFalse(resumed["result"]["window_associated"])
        self.assertEqual(self.store.human_actions("task", a.id)[0].action, HumanAction.RESUME)
        self.assertEqual(self.store.human_actions("task", a.id)[0].actor.value, "local_owner")
        self.assertEqual(self.request("resume_application", {"task_id": a.id})["error"]["code"],
                         "NOT_RESUMABLE")

    def test_foreground_only_on_explicit_command_and_missing_window_error(self):
        a = self.queue("Beta")
        self.pause(a)
        self.request("get_application", {"task_id": a.id})
        self.request("list_applications")
        self.request("list_attention_required")
        self.assertEqual(self.windows.front, [])
        self.assertTrue(self.request("bring_window_to_front", {"task_id": a.id})["ok"])
        self.assertEqual(self.windows.front, [self.windows.by_task[a.id]])
        self.windows.release(self.windows.by_task[a.id])
        self.assertEqual(self.request("bring_window_to_front", {"task_id": a.id})["error"]["code"],
                         "WINDOW_UNAVAILABLE")

    def test_passive_request_service_does_not_call_scheduler_step(self):
        task = self.queue("Passive")
        def forbidden_step(*_args, **_kwargs):
            raise AssertionError("passive request called scheduler.step")
        self.control.scheduler.step = forbidden_step
        requests = [
            {"id": "1", "method": "list_runs", "params": {}},
            {"id": "2", "method": "list_applications", "params": {}},
            {"id": "3", "method": "get_application", "params": {"task_id": task.id}},
            {"id": "4", "method": "list_attention_required", "params": {}},
        ]
        incoming = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
        outgoing = io.StringIO()
        serve(incoming, outgoing, self.control)
        self.assertEqual(len(outgoing.getvalue().splitlines()), 4)
        self.assertEqual(self.windows.by_task, {})
        self.assertEqual(self.windows.front, [])
        self.assertEqual(self.store.human_actions("task", task.id), ())

    def test_review_phase_and_owner_recorded_submission(self):
        task = self.queue("Review phases")
        window_id = self.windows.allocate(task.id)
        self.store.start(task.id, browser_session_id=window_id)
        self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="First name",
            semantic_key="personal.first_name", kind=ReportKind.FIELD,
            provenance=Provenance.VERIFIED_PROFILE, action="filled_text",
            verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED)
        pending = self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="Fixture clearance code",
            semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="no_safe_answer")
        self.store.ready_for_review(task.id)
        initial = self.request("get_application", {"task_id": task.id})["result"]
        self.assertEqual(initial["review_phase"], "needs_review")
        self.assertFalse(initial["record_submission_available"])
        self.assertEqual(self.request("record_submission", {"task_id": task.id})["error"]["code"],
                         "INVALID_TRANSITION")
        self.assertEqual(self.request("review_report_entry", {"entry_id": pending.id})["result"]["review_state"],
                         "approved")
        after_item = self.request("get_application", {"task_id": task.id})["result"]
        self.assertEqual(after_item["review_phase"], "ready_for_final_review")
        self.assertFalse(after_item["record_submission_available"])
        self.assertEqual(self.request("record_submission", {"task_id": task.id})["error"]["code"],
                         "INVALID_TRANSITION")
        checked = self.request("mark_final_review_checked", {"task_id": task.id})["result"]
        self.assertEqual(checked["review_phase"], "ready_to_submit")
        self.assertTrue(checked["record_submission_available"])
        self.assertEqual(checked["status"], "ready_for_review")
        before_report = self.store.get_report(task.id)
        self.assertEqual(self.request("record_submission", {"task_id": task.id,
                                                            "actor": "local_owner"})["error"]["code"],
                         "BAD_REQUEST")
        self.assertEqual(self.request("record_submission", {"task_id": task.id,
                                                            "action": "record_submission"})["error"]["code"],
                         "BAD_REQUEST")
        self.assertEqual(self.request("record_submission")["error"]["code"], "BAD_REQUEST")
        self.assertFalse(self.request("record_submission", {"task_id": task.id, "text": "Submit"})["ok"])
        submitted = self.request("record_submission", {"task_id": task.id})["result"]
        durable = self.store.get_task(task.id)
        self.assertEqual((submitted["status"], submitted["review_phase"]),
                         ("submitted_by_human", "submitted"))
        self.assertEqual(durable.status, TaskStatus.SUBMITTED_BY_HUMAN)
        self.assertEqual(durable.ownership, Ownership.HUMAN_OWNED)
        self.assertIsNotNone(durable.submitted_at)
        self.assertEqual(durable.submitted_by, HumanActor.LOCAL_OWNER.value)
        self.assertFalse(submitted["record_submission_available"])
        self.assertFalse(submitted["resume_available"])
        self.assertFalse(submitted["final_review_available"])
        self.assertEqual(self.store.get_report(task.id), before_report)
        self.assertEqual(self.store.human_actions("task", task.id)[-1].action,
                         HumanAction.RECORD_SUBMISSION)
        self.assertEqual(self.request("list_attention_required")["result"], [])
        self.assertEqual(self.request("list_applications")["result"][0]["task_id"], task.id)
        self.assertTrue(self.request("bring_window_to_front", {"task_id": task.id})["ok"])
        self.assertEqual(self.windows.front, [window_id])
        self.assertEqual(self.request("record_submission", {"task_id": task.id})["error"]["code"],
                         "INVALID_TRANSITION")
        self.assertEqual(self.request("resume_application", {"task_id": task.id})["error"]["code"],
                         "NOT_RESUMABLE")

    def test_submission_rejects_other_statuses_and_wrong_authorization(self):
        queued = self.queue("Queued")
        self.assertIsNone(self.request("get_application", {"task_id": queued.id})["result"]["review_phase"])
        self.assertEqual(self.request("record_submission", {"task_id": queued.id})["error"]["code"],
                         "INVALID_TRANSITION")
        paused = self.queue("Paused")
        self.pause(paused)
        self.assertIsNone(self.request("get_application", {"task_id": paused.id})["result"]["review_phase"])
        self.assertEqual(self.request("record_submission", {"task_id": paused.id})["error"]["code"],
                         "INVALID_TRANSITION")
        ready = self.queue("Ready")
        self.store.start(ready.id, browser_session_id=self.windows.allocate(ready.id))
        self.store.ready_for_review(ready.id)
        with self.assertRaises(PermissionError):
            self.store.mark_submitted_by_human(ready.id, authorization=None)
        with self.assertRaises(PermissionError):
            self.store.mark_submitted_by_human(ready.id, authorization=HumanAuthorization(
                HumanActor.LOCAL_OWNER, HumanAction.FINAL_REVIEW))
        self.assertEqual(self.request("record_submission", {"task_id": ready.id})["error"]["code"],
                         "INVALID_TRANSITION")

    def test_only_persisted_running_batch_progresses_on_idle_tick(self):
        task = self.queue("Running")
        worker = ProgressWorker()
        scheduler = ApplicationScheduler(self.store, worker, self.windows,
                                         max_open_applications=3)
        completed = set()
        progress_running_batch(self.store, scheduler, completed)
        self.assertEqual((worker.calls, self.windows.by_task), ([], {}))
        self.store.set_run_status(self.run.id, RunStatus.RUNNING)
        progress_running_batch(self.store, scheduler, completed)
        self.assertEqual(worker.calls, [task.id])
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.LAUNCHING)
        progress_running_batch(self.store, scheduler, completed)
        self.assertEqual(worker.calls, [task.id])
        self.assertEqual(self.windows.front, [])

    def test_human_paused_and_ready_for_review_are_not_idle_candidates(self):
        task = self.queue("Paused")
        self.store.set_run_status(self.run.id, RunStatus.RUNNING)
        self.pause(task)
        worker = ProgressWorker()
        scheduler = ApplicationScheduler(self.store, worker, self.windows,
                                         max_open_applications=3)
        progress_running_batch(self.store, scheduler, set())
        self.assertEqual(worker.calls, [])
        self.store.resume_by_human(task.id,
            authorization=self.control._owner(HumanAction.RESUME))
        self.store.start(task.id, browser_session_id="review-window")
        self.store.ready_for_review(task.id)
        progress_running_batch(self.store, scheduler, set())
        self.assertEqual(worker.calls, [])

    def test_backend_eof_runs_managed_runtime_cleanup(self):
        class ClosingWindows(Windows):
            def __init__(self):
                super().__init__()
                self.closed = False

            def close(self):
                self.closed = True

        cli = self.path.parent / "synthetic-cli.js"
        cli.write_text("// never executed: no RUNNING batch")
        windows = ClosingWindows()
        reader, writer = os.pipe()
        os.close(writer)
        with os.fdopen(reader, "r") as input_stream, patch(
                "jobagent.control_plane_stdio.MCPManagedWindows", return_value=windows), patch(
                "jobagent.control_plane_stdio.shutil.which", return_value="node"), patch(
                "jobagent.control_plane_stdio.sys.stdin", input_stream), patch(
                "jobagent.control_plane_stdio.sys.stdout", io.StringIO()):
            _run_backend(argparse.Namespace(db=self.path, config="missing.json",
                                            mcp_cli=str(cli), max_open_applications=3))
        self.assertTrue(windows.closed)
        self.assertEqual(windows.by_task, {})

    @unittest.skipUnless(shutil.which("node"), "Node is required for configured backend startup")
    def test_configured_backend_sigterm_exits_through_cleanup(self):
        cli = self.path.parent / "synthetic-cli.js"
        cli.write_text("// never executed: no RUNNING batch")
        child = subprocess.Popen(
            [sys.executable, "-m", "jobagent.control_plane_stdio", "--db", str(self.path),
             "--mcp-cli", str(cli)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write('{"id":"1","method":"list_runs","params":{}}\n')
            child.stdin.flush()
            ready, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(ready)
            self.assertTrue(json.loads(child.stdout.readline())["ok"])
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(child.stdout.read(), "")
            self.assertEqual(child.stderr.read(), "")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_narrative_review_checkoff_and_no_submit(self):
        a = self.queue("Gamma")
        self.store.start(a.id, browser_session_id=self.windows.allocate(a.id))
        draft = self.store.add_report_entry(
            a.id, page_or_step="page 2", visible_label="Why this company?",
            semantic_key="why_company", kind=ReportKind.NARRATIVE,
            provenance=Provenance.AI_DRAFT_REVIEW, action="drafted",
            verification=Verification.UNKNOWN, review_state=ReviewState.PENDING,
            narrative_text="A synthetic draft")
        self.store.ready_for_review(a.id)
        detail = self.request("get_application", {"task_id": a.id})["result"]
        self.assertEqual((detail["ownership"], detail["pending_narrative_count"]), ("human_owned", 1))
        self.assertFalse(detail["final_review_available"])
        self.assertEqual(self.request("get_narrative_entries", {"task_id": a.id})["result"]["entries"][0]["narrative_text"],
                         "A synthetic draft")
        self.assertEqual(self.request("mark_final_review_checked", {"task_id": a.id})["error"]["code"],
                         "INVALID_TRANSITION")
        revised = self.request("replace_narrative", {"entry_id": draft.id, "text": "Human revision"})
        self.assertEqual(revised["result"]["provenance"], "human_provided")
        self.assertEqual(revised["result"]["review_state"], "approved")
        original = self.store.get_report_entry(draft.id)
        self.assertEqual(original.provenance, Provenance.AI_DRAFT_REVIEW)
        self.assertEqual(original.review_state, ReviewState.APPROVED)
        self.assertEqual(original.narrative_text, "A synthetic draft")
        self.assertIsNone(self.store.get_task(a.id).review_checked_at)
        self.assertIsNone(self.store.get_task(a.id).submitted_at)
        self.assertEqual(len(self.request("get_narrative_entries", {"task_id": a.id})["result"]["entries"]), 2)
        self.assertTrue(self.request("get_application", {"task_id": a.id})["result"]["final_review_available"])
        self.assertTrue(self.request("mark_final_review_checked", {"task_id": a.id})["ok"])
        self.assertIsNotNone(self.store.get_task(a.id).review_checked_at)
        self.assertTrue(self.request("get_application", {"task_id": a.id})["result"]["final_review_checked"])
        self.assertFalse(self.request("get_application", {"task_id": a.id})["result"]["final_review_available"])
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.READY_FOR_REVIEW)
        self.assertIsNone(self.store.get_task(a.id).submitted_at)
        self.assertEqual(self.request("submit_application", {"task_id": a.id})["error"]["code"],
                         "BAD_REQUEST")

    def test_report_item_review_and_privacy(self):
        a = self.queue("Delta")
        self.store.start(a.id, browser_session_id=self.windows.allocate(a.id))
        entry = self.store.add_report_entry(
            a.id, page_or_step="page 1", visible_label="Email",
            semantic_key="personal.email", kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING)
        self.store.ready_for_review(a.id)
        reviewed = self.request("review_report_entry", {"entry_id": entry.id})
        self.assertEqual(reviewed["result"]["review_state"], "approved")
        wire = json.dumps(self.request("get_application", {"task_id": a.id}))
        wire += json.dumps(self.request("get_application_report", {"task_id": a.id}))
        for forbidden in ("browser_session_id", "target_ref", "snapshot", "password", "cookie",
                          "auth_token", "candidate@example.test", self.windows.by_task[a.id]):
            self.assertNotIn(forbidden, wire)
        self.assertIsNone(self.request("get_application_report", {"task_id": a.id})["result"]["entries"][0]["narrative_text"])

    def test_protocol_validation_and_bounded_internal_error(self):
        self.assertEqual(json.loads(handle_line("not json", self.control))["error"]["code"], "BAD_REQUEST")
        self.assertEqual(dispatch({"id": "x", "method": "list_runs", "params": {},
                                   "action": "resume"}, self.control)["error"]["code"], "BAD_REQUEST")
        self.assertEqual(self.request("get_run", {"run_id": "missing"})["error"]["code"], "NOT_FOUND")
        original = self.control.list_runs
        def broken():
            raise RuntimeError("private traceback detail")
        self.control.list_runs = broken
        error = self.request("list_runs")
        self.assertEqual(error["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("private traceback detail", json.dumps(error))
        self.control.list_runs = original
        source = io.StringIO('{"id":"one","method":"list_runs","params":{}}\nnot json\n')
        output = io.StringIO()
        serve(source, output, self.control)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(json.loads(lines[0])["ok"])
        self.assertEqual(json.loads(lines[1])["error"]["code"], "BAD_REQUEST")

    def test_restart_with_standalone_child_protocol(self):
        a = self.queue("Echo")
        self.pause(a)
        self.store.close()
        command = [sys.executable, "-m", "jobagent.control_plane_stdio", "--db", str(self.path)]

        def start_child():
            return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)

        def exchange(child, payload):
            child.stdin.write(payload + "\n")
            child.stdin.flush()
            ready, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(ready, "child did not produce one response line")
            line = child.stdout.readline()
            self.assertTrue(line.endswith("\n"))
            response = json.loads(line)
            self.assertNotIn("Traceback", line)
            if not response["ok"]:
                self.assertLessEqual(len(response["error"]["message"]), 128)
            return response

        def finish(child):
            child.stdin.close()  # EOF is the shutdown signal.
            try:
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(child.stdout.read(), "")  # No extra stdout/log lines.
                self.assertEqual(child.stderr.read(), "")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

        child = start_child()
        try:
            read = exchange(child, json.dumps({"id": "1", "method": "list_attention_required", "params": {}}))
            self.assertEqual(read["result"][0]["status"], "human_paused")
            self.assertEqual(exchange(child, "not json")["error"]["code"], "BAD_REQUEST")
            self.assertEqual(exchange(child, json.dumps({"id": "3", "method": "unknown", "params": {}}))
                             ["error"]["code"], "BAD_REQUEST")
            self.assertEqual(exchange(child, json.dumps({"id": "4", "method": "get_application", "params": {}}))
                             ["error"]["code"], "BAD_REQUEST")
            for forbidden in ({"actor": "local_owner"}, {"action": "final_review"}):
                params = {"task_id": a.id, **forbidden}
                response = exchange(child, json.dumps({"id": "5", "method": "resume_application",
                                                       "params": params}))
                self.assertEqual(response["error"]["code"], "BAD_REQUEST")
                self.assertNotIn("Traceback", json.dumps(response))
            still_paused = exchange(child, json.dumps({"id": "6", "method": "get_application",
                                                       "params": {"task_id": a.id}}))
            self.assertEqual(still_paused["result"]["status"], "human_paused")
            resumed = exchange(child, json.dumps({"id": "7", "method": "resume_application",
                                                  "params": {"task_id": a.id}}))
            self.assertEqual(resumed["result"]["status"], "queued")
            finish(child)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        second = start_child()
        try:
            recovered = exchange(second, json.dumps({"id": "8", "method": "get_application",
                                                     "params": {"task_id": a.id}}))
            self.assertEqual(recovered["result"]["status"], "queued")
            self.assertFalse(recovered["result"]["resume_available"])
            finish(second)
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=5)
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.QUEUED)
        self.assertEqual(self.store.get_task(a.id).ownership, Ownership.AUTOMATION_OWNED)


if __name__ == "__main__":
    unittest.main()
