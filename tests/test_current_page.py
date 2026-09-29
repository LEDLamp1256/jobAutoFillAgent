"""Offline V2-11 current-page, reporting, and Resume contracts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from jobagent.application_launcher import ApplicationLauncher
from jobagent.batch_domain import HumanAction, Provenance, ReviewState, TaskStatus, Verification
from jobagent.browser import BrowserActionResult
from jobagent.controller import ApplicationController, ControllerStop
from jobagent.dedupe import ListingInput
from jobagent.domain import (ActionOutcome, ActionPolicy, ActionStatus, ApplicationObservation,
                             ApplicationSession, ControlType, NavigationControl, NavigationKind,
                             QuestionObservation)
from jobagent.persistence import BatchStore
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from jobagent.runtime_worker import LaunchAndLoginWorker, LocalLoginConfiguration
from jobagent.scheduler import ApplicationScheduler
from tests.test_resolution import profile_data
from tests.test_scheduler import owner


class PageBrowser:
    def __init__(self, *, terminal=True, ambiguous=False, failed_verify=False,
                 unknown_required=True, extra_fields=()):
        self.values = {}
        self.counter = 0
        self.last = None
        self.calls = []
        self.terminal = terminal
        self.ambiguous = ambiguous
        self.failed_verify = failed_verify
        self.annotations = {}
        self.unknown_required = unknown_required
        self.extra_fields = extra_fields

    def observation(self):
        self.counter += 1
        oid = f"obs-{self.counter}"
        def field(label, kind=ControlType.TEXT, options=(), required=True):
            return QuestionObservation(label, kind, options=options, required=required,
                                       current_value=self.values.get(label), target_ref=f"{oid}-{label}")
        questions = (field("First name"), field("Last name"), field("Email"),
                     field("Are you authorized to work in the US?", ControlType.CHOICE, ("Yes", "No")),
                     field("Fixture clearance code", required=self.unknown_required),
                     *(field(extra[0], extra[1], required=extra[2] if len(extra) > 2 else True)
                       for extra in self.extra_fields))
        nav = [NavigationControl("Submit application" if self.terminal else "Continue",
                                 NavigationKind.SUBMIT if self.terminal else NavigationKind.ADVANCE,
                                 f"{oid}-nav")]
        if self.ambiguous:
            nav.append(NavigationControl("Other", NavigationKind.UNKNOWN, f"{oid}-other"))
        self.last = ApplicationObservation(oid, "http://127.0.0.1/fixture", "Application details",
                                           questions=questions, navigation_controls=tuple(nav))
        return self.last

    async def observe(self):
        self.calls.append("observe")
        return self.observation()

    async def fill_text(self, action, session):
        ActionPolicy.authorize(action, session)
        self.calls.append("fill")
        if not self.failed_verify:
            label = next(q.label for q in self.last.questions if q.target_ref == action.target_ref)
            self.values[label] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

    async def choose_option(self, action, session):
        ActionPolicy.authorize(action, session)
        self.calls.append("choose")
        self.values["Are you authorized to work in the US?"] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

    async def activate_navigation(self, *_):
        raise AssertionError("current-page automation must not navigate")

    async def annotate_field(self, ref, observation_id, state):
        assert observation_id == self.last.observation_id
        question = next(q for q in self.last.questions if q.target_ref == ref)
        self.annotations[question.label] = state


def resolver():
    return DeterministicAnswerResolver(CandidateProfile.from_mapping(profile_data()))


class ControllerPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_submit_does_not_prevent_safe_fill(self):
        browser = PageBrowser()
        session = ApplicationSession("fixture", "http://127.0.0.1/fixture")
        result = await ApplicationController(browser, resolver()).run(session, current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.values["First name"], "Ada")
        self.assertEqual(browser.values["Last name"], "Lovelace")
        self.assertEqual(browser.values["Email"], "ada@example.test")
        self.assertEqual(browser.values["Are you authorized to work in the US?"], "Yes")
        self.assertEqual([f.reason for f in result.fields if f.needs_review], ["no_safe_answer"])
        self.assertEqual(browser.calls.count("fill") + browser.calls.count("choose"), 4)
        self.assertEqual(len(session.observations), 5)
        self.assertEqual(set(browser.calls), {"observe", "fill", "choose"})

    async def test_conflict_and_unverified_attempt_never_become_verified(self):
        browser = PageBrowser(failed_verify=True)
        browser.values["Last name"] = "Conflicting"
        session = ApplicationSession("fixture", "http://127.0.0.1/fixture")
        result = await ApplicationController(browser, resolver()).run(session, current_page_only=True)
        by_label = {f.question.label: f for f in result.fields}
        self.assertEqual(by_label["First name"].reason, "unable_to_freshly_verify")
        self.assertFalse(by_label["First name"].verified)
        self.assertEqual(by_label["Last name"].reason, "conflicting_existing_value")
        self.assertEqual(browser.values["Last name"], "Conflicting")

    async def test_manual_value_reassessed_without_auto_provenance(self):
        browser = PageBrowser()
        first = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
        browser.values["Fixture clearance code"] = "owner supplied"
        second = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(second.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(next(f.action for f in second.fields if f.question.label == "Fixture clearance code"),
                         "manual_complete")
        self.assertEqual(browser.calls.count("fill"), 3)

    async def test_advance_and_ambiguous_navigation_pause_without_click(self):
        for browser, expected_reason in ((PageBrowser(terminal=False),
                                          "page complete; automatic advancement is outside V2-11"),
                                         (PageBrowser(ambiguous=True),
                                          "terminal or navigation state is ambiguous")):
            browser.values["Fixture clearance code"] = "owner supplied"
            result = await ApplicationController(browser, resolver()).run(
                ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
            self.assertEqual(result.stop, ControllerStop.STOPPED_BEFORE_ADVANCE)
            self.assertEqual(result.reason, expected_reason)
            self.assertNotIn("advance", browser.calls)

    async def test_optional_unknown_skipped_but_unsupported_and_narrative_review(self):
        browser = PageBrowser(unknown_required=False, extra_fields=(
            ("Consent to background check", ControlType.TOGGLE),
            ("Why do you want to work here?", ControlType.TEXT),
            ("Resume", ControlType.FILE)))
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        fields = {f.question.label: f for f in result.fields}
        self.assertEqual(fields["Fixture clearance code"].action, "optional_skipped")
        self.assertEqual(fields["Consent to background check"].reason, "unsupported_control")
        self.assertEqual(fields["Why do you want to work here?"].reason, "narrative_deferred")
        self.assertEqual(fields["Resume"].reason, "unsupported_control")
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)

    async def test_optional_human_owned_controls_outrank_optional_skip(self):
        browser = PageBrowser(unknown_required=False, extra_fields=(
            ("Consent to background check", ControlType.TOGGLE, False),
            ("Resume", ControlType.FILE, False),
            ("Why do you want to work here?", ControlType.TEXT, False),
            ("Gender", ControlType.CHOICE, False),
            ("Work authorization", ControlType.TEXT, False)))
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        fields = {field.question.label: field for field in result.fields}
        self.assertEqual(fields["Fixture clearance code"].action, "optional_skipped")
        expected = {
            "Consent to background check": "unsupported_control",
            "Resume": "unsupported_control",
            "Why do you want to work here?": "narrative_deferred",
            "Gender": "requires_human_review",
            "Work authorization": "ambiguous_semantic_mapping",
        }
        self.assertEqual({label: fields[label].reason for label in expected}, expected)
        self.assertTrue(all(fields[label].needs_review and
                            fields[label].action != "optional_skipped" for label in expected))
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)

    async def test_optional_unsupported_toggle_alone_blocks_terminal_handoff(self):
        browser = PageBrowser(extra_fields=(("Consent to background check", ControlType.TOGGLE, False),))
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        toggle = next(field for field in result.fields if field.question.label == "Consent to background check")
        self.assertEqual((toggle.action, toggle.reason), ("deferred", "unsupported_control"))
        self.assertTrue(toggle.needs_review)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.calls.count("fill") + browser.calls.count("choose"), 4)


class FakeWindows:
    def __init__(self, browser):
        self.browser = browser
        self.task_id = None

    def open_count(self): return int(self.task_id is not None)
    def window_for_task(self, task_id): return "window" if self.task_id == task_id else None
    def exists(self, window_id): return window_id == "window"
    def allocate(self, task_id): self.task_id = task_id; return "window"
    def browser_for(self, window_id): return self.browser
    def opened_url(self, window_id): return "http://127.0.0.1/fixture"
    def run_browser(self, window_id, coroutine): return asyncio.run(coroutine)
    def bring_to_front(self, window_id): pass
    def release(self, window_id): self.task_id = None


class RuntimePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BatchStore(Path(self.temp.name) / "test.sqlite3")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(profile_data()), encoding="utf-8")
        self.browser = PageBrowser()
        self.windows = FakeWindows(self.browser)
        self.worker = LaunchAndLoginWorker(self.store, ApplicationLauncher(self.windows),
                                           LocalLoginConfiguration(config))
        self.scheduler = ApplicationScheduler(self.store, self.worker, self.windows,
                                              max_open_applications=1)
        run = self.store.create_run(("fixture",), 1)
        listing, _ = self.store.register_listing(ListingInput(
            "fixture", "Fixture", "Engineer", "http://127.0.0.1/fixture",
            application_url="http://127.0.0.1/fixture"))
        self.task = self.store.queue_task(run.id, listing.id)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_report_annotations_and_resume(self):
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        report = self.store.get_report(self.task.id)
        self.assertEqual(len(report.entries), 5)
        self.assertEqual(len([e for e in report.entries if e.verification is Verification.VERIFIED]), 4)
        self.assertEqual(len([e for e in report.entries if e.review_state is ReviewState.PENDING]), 1)
        self.assertEqual(self.browser.annotations["First name"], "verified")
        self.assertEqual(self.browser.annotations["Fixture clearance code"], "needs-review")
        self.assertEqual(report.entries[-1].reason, "no_safe_answer")
        self.assertNotIn("obs-", repr(report))
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
        self.browser.values["Fixture clearance code"] = "owner supplied"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        third = self.scheduler.step()
        self.assertEqual(third.task.status, TaskStatus.READY_FOR_REVIEW)
        self.assertEqual(self.browser.annotations["Fixture clearance code"], "clear")
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
        self.assertEqual(self.browser.calls.count("fill"), 3)
        self.assertFalse(any(e.provenance is Provenance.HUMAN_PROVIDED for e in report.entries))

    def test_advance_boundary_has_report_reason_and_zero_navigation(self):
        self.browser.terminal = False
        self.browser.values["Fixture clearance code"] = "owner supplied"
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertIn("automatic_advancement_outside_v2_11",
                      [e.reason for e in self.store.get_report(self.task.id).entries])
        self.assertEqual(set(self.browser.calls) - {"observe", "fill", "choose"}, set())

    def test_ambiguous_navigation_has_report_reason(self):
        self.browser.ambiguous = True
        self.browser.values["Fixture clearance code"] = "owner supplied"
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertIn("ambiguous_navigation",
                      [e.reason for e in self.store.get_report(self.task.id).entries])

    def test_annotation_failure_cannot_cancel_safe_fill_or_report(self):
        async def fail_annotation(*_):
            self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
            raise PermissionError("missing current target")
        self.browser.annotate_field = fail_annotation
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.browser.values["First name"], "Ada")
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)

    def test_optional_human_owned_fields_report_and_annotate_once(self):
        self.browser.values["Fixture clearance code"] = "owner supplied"
        self.browser.extra_fields = (
            ("Consent to background check", ControlType.TOGGLE, False),
            ("Resume", ControlType.FILE, False),
            ("Why do you want to work here?", ControlType.TEXT, False))
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual({issue.reason for issue in first.outcome.issues},
                         {"unsupported_control", "narrative_deferred"})
        report = self.store.get_report(self.task.id)
        pending = {entry.visible_label: entry for entry in report.entries
                   if entry.review_state is ReviewState.PENDING}
        self.assertEqual({label: entry.reason for label, entry in pending.items()}, {
            "Consent to background check": "unsupported_control",
            "Resume": "unsupported_control",
            "Why do you want to work here?": "narrative_deferred",
        })
        self.assertTrue(all(entry.provenance is Provenance.UNRESOLVED and
                            entry.verification is Verification.NOT_ATTEMPTED for entry in pending.values()))
        self.assertEqual(len(report.entries), 7)
        self.assertNotIn("obs-", repr(report))
        self.assertNotIn("owner supplied", repr(report))
        self.assertTrue(all(self.browser.annotations[label] == "needs-review" for label in pending))
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 7)
        self.assertEqual(self.browser.calls.count("fill") + self.browser.calls.count("choose"), 4)
        self.assertTrue(all(self.browser.annotations[label] == "needs-review" for label in pending))


if __name__ == "__main__":
    unittest.main()
