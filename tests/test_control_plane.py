"""Offline trusted-control-plane and JSON-line transport tests."""

import io
import asyncio
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
from jobagent.domain import ApplicationObservation, ControlType, DiscoverySummary, QuestionObservation
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

    def test_archive_is_local_only_and_hidden_from_scheduler(self):
        queued = self.queue("queued-archive")
        paused = self.queue("paused-archive")
        self.store.start(paused.id)
        self.store.pause_for_human(paused.id, Blocker.NEEDS_ANSWER, page_or_step="page")
        before_windows = dict(self.windows.by_task)
        result = dispatch({"id": "archive", "method": "archive_application",
                           "params": {"task_id": queued.id}}, self.control)
        self.assertTrue(result["result"]["removed_from_queue"])
        self.assertEqual(self.store.get_task(queued.id).status, TaskStatus.SKIPPED)
        self.assertNotIn(queued.id, [task.id for task in self.control.scheduler.tasks()])
        result = self.control.archive_application(paused.id)
        self.assertFalse(result["removed_from_queue"])
        self.assertEqual(self.store.get_task(paused.id).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(before_windows, self.windows.by_task)
        self.assertEqual(self.control.list_applications(), [])
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_historical_run_keeps_listing_label_after_application_archive(self):
        task = self.queue("history-label")
        self.control.archive_application(task.id)
        run = self.control.get_run(self.run.id)
        self.assertEqual(run["display_name"], "Engineer · history-label")
        self.assertEqual(run["queued_count"], 1)
        self.assertEqual(self.control.list_applications(), [])

    def test_current_report_reconciles_lifecycle_and_prior_resumes(self):
        task = self.queue("current-report")
        page = "Application page"
        identity = '["first_name","first name","application","legal name","0"]'
        for action, review, reason in (
                ("deferred", ReviewState.PENDING, "no_safe_answer"),
                ("filled_text", ReviewState.NOT_REQUIRED, None),
                ("confirmed_trusted", ReviewState.NOT_REQUIRED, None),
                ("confirmed_trusted", ReviewState.NOT_REQUIRED, None)):
            self.store.add_report_entry(task.id, page_or_step=page,
                visible_label="First Name", semantic_key="first_name", kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED if review is ReviewState.PENDING
                           else Provenance.VERIFIED_PROFILE,
                action=action, verification=Verification.NOT_ATTEMPTED if review is ReviewState.PENDING
                       else Verification.VERIFIED, review_state=review, reason=reason,
                requiredness="required", question_identity=identity)
        # Earlier parser versions did not record question_identity. The unique
        # newer scoped identity still identifies the same durable question.
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Phone Extension", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.SKIPPED, action="manual_complete",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.NOT_REQUIRED,
            reason="human_entered_value", requiredness="optional")
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Phone Extension", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="no_safe_answer", requiredness="optional",
            question_identity='["","phone extension","application","phone","0"]')
        entries = self.control.get_application_report(task.id)["entries"]
        self.assertEqual(len([e for e in entries if e["visible_label"] == "First Name"]), 1)
        self.assertEqual(len([e for e in entries if e["visible_label"] == "Phone Extension"]), 1)
        self.assertEqual(next(e for e in entries if e["visible_label"] == "First Name")["action"],
                         "confirmed_trusted")
        self.assertEqual(next(e for e in entries if e["visible_label"] == "Phone Extension")["action"],
                         "deferred")
        self.assertEqual(self.control.get_application(task.id)["pending_review_count"], 1)
        self.assertEqual(len(self.store.get_report(task.id).entries), 6)

    def test_answered_then_fresh_empty_referral_reopens_same_canonical_card(self):
        task = self.queue("referral-reopened")
        page = "Application page"
        question = QuestionObservation("How Did You Hear About Us?", ControlType.CHOICE,
            required=True, current_value="Select One")
        identity = question.report_identity()
        self.assertFalse(question.answer_state().satisfied)
        issue = dict(task_id=task.id, page_or_step=page, visible_label=question.label,
            semantic_key=None, kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
            action="deferred", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.PENDING, reason="unsupported_control",
            requiredness="required", question_identity=identity)
        self.store.add_report_entry_once(**issue)
        self.store.add_report_entry_once(task.id, page_or_step=page,
            visible_label=question.label, semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.VERIFIED_PROFILE, action="selected_option",
            verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED,
            requiredness="required", question_identity=identity)
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
            blocker=Blocker.UNSUPPORTED_CONTROL.value, current_page_or_step=page)
        self.store.add_report_entry_once(**issue)
        canonical = [entry for entry in self.control.get_application_report(task.id)["entries"]
                     if entry["visible_label"] == question.label]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["report_group"], "needs_attention")
        self.assertTrue(canonical[0]["is_blocking"])
        self.assertEqual(canonical[0]["review_state"], "pending")
        self.assertEqual(self.control.get_application(task.id)["pending_review_count"], 1)

    def test_pending_count_matches_visible_canonical_field_cards(self):
        task = self.queue("pending-count")
        page = "Application page"
        for label in ("Referral", "State", "Phone Code"):
            self.store.add_report_entry_once(task.id, page_or_step=page,
                visible_label=label, semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED, action="deferred",
                verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
                reason="unsupported_control", requiredness="required",
                question_identity=label)
        # Four unresolved historical field rows still project to three cards.
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Referral", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="unsupported_control", requiredness="required",
            question_identity="Referral")
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Page classification", semantic_key="page_classification",
            kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
            action="unknown", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.PENDING)
        report = self.control.get_application_report(task.id)["entries"]
        visible = [entry for entry in report if entry["kind"] == "field" and
                   entry["report_group"] in {"needs_attention", "needs_review"} and
                   entry["review_state"] == "pending"]
        self.assertEqual(len(visible), 3)
        self.assertEqual(self.control.get_application_report(task.id)["pending_review_count"], 3)
        self.assertEqual(self.control.get_application(task.id)["pending_review_count"], 3)
        self.assertEqual(self.control.get_application_report(task.id)["needs_attention_count"], 0)
        self.assertEqual(self.control.get_application_report(task.id)["needs_review_count"], 3)
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Why this role", semantic_key="why_this_role",
            kind=ReportKind.NARRATIVE, provenance=Provenance.UNRESOLVED,
            action="deferred", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.PENDING, reason="narrative_deferred")
        snapshot = self.control.get_application_report(task.id)
        self.assertEqual(snapshot["needs_attention_count"], 0)
        self.assertEqual(snapshot["needs_review_count"], 3)
        self.assertEqual(snapshot["needs_review_count"], sum(
            entry["kind"] == "field" and entry["report_group"] == "needs_review"
            for entry in snapshot["entries"]))

    def test_current_required_blockers_precede_optional_and_completed(self):
        task = self.queue("blocked-report")
        page = "Application page"
        for label, requiredness, action, state, reason in (
                ("First Name", "required", "filled_text", ReviewState.NOT_REQUIRED, None),
                ("Phone Extension", "optional", "deferred", ReviewState.PENDING, "no_safe_answer"),
                ("Referral source", "required", "deferred", ReviewState.PENDING, "unsupported_control"),
                ("Phone Code", "required", "deferred", ReviewState.PENDING, "unsupported_control")):
            self.store.add_report_entry(task.id, page_or_step=page, visible_label=label,
                semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED if state is ReviewState.PENDING
                           else Provenance.VERIFIED_PROFILE,
                action=action, verification=Verification.NOT_ATTEMPTED if state is ReviewState.PENDING
                       else Verification.VERIFIED, review_state=state, reason=reason,
                requiredness=requiredness, question_identity=label)
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                               blocker=Blocker.UNSUPPORTED_CONTROL.value, current_page_or_step=page)
        entries = self.control.get_application_report(task.id)["entries"]
        self.assertEqual([e["visible_label"] for e in entries[:2]], ["Phone Code", "Referral source"])
        self.assertTrue(all(e["is_blocking"] and e["report_group"] == "needs_attention"
                            for e in entries[:2]))
        self.assertEqual(entries[2]["report_group"], "needs_review")
        self.assertEqual(entries[3]["report_group"], "completed")
        self.assertEqual(self.control.get_application(task.id)["blocker_label"],
                         "Required control needs manual input")
        # A later fresh observation updates the same logical field, leaving
        # historical pending rows in SQLite but no longer in the normal report.
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="Referral source", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.SKIPPED, action="manual_complete",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.NOT_REQUIRED,
            reason="unsupported_control", requiredness="required",
            question_identity="Referral source")
        entries = self.control.get_application_report(task.id)["entries"]
        self.assertEqual(len(entries), 4)
        self.assertEqual(len([e for e in entries if e["visible_label"] == "Referral source"]), 1)
        self.assertEqual(next(e for e in entries if e["visible_label"] == "Referral source")["report_group"],
                         "completed")

    def test_resume_and_open_keep_last_blockers_until_fresh_result(self):
        for action in ("resume", "open"):
            with self.subTest(action=action):
                task = self.queue(f"retained-{action}")
                page = "Application page"
                for label in ("Country / Territory Phone Code", "How Did You Hear About Us?"):
                    self.store.add_report_entry(task.id, page_or_step=page, visible_label=label,
                        semantic_key=None, kind=ReportKind.FIELD,
                        provenance=Provenance.UNRESOLVED, action="deferred",
                        verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
                        reason="unsupported_control", requiredness="required",
                        question_identity=label)
                self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                                       blocker=Blocker.UNSUPPORTED_CONTROL.value,
                                       current_page_or_step=page)
                if action == "open":
                    self.control.open_application(task.id)
                else:
                    self.control.resume_application(task.id)
                queued = self.store.get_task(task.id)
                self.assertEqual(queued.status, TaskStatus.QUEUED)
                self.assertEqual(queued.blocker, Blocker.UNSUPPORTED_CONTROL)
                current = self.control.get_application_report(task.id)["entries"]
                self.assertEqual(len([entry for entry in current if entry["is_blocking"]]), 2)
                self.assertEqual(self.control.get_application(task.id)["blocker_label"],
                                 "Required control needs manual input")
                self.assertEqual(sum(item["task_id"] == task.id
                                     for item in self.control.list_attention_required()), 1)
                self.assertEqual(self.control.get_application(task.id)["attention_category"], "queued")
                self.store.start(task.id, browser_session_id=f"window-{action}")
                self.assertEqual(sum(item["task_id"] == task.id
                                     for item in self.control.list_attention_required()), 1)
                self.assertEqual(len([entry for entry in self.control.get_application_report(task.id)["entries"]
                                      if entry["is_blocking"]]), 2)
                if action == "resume":
                    # A failed fresh read must not turn the old report into an
                    # empty or falsely completed current state.
                    from jobagent.batch_domain import FailureReason
                    self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR)
                    self.assertEqual(sum(item["task_id"] == task.id
                                         for item in self.control.list_attention_required()), 1)
                    self.assertEqual(len([entry for entry in self.control.get_application_report(task.id)["entries"]
                                          if entry["is_blocking"]]), 2)
                else:
                    self.store.add_report_entry(task.id, page_or_step=page,
                        visible_label="Country / Territory Phone Code", semantic_key=None,
                        kind=ReportKind.FIELD, provenance=Provenance.SKIPPED,
                        action="manual_complete", verification=Verification.NOT_ATTEMPTED,
                        review_state=ReviewState.NOT_REQUIRED, reason="unsupported_control",
                        requiredness="required", question_identity="Country / Territory Phone Code")
                    self.store.clear_observed_blocker(task.id)
                    current = self.control.get_application_report(task.id)["entries"]
                    self.assertEqual(len([entry for entry in current
                                          if entry["visible_label"] == "Country / Territory Phone Code"]), 1)
                    self.assertFalse(any(entry["is_blocking"] for entry in current))

    def test_explicit_field_diagnostic_reconciles_report_without_browser_action(self):
        task = self.queue("diagnostic")
        page = "Application"
        phone = QuestionObservation("Country / Territory Phone Code", ControlType.UNKNOWN,
                                    required=True, current_value="Exampleland (+9)",
                                    target_ref="fresh-phone", raw_role="button",
                                    required_evidence="group_required")
        state = QuestionObservation("State", ControlType.CHOICE, required=True,
                                    current_value="Select One", target_ref="fresh-state")
        secret = QuestionObservation("Password", ControlType.SECRET,
                                     current_value="private credential", target_ref="fresh-secret")
        observation = ApplicationObservation("fresh-1", "https://example.test/apply", page,
                                             questions=(phone, state, secret),
                                             discovery_summary=DiscoverySummary(
                                                 raw_actionable_count=5,
                                                 accessibility_question_count=2,
                                                 dom_recovered_field_count=1,
                                                 ignored_reasons=(("navigation_or_submit", 1),)))

        class PassiveBrowser:
            calls = 0

            async def observe(self):
                self.calls += 1
                return observation

        browser = PassiveBrowser()
        self.windows.by_task[task.id] = "window-diagnostic"
        self.windows.browser_for = lambda _window: browser
        self.windows.run_browser = lambda _window, operation: asyncio.run(operation)
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label=phone.label, semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="unsupported_control", requiredness="required",
            question_identity=phone.report_identity())
        before = len(self.store.get_report(task.id).entries)
        response = dispatch({"id": "diagnostic", "method": "diagnose_current_fields",
                             "params": {"task_id": task.id}}, self.control)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["task_id"], task.id)
        self.assertTrue(response["result"]["captured_at"])
        self.assertTrue(response["result"]["diagnostic_generation"])
        fields = {field["label"]: field for field in response["result"]["fields"]}
        self.assertEqual(browser.calls, 1)
        self.assertEqual(set(fields), {phone.label, state.label})
        self.assertTrue(fields[phone.label]["satisfied"])
        self.assertFalse(fields[phone.label]["blocking"])
        self.assertTrue(fields[state.label]["blocking"])
        summary = response["result"]["discovery_summary"]
        self.assertEqual(summary["normalized_field_count"], 3)
        self.assertEqual(summary["dom_recovered_field_count"], 1)
        self.assertEqual(summary["ignored_reasons"], {"navigation_or_submit": 1})
        self.assertNotIn("Exampleland", repr(response))
        self.assertNotIn("private credential", repr(response))
        self.assertNotIn("fresh-phone", repr(response))
        self.assertEqual(len(self.store.get_report(task.id).entries), before + 2)
        current = {entry.visible_label: entry for entry in self.store.current_report_entries(task.id)}
        self.assertEqual(current[phone.label].action, "manual_complete")
        self.assertEqual(current[phone.label].verification, Verification.NOT_ATTEMPTED)
        self.assertEqual(fields[phone.label]["report_group"], "completed")
        self.assertEqual(len([entry for entry in self.store.get_report(task.id).entries
                              if entry.visible_label == phone.label]), 2)
        self.assertEqual(self.windows.front, [])
        later = dispatch({"id": "diagnostic-next", "method": "diagnose_current_fields",
                          "params": {"task_id": task.id}}, self.control)
        self.assertTrue(later["ok"])
        self.assertEqual(browser.calls, 2)
        self.assertEqual(len(self.store.get_report(task.id).entries), before + 2)
        self.assertNotEqual(response["result"]["diagnostic_generation"],
                            later["result"]["diagnostic_generation"])

    def test_fresh_text_fields_supersede_stale_attention_cards_without_losing_history(self):
        task = self.queue("fresh-fields")
        page = "My Information"
        self.store.start(task.id, browser_session_id="window-fields")
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step=page)
        labels = ("First Name", "Last Name", "City", "Phone Number")
        questions = tuple(QuestionObservation(label, ControlType.TEXT, required=True,
                           current_value=f"value-{index}", target_ref=f"ref-{index}")
                          for index, label in enumerate(labels))
        questions += (QuestionObservation("Still empty", ControlType.TEXT,
                      required=True, target_ref="empty-ref"),)
        observation = ApplicationObservation("fresh-fields", "https://example.test/apply",
                                             page, questions=questions)
        for question in questions:
            self.store.add_report_entry(task.id, page_or_step=page,
                visible_label=question.label, semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED, action="deferred",
                verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
                reason="no_safe_answer", requiredness="required",
                question_identity=question.report_identity())

        class PassiveBrowser:
            calls = 0
            async def observe(self):
                self.calls += 1
                return observation

        browser = PassiveBrowser()
        self.windows.by_task[task.id] = "window-fields"
        self.windows.browser_for = lambda _window: browser
        self.windows.run_browser = lambda _window, operation: asyncio.run(operation)
        fields = {field["label"]: field for field in
                  self.control.diagnose_current_fields(task.id)["fields"]}
        report = self.control.get_application_report(task.id)
        cards = {card["visible_label"]: card for card in report["entries"]}
        for label in labels:
            self.assertTrue(fields[label]["satisfied"])
            self.assertFalse(fields[label]["blocking"])
            self.assertEqual(fields[label]["report_group"], "completed")
            self.assertEqual(cards[label]["action"], "manual_complete")
            self.assertFalse(cards[label]["is_blocking"])
            self.assertEqual(len([row for row in self.store.get_report(task.id).entries
                                  if row.visible_label == label]), 2)
        self.assertEqual(cards["Still empty"]["report_group"], "needs_attention")
        self.assertEqual(report["needs_attention_count"], 1)
        self.assertEqual(browser.calls, 1)
        self.assertEqual(self.windows.front, [])

    def test_freshly_satisfied_attested_field_keeps_human_history_separate(self):
        task = self.queue("attested-now-observed")
        self.pause(task)
        question = QuestionObservation("City", ControlType.TEXT, required=True,
                                       current_value="Filled on employer page", target_ref="fresh-city")
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label=question.label, semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity=question.report_identity())
        self.store.resolve_field_by_human(pending.id,
            authorization=self.control._owner(HumanAction.RESOLVE_FIELD))
        observation = ApplicationObservation("fresh-city", "https://example.test/apply",
                                             "page 1", questions=(question,))
        class PassiveBrowser:
            async def observe(self): return observation
        self.windows.browser_for = lambda _window: PassiveBrowser()
        self.windows.run_browser = lambda _window, operation: asyncio.run(operation)
        diagnostic = self.control.diagnose_current_fields(task.id)["fields"][0]
        self.assertTrue(diagnostic["satisfied"])
        self.assertFalse(diagnostic["blocking"])
        self.assertEqual(diagnostic["report_group"], "completed")
        current = next(entry for entry in self.store.current_report_entries(task.id)
                       if entry.visible_label == "City")
        self.assertEqual((current.action, current.verification),
                         ("manual_complete", Verification.NOT_ATTEMPTED))
        history = [entry for entry in self.store.get_report(task.id).entries
                   if entry.visible_label == "City"]
        self.assertIn("human_resolved", [entry.action for entry in history])
        self.assertEqual(len(history), 3)

    def test_resume_keeps_unknown_page_form_blockers_in_attention(self):
        task = self.queue("unknown-page-refresh")
        self.store.add_report_entry(task.id, page_or_step="Application page",
            visible_label="Country / Territory Phone Code", semantic_key=None,
            kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
            action="deferred", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.PENDING, reason="unsupported_control",
            requiredness="required", question_identity="Country / Territory Phone Code")
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                               blocker=Blocker.OTHER.value, current_page_or_step="UNKNOWN")
        self.control.resume_application(task.id)
        report = self.control.get_application_report(task.id)["entries"]
        field = next(entry for entry in report
                     if entry["visible_label"] == "Country / Territory Phone Code")
        self.assertTrue(field["is_blocking"])
        self.assertEqual(field["report_group"], "needs_attention")
        self.assertEqual(sum(item["task_id"] == task.id
                             for item in self.control.list_attention_required()), 1)

    def test_repeated_scoped_fields_stay_distinct_and_technical_stays_last(self):
        task = self.queue("scoped-report")
        for scope in ("experience 1", "experience 2"):
            self.store.add_report_entry(task.id, page_or_step="Experience",
                visible_label="Job Title", semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.SKIPPED, action="optional_skipped",
                verification=Verification.NOT_ATTEMPTED,
                review_state=ReviewState.NOT_REQUIRED, requiredness="optional",
                question_identity=f'["","job title","experience","{scope}","0"]')
        for action in ("unknown", "application"):
            self.store.add_report_entry(task.id, page_or_step="https://example.test/apply",
                visible_label="Page classification", semantic_key="page_classification",
                kind=ReportKind.FIELD, provenance=Provenance.DETERMINISTIC,
                action=action, verification=Verification.VERIFIED,
                review_state=ReviewState.NOT_REQUIRED)
        entries = self.control.get_application_report(task.id)["entries"]
        self.assertEqual(len([e for e in entries if e["visible_label"] == "Job Title"]), 2)
        self.assertEqual(entries[-1]["category"], "technical")
        self.assertEqual(entries[-1]["action"], "application")

    def test_stale_pending_history_does_not_block_final_review(self):
        task = self.queue("review-history")
        for action, state in (("deferred", ReviewState.PENDING),
                              ("manual_complete", ReviewState.NOT_REQUIRED)):
            self.store.add_report_entry(task.id, page_or_step="Application",
                visible_label="Referral source", semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED if state is ReviewState.PENDING
                           else Provenance.SKIPPED,
                action=action, verification=Verification.NOT_ATTEMPTED,
                review_state=state,
                reason="unsupported_control", requiredness="required",
                question_identity="referral-source")
        self.store._transition(task.id, TaskStatus.READY_FOR_REVIEW, Ownership.HUMAN_OWNED)
        self.assertEqual(self.control.get_application(task.id)["pending_review_count"], 0)
        authorization = HumanAuthorization(HumanActor.LOCAL_OWNER, HumanAction.FINAL_REVIEW)
        checked = self.store.mark_review_checked(task.id, authorization=authorization)
        self.assertIsNotNone(checked.review_checked_at)

    def test_unknown_classification_keeps_last_observed_required_fields_in_attention(self):
        task = self.queue("last-form-page")
        page = "Application form"
        for label, requiredness, state, action in (
                ("Country / Territory Phone Code", "required", ReviewState.PENDING, "deferred"),
                ("How Did You Hear About Us?", "required", ReviewState.PENDING, "deferred"),
                ("Optional source", "optional", ReviewState.PENDING, "deferred"),
                ("Unknown source", "unknown", ReviewState.PENDING, "deferred"),
                ("First Name", "required", ReviewState.NOT_REQUIRED, "filled_text"),
                ("Previously worked here", "required", ReviewState.NOT_REQUIRED,
                 "manual_complete")):
            self.store.add_report_entry(task.id, page_or_step=page, visible_label=label,
                semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED if state is ReviewState.PENDING
                           else Provenance.SKIPPED if action == "manual_complete"
                           else Provenance.VERIFIED_PROFILE,
                action=action, verification=Verification.VERIFIED if action == "filled_text"
                       else Verification.NOT_ATTEMPTED, review_state=state,
                reason="unsupported_control" if state is ReviewState.PENDING or
                       action == "manual_complete" else None,
                requiredness=requiredness, question_identity=label)
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="items selected", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="unsupported_control", requiredness="unknown")
        self.store.add_report_entry(task.id, page_or_step="https://example.test/apply",
            visible_label="Page classification", semantic_key="page_classification",
            kind=ReportKind.FIELD, provenance=Provenance.DETERMINISTIC, action="unknown",
            verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED)
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                               blocker=Blocker.OTHER.value, current_page_or_step="UNKNOWN")
        entries = self.control.get_application_report(task.id)["entries"]
        self.assertEqual([entry["visible_label"] for entry in entries[:2]],
                         ["Country / Territory Phone Code", "How Did You Hear About Us?"])
        self.assertTrue(all(entry["report_group"] == "needs_attention" and
                            entry["is_blocking"] and entry["requires_user_action"]
                            for entry in entries[:2]))
        self.assertTrue(all(entry["report_group"] == "needs_review" and
                            not entry["is_blocking"] for entry in entries[2:4]))
        self.assertTrue(all(entry["report_group"] == "completed" and
                            not entry["is_blocking"] for entry in entries[4:6]))
        self.assertEqual(entries[-1]["report_group"], "technical")
        self.assertNotIn("items selected", [entry["visible_label"] for entry in entries])
        self.assertIn("items selected", [entry.visible_label
                                         for entry in self.store.get_report(task.id).entries])

        # A fresh manually observed answer changes the same card's canonical
        # state; the historical pending row can remain in SQLite.
        self.store.add_report_entry(task.id, page_or_step=page,
            visible_label="How Did You Hear About Us?", semantic_key=None,
            kind=ReportKind.FIELD, provenance=Provenance.SKIPPED,
            action="manual_complete", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.NOT_REQUIRED, reason="unsupported_control",
            requiredness="required", question_identity="How Did You Hear About Us?")
        refreshed = self.control.get_application_report(task.id)["entries"]
        matching = [entry for entry in refreshed
                    if entry["visible_label"] == "How Did You Hear About Us?"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["report_group"], "completed")
        self.assertFalse(matching[0]["is_blocking"])
        self.assertEqual(len([entry for entry in refreshed if entry["is_blocking"]]), 1)
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                               blocker=Blocker.LOGIN_REQUIRED.value, current_page_or_step="AUTH")
        auth_entries = self.control.get_application_report(task.id)["entries"]
        self.assertFalse(any(entry["is_blocking"] for entry in auth_entries))

    def pause(self, task):
        window_id = self.windows.allocate(task.id)
        self.store.start(task.id, browser_session_id=window_id)
        return self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")

    def test_resolve_field_command_changes_one_blocker_without_browser_action(self):
        task = self.queue("manual-resolution")
        self.pause(task)
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity='["","prior work","","","0"]')
        another = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Other question", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity='["","other question","","","0"]')
        result = self.request("resolve_field", {"entry_id": pending.id})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["action"], "human_resolved")
        self.assertEqual(result["result"]["verification"], "not_attempted")
        report = self.control.get_application_report(task.id)["entries"]
        self.assertEqual(next(e for e in report if e["visible_label"] == "Prior work")["report_group"],
                         "completed")
        self.assertEqual(next(e for e in report if e["visible_label"] == "Other question")["report_group"],
                         "needs_attention")
        self.assertEqual(self.control.get_application_report(task.id)["needs_attention_count"], 1)
        self.assertEqual(self.windows.front, [])
        self.assertEqual(self.request("resolve_field", {"entry_id": pending.id})["error"]["code"],
                         "INVALID_TRANSITION")
        self.assertEqual(self.store.get_report_entry(another.id).review_state, ReviewState.PENDING)

    def test_attested_field_keeps_machine_state_and_others_stay_blocking(self):
        task = self.queue("attestation-diagnostic")
        self.pause(task)
        city = QuestionObservation("City", ControlType.TEXT, required=True, target_ref="c1")
        state = QuestionObservation("State", ControlType.CHOICE, required=True,
                                    current_value="Select One", target_ref="s1")
        observation = ApplicationObservation("fresh", "https://example.test/apply", "page 1",
                                             questions=(city, state))

        class PassiveBrowser:
            async def observe(self):
                return observation

        self.windows.browser_for = lambda _window: PassiveBrowser()
        self.windows.run_browser = lambda _window, operation: asyncio.run(operation)
        pending = {}
        for question in (city, state):
            pending[question.label] = self.store.add_report_entry(task.id, page_or_step="page 1",
                visible_label=question.label, semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED, action="deferred",
                verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
                requiredness="required", question_identity=question.report_identity())
        # Widget status text persisted by an older parser must not become a card.
        self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Options Expanded", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="no_safe_answer", requiredness="unknown",
            question_identity='["","options expanded","","","0"]')
        self.assertTrue(self.request("resolve_field", {"entry_id": pending["City"].id})["ok"])

        report = self.control.get_application_report(task.id)
        cards = {entry["visible_label"]: entry for entry in report["entries"]}
        self.assertNotIn("Options Expanded", cards)
        self.assertEqual(cards["City"]["report_group"], "completed")
        self.assertEqual(cards["City"]["action"], "human_resolved")
        self.assertEqual(cards["City"]["verification"], "not_attempted")
        self.assertEqual(cards["State"]["report_group"], "needs_attention")
        self.assertTrue(cards["State"]["is_blocking"])
        self.assertEqual(report["needs_attention_count"],
                         sum(e["report_group"] == "needs_attention" for e in report["entries"]))
        self.assertEqual(report["needs_review_count"],
                         sum(e["report_group"] == "needs_review" for e in report["entries"]))
        self.assertEqual((report["needs_attention_count"], report["needs_review_count"]), (1, 0))

        fields = {field["label"]: field for field in
                  self.request("diagnose_current_fields", {"task_id": task.id})["result"]["fields"]}
        # Machine observation is unchanged by the attestation; both facts remain.
        self.assertFalse(fields["City"]["satisfied"])
        self.assertTrue(fields["City"]["blocking"])
        self.assertEqual(fields["City"]["manual_resolution"], "human_attested")
        self.assertEqual(fields["City"]["report_group"], "completed")
        self.assertFalse(fields["State"]["satisfied"])
        self.assertTrue(fields["State"]["blocking"])
        self.assertIsNone(fields["State"]["manual_resolution"])
        self.assertEqual(fields["State"]["report_group"], "needs_attention")
        # Attestation never touches the employer page or submission state.
        self.assertEqual(self.windows.front, [])
        self.assertIsNot(self.store.get_task(task.id).status, TaskStatus.SUBMITTED_BY_HUMAN)

    def test_undo_mark_resolved_returns_one_field_to_attention_without_browser(self):
        task = self.queue("undo-attestation")
        self.pause(task)
        entries = {}
        for label in ("City", "State"):
            entries[label] = self.store.add_report_entry(task.id, page_or_step="page 1",
                visible_label=label, semantic_key=None, kind=ReportKind.FIELD,
                provenance=Provenance.UNRESOLVED, action="deferred",
                verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
                requiredness="required", question_identity=f'["","{label.casefold()}","","","0"]')
            self.assertTrue(self.request("resolve_field", {"entry_id": entries[label].id})["ok"])
        report = self.control.get_application_report(task.id)
        attested = {e["visible_label"]: e for e in report["entries"]}
        self.assertEqual(report["needs_attention_count"], 0)
        self.assertEqual(self.request("undo_field_resolution",
                                      {"entry_id": entries["City"].id})["error"]["code"],
                         "INVALID_TRANSITION")
        result = self.request("undo_field_resolution", {"entry_id": attested["City"]["entry_id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["report_group"], "needs_attention")
        self.assertTrue(result["result"]["is_blocking"])
        report = self.control.get_application_report(task.id)
        cards = {e["visible_label"]: e for e in report["entries"]}
        self.assertEqual(cards["City"]["report_group"], "needs_attention")
        self.assertEqual(cards["City"]["reason"], "human_attestation_undone")
        self.assertEqual(cards["State"]["action"], "human_resolved")
        self.assertEqual(cards["State"]["report_group"], "completed")
        self.assertEqual(report["needs_attention_count"], 1)
        # A second undo of the same attestation is refused.
        self.assertEqual(self.request("undo_field_resolution",
                                      {"entry_id": attested["City"]["entry_id"]})["error"]["code"],
                         "INVALID_TRANSITION")
        task_after = self.store.get_task(task.id)
        self.assertEqual(task_after.status, TaskStatus.HUMAN_PAUSED)
        self.assertIsNone(task_after.resume_requested_at)
        self.assertEqual(self.windows.front, [])
        self.assertIsNone(task_after.review_checked_at)

    def test_open_application_is_explicit_and_does_not_foreground_on_selection(self):
        task = self.queue("Reopen")
        self.pause(task)
        old = self.windows.window_for_task(task.id)
        self.windows.release(old)
        self.assertFalse(self.request("get_application", {"task_id": task.id})["result"]["window_available"])
        self.assertEqual(self.windows.front, [])
        opened = self.request("open_application", {"task_id": task.id})
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["result"]["status"], "queued")
        self.assertEqual(self.windows.front, [])
        self.assertEqual(self.request("open_application", {"task_id": task.id})["error"]["code"],
                         "NOT_RESUMABLE")

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
