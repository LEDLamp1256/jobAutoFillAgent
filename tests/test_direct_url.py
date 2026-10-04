"""Offline direct URL intake and fresh-page routing tests."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jobagent.batch_domain import (Blocker, FailureDiagnostic, FailureReason, HumanAction,
                                   HumanActor, HumanAuthorization, TaskStatus)
from jobagent.control_plane import LocalControlPlane
from jobagent.control_plane_stdio import dispatch
from jobagent.dedupe import ListingInput
from jobagent.domain import ApplicationObservation, ControlType, NavigationControl, QuestionObservation
from jobagent.application_launcher import ApplicationLauncher, application_url
from jobagent.managed_runtime import MCPManagedWindows
from jobagent.mcp_browser import MCPServerCommand
from jobagent.persistence import BatchStore
from jobagent.runtime_worker import LaunchAndLoginWorker, LocalLoginConfiguration, classify_direct_page
from jobagent.scheduler import ApplicationScheduler
from jobagent.snapshot import SnapshotEmpty, SnapshotNormalizer
from tests.test_managed_runtime import FakeManagedBrowser


class DirectBrowser(FakeManagedBrowser):
    def __init__(self, observation):
        super().__init__()
        self.initial = observation

    async def navigate(self, url):
        self.navigations.append(url)
        if isinstance(self.initial, BaseException):
            raise self.initial
        self.current = self.initial
        return self.current


class DirectURLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "batch.sqlite3"
        self.store = BatchStore(self.db)
        self.observation = ApplicationObservation("one", "https://example.test/jobs/1",
            "Software Engineer", navigation_controls=(NavigationControl("Apply"),))
        self.browsers = []
        def factory(_command):
            browser = DirectBrowser(self.observation)
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.scheduler = ApplicationScheduler(self.store,
            LaunchAndLoginWorker(self.store, ApplicationLauncher(self.windows),
                                 LocalLoginConfiguration(Path(self.temp.name) / "missing.json")),
            self.windows, max_open_applications=3)
        self.control = LocalControlPlane(self.store, self.scheduler)

    def tearDown(self):
        self.windows.close()
        self.store.close()
        self.temp.cleanup()

    def intake(self, url):
        return dispatch({"id": "1", "method": "start_application_url", "params": {"url": url}}, self.control)

    def test_validation_trim_durable_creation_and_reload(self):
        for invalid in ("", "example.test/jobs/1", "ftp://example.test/a", "https://",
                        "https://example.test/a b", "http://example.test/jobs/1"):
            self.assertFalse(self.intake(invalid)["ok"])
        self.assertEqual(self.store.list_tasks(), ())
        self.assertEqual(self.store.list_runs(), ())
        result = self.intake("  https://example.test/jobs/1?utm_source=mail  ")
        self.assertTrue(result["ok"])
        task_id = result["result"]["task_id"]
        listing = self.store.get_listing(result["result"]["listing_id"])
        self.assertEqual(listing.application_url, "https://example.test/jobs/1?utm_source=mail")
        self.assertEqual(listing.canonical_application_url, "https://example.test/jobs/1")
        self.assertEqual(self.store.get_task(task_id).status, TaskStatus.QUEUED)
        self.assertEqual(self.intake("https://example.test/jobs/1")["result"]["task_id"], task_id)
        self.assertEqual(len(self.store.list_runs()), 1)
        loopback = self.intake("http://localhost:8765/apply")
        self.assertTrue(loopback["ok"])
        self.assertEqual(loopback["result"]["source_url"], "http://localhost:8765/apply")
        with BatchStore(self.db) as reopened:
            self.assertEqual(reopened.get_task(task_id).status, TaskStatus.QUEUED)
            self.assertEqual(reopened.get_listing(listing.id).application_url, listing.application_url)

    def test_posting_stops_without_apply_and_reports_live_url(self):
        task = self.intake("https://example.test/jobs/1")["result"]
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertEqual((view["classification"], view["status"], view["live_url"]),
                         ("JOB_POSTING", "human_paused", self.observation.location))
        self.assertEqual(self.browsers[0].navigations, ["https://example.test/jobs/1"])
        self.assertEqual(self.browsers[0].foreground_count, 0)
        with BatchStore(self.db) as reopened:
            self.assertEqual(reopened.get_task(task["task_id"]).status, TaskStatus.HUMAN_PAUSED)
            self.assertEqual(len(reopened.get_report(task["task_id"]).entries), 2)

    def test_failed_application_is_inspectable_and_recovers_by_observation_only(self):
        task_id = self.intake("https://example.test/jobs/1")["result"]["task_id"]
        self.scheduler.step()
        old_window = self.windows.window_for_task(task_id)
        self.store.fail(task_id, reason=FailureReason.BROWSER_ERROR,
                        diagnostic=FailureDiagnostic("fill", "timeout", "Managed browser operation timed out"))
        self.scheduler.release_window(task_id)
        self.assertNotEqual(old_window, self.windows.window_for_task(task_id))
        before = self.control.get_application(task_id)
        self.assertEqual(before["status"], "failed")
        self.assertTrue(before["recovery_available"])
        self.assertEqual(before["failure_events"][0]["stage"], "fill")
        self.assertIn(task_id, [item["task_id"] for item in self.control.list_attention_required()])
        self.assertEqual(len(self.browsers), 1)  # passive inspection did not open a browser
        self.observation = ApplicationObservation("fresh", "https://example.test/apply", "Application",
            questions=(QuestionObservation("Prior work", ControlType.CHOICE, required=True),),
            navigation_controls=(NavigationControl("Submit"),))
        recovered = dispatch({"id": "r", "method": "recover_application",
                              "params": {"task_id": task_id}}, self.control)
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["result"]["status"], "human_paused")
        self.assertTrue(recovered["result"]["resume_available"])
        self.assertEqual(recovered["result"]["current_page_or_step"], "Application")
        self.assertEqual(len(recovered["result"]["failure_events"]), 1)
        report = self.control.get_application_report(task_id)
        prior = next(item for item in report["entries"] if item["visible_label"] == "Prior work")
        self.assertEqual(prior["report_group"], "needs_attention")
        self.assertEqual(prior["reason"], "no_safe_answer")
        self.assertEqual(prior["verification"], "not_attempted")
        self.assertEqual(len(self.browsers), 2)
        self.assertEqual(self.browsers[-1].navigations, ["https://example.test/jobs/1"])
        self.assertEqual(self.browsers[-1].foreground_count, 0)
        self.assertTrue(all(call[0] == "observe" for call in self.browsers[-1].calls))
        browser_calls = list(self.browsers[-1].calls)
        resolved = dispatch({"id": "m", "method": "resolve_field",
                             "params": {"entry_id": prior["entry_id"]}}, self.control)
        self.assertTrue(resolved["ok"], resolved)
        self.assertEqual(resolved["result"]["action"], "human_resolved")
        self.assertEqual(self.browsers[-1].calls, browser_calls)
        self.assertEqual(self.control.resume_application(task_id)["status"], "queued")
        self.assertIn(HumanAction.RECOVER_APPLICATION,
                      [action.action for action in self.store.human_actions("task", task_id)])
        self.assertEqual(self.store.human_actions("task", task_id)[-1].action,
                         HumanAction.RESUME)

    def test_recovery_login_pauses_and_failure_preserves_both_events(self):
        task_id = self.intake("https://example.test/jobs/1")["result"]["task_id"]
        self.scheduler.step()
        self.store.fail(task_id, reason=FailureReason.BROWSER_ERROR,
                        diagnostic=FailureDiagnostic("observe_snapshot", "timeout", "Timed out"))
        self.scheduler.release_window(task_id)
        self.observation = RuntimeError("password=private-value ref=secret-browser-ref")
        failed = dispatch({"id": "f", "method": "recover_application",
                           "params": {"task_id": task_id}}, self.control)
        self.assertFalse(failed["ok"])
        self.assertEqual(self.store.get_task(task_id).status, TaskStatus.FAILED)
        events = self.store.failure_events(task_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].stage, "recovery_navigate")
        self.assertEqual(events[-1].mode, "recovery")
        self.assertNotIn("private-value", str(events))
        self.assertNotIn("secret-browser-ref", str(events))
        self.observation = ApplicationObservation("auth", "https://example.test/login", "Sign In",
            questions=(QuestionObservation("Email", ControlType.TEXT, required=True),
                       QuestionObservation("Password", ControlType.SECRET, required=True)))
        recovered = dispatch({"id": "a", "method": "recover_application",
                              "params": {"task_id": task_id}}, self.control)
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["result"]["blocker"], Blocker.LOGIN_REQUIRED.value)
        self.assertEqual(recovered["result"]["status"], "human_paused")
        self.assertEqual(len(recovered["result"]["failure_events"]), 2)

    def test_normal_browser_failure_records_sanitized_stage_without_values_or_refs(self):
        task_id = self.intake("https://example.test/jobs/1")["result"]["task_id"]
        self.observation = RuntimeError("password=private-value ref=secret-browser-ref")
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.FAILED)
        event = self.store.failure_events(task_id)[0]
        self.assertEqual((event.stage, event.category, event.mode),
                         ("navigate", "unexpected_exception", "run"))
        self.assertNotIn("private-value", str(event))
        self.assertNotIn("secret-browser-ref", str(event))
        self.assertFalse(self.control.get_application(task_id)["window_available"])

    def test_existing_listing_without_task_is_reused_and_opens_supplied_posting(self):
        listing, _ = self.store.register_listing(ListingInput(
            "board", "Acme", "Engineer", "https://example.test/jobs/existing",
            application_url="https://example.test/apply/existing"))
        result = self.intake("https://example.test/jobs/existing")
        self.assertTrue(result["ok"])
        view = result["result"]
        self.assertEqual(view["listing_id"], listing.id)
        self.assertEqual(view["source_url"], "https://example.test/jobs/existing")
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.scheduler.step()
        self.assertEqual(self.browsers[0].navigations, ["https://example.test/jobs/existing"])
        self.assertEqual(self.control.get_application(view["task_id"])["classification"], "JOB_POSTING")
        self.assertEqual(self.store.get_listing(listing.id).application_url,
                         "https://example.test/apply/existing")

    def test_existing_task_is_returned_without_new_run(self):
        listing, _ = self.store.register_listing(ListingInput(
            "board", "Acme", "Engineer", "https://example.test/jobs/existing"))
        run = self.store.create_run(("board",), 1)
        task = self.store.queue_task(run.id, listing.id)
        result = self.intake("https://example.test/jobs/existing")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["task_id"], task.id)
        self.assertEqual(tuple(item.id for item in self.store.list_runs()), (run.id,))
        self.assertEqual(tuple(item.id for item in self.store.list_tasks()), (task.id,))

    def test_archived_human_paused_task_allows_fresh_direct_intake(self):
        url = "https://example.test/jobs/archived-pause"
        old = self.intake(url)["result"]
        self.store.start(old["task_id"], browser_session_id="historical-window")
        self.store.pause_for_human(old["task_id"], Blocker.LOGIN_REQUIRED, page_or_step="authentication")
        self.control.archive_application(old["task_id"])

        fresh = self.intake(url)
        self.assertTrue(fresh["ok"], fresh)
        new = fresh["result"]
        self.assertNotEqual(new["task_id"], old["task_id"])
        self.assertEqual(new["listing_id"], old["listing_id"])
        self.assertEqual(new["status"], "queued")
        self.assertEqual(self.store.get_task(old["task_id"]).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(len(self.store.list_runs()), 2)
        self.assertEqual(len(self.store.list_tasks()), 2)
        self.assertEqual([item["task_id"] for item in self.control.list_applications()],
                         [new["task_id"]])
        step = self.scheduler.step()
        self.assertEqual(step.task.id, new["task_id"])
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.browsers[-1].navigations, [url])

    def test_archived_failed_and_review_tasks_allow_fresh_intake(self):
        for suffix, old_state in (("failed", TaskStatus.FAILED),
                                  ("review", TaskStatus.READY_FOR_REVIEW)):
            with self.subTest(old_state=old_state):
                url = f"https://example.test/jobs/archived-{suffix}"
                old = self.intake(url)["result"]
                self.store.start(old["task_id"], browser_session_id="historical-window")
                if old_state is TaskStatus.FAILED:
                    self.store.fail(old["task_id"], reason=FailureReason.BROWSER_ERROR)
                else:
                    self.store.ready_for_review(old["task_id"])
                self.control.archive_application(old["task_id"])
                result = self.intake(url)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["result"]["status"], "queued")
                self.assertNotEqual(result["result"]["task_id"], old["task_id"])
                self.assertEqual(self.store.get_task(old["task_id"]).status, old_state)

    def test_skipped_task_allows_new_intake_and_double_request_reuses_it(self):
        url = "https://example.test/jobs/skipped"
        old = self.intake(url)["result"]
        self.control.archive_application(old["task_id"])
        self.assertEqual(self.store.get_task(old["task_id"]).status, TaskStatus.SKIPPED)
        first = self.intake(url)
        second = self.intake(url)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first["result"]["task_id"], second["result"]["task_id"])
        self.assertEqual(len(self.store.list_runs()), 2)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_unarchived_paused_and_failed_tasks_are_reused_for_recovery(self):
        for suffix, old_state in (("paused", TaskStatus.HUMAN_PAUSED),
                                  ("failed", TaskStatus.FAILED)):
            with self.subTest(old_state=old_state):
                url = f"https://example.test/jobs/{suffix}"
                old = self.intake(url)["result"]
                self.store.start(old["task_id"], browser_session_id="historical-window")
                if old_state is TaskStatus.FAILED:
                    self.store.fail(old["task_id"], reason=FailureReason.BROWSER_ERROR)
                else:
                    self.store.pause_for_human(old["task_id"], Blocker.LOGIN_REQUIRED,
                                               page_or_step="authentication")
                again = self.intake(url)
                self.assertTrue(again["ok"], again)
                self.assertEqual(again["result"]["task_id"], old["task_id"])
                self.assertEqual(again["result"]["status"], old_state.value)

    def test_ambiguous_strong_url_match_creates_no_run(self):
        url = "https://example.test/jobs/shared"
        self.store.register_listing(ListingInput("board-a", "Acme", "Engineer", listing_url=url))
        self.store.register_listing(ListingInput("board-b", "Other", "Developer",
                                                 application_url=url))
        result = self.intake(url)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "POSSIBLE_DUPLICATE")
        self.assertEqual(self.store.list_runs(), ())
        self.assertEqual(self.store.list_tasks(), ())

    def test_launcher_rejects_remote_http_even_for_direct_listing(self):
        task = self.intake("https://example.test/apply/secure")["result"]
        listing = self.store.get_listing(task["listing_id"])
        with self.assertRaises(ValueError):
            application_url(replace(listing, application_url="http://example.test/apply/insecure"))
        self.assertEqual(application_url(replace(listing, application_url="http://127.0.0.1:8765/apply")),
                         "http://127.0.0.1:8765/apply")

    def test_https_redirect_to_remote_http_never_enters_fill_workflow(self):
        self.observation = ApplicationObservation("one", "http://example.test/apply/insecure",
            "Personal Information", questions=(QuestionObservation("First Name", ControlType.TEXT),))
        task = self.intake("https://example.test/apply/secure")["result"]
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertEqual((view["classification"], view["status"], view["blocker"]),
                         ("BLOCKER", "human_paused", "other"))
        self.assertEqual(view["pending_review_count"], 0)

    def test_persistent_empty_observation_uses_existing_safe_failure_path(self):
        self.observation = SnapshotEmpty("accessibility snapshot is empty")
        task = self.intake("https://example.test/apply/secure")["result"]
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.FAILED)
        self.assertEqual(step.task.last_error, "browser_error")
        self.assertEqual(self.browsers[0].navigations, ["https://example.test/apply/secure"])
        self.assertEqual([entry.semantic_key for entry in self.store.get_report(task["task_id"]).entries],
                         ["direct_source_url"])

    def test_submitted_listing_is_rejected_without_orphan_run(self):
        listing, _ = self.store.register_listing(ListingInput(
            "board", "Acme", "Engineer", "https://example.test/jobs/submitted"))
        run = self.store.create_run(("board",), 1)
        task = self.store.queue_task(run.id, listing.id)
        self.store.start(task.id, browser_session_id="window")
        self.store.ready_for_review(task.id)
        self.store.mark_review_checked(task.id, authorization=HumanAuthorization(
            HumanActor.LOCAL_OWNER, HumanAction.FINAL_REVIEW))
        self.store.mark_submitted_by_human(task.id, authorization=HumanAuthorization(
            HumanActor.LOCAL_OWNER, HumanAction.RECORD_SUBMISSION))
        result = self.intake("https://example.test/jobs/submitted")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ALREADY_SUBMITTED")
        self.assertEqual(tuple(item.id for item in self.store.list_runs()), (run.id,))
        self.assertEqual(tuple(item.id for item in self.store.list_tasks()), (task.id,))

    def test_archiving_submitted_history_never_allows_duplicate_submission(self):
        url = "https://example.test/jobs/submitted-archive"
        task = self.intake(url)["result"]
        self.store.start(task["task_id"], browser_session_id="historical-window")
        self.store.ready_for_review(task["task_id"])
        self.store.mark_review_checked(task["task_id"], authorization=HumanAuthorization(
            HumanActor.LOCAL_OWNER, HumanAction.FINAL_REVIEW))
        self.store.mark_submitted_by_human(task["task_id"], authorization=HumanAuthorization(
            HumanActor.LOCAL_OWNER, HumanAction.RECORD_SUBMISSION))
        self.control.archive_application(task["task_id"])
        before = (len(self.store.list_runs()), len(self.store.list_tasks()))
        retry = self.intake(url)
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["error"]["code"], "ALREADY_SUBMITTED")
        self.assertEqual((len(self.store.list_runs()), len(self.store.list_tasks())), before)

    def test_application_enters_existing_current_page_controller(self):
        self.observation = ApplicationObservation("one", "https://example.test/apply/1",
            "Personal Information", questions=(QuestionObservation("First Name", ControlType.TEXT),))
        task = self.intake("https://example.test/apply/1")["result"]
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertEqual(view["classification"], "APPLICATION")
        self.assertEqual(view["status"], "human_paused")
        self.assertGreater(view["pending_review_count"], 0)
        self.assertEqual(view["live_url"], self.observation.location)

    def test_report_categories_keep_questions_separate_from_technical_details(self):
        self.observation = ApplicationObservation("one", "https://example.test/apply/1",
            "Application", questions=(QuestionObservation(
                "First Name", ControlType.TEXT, required=True),))
        task = self.intake("https://example.test/apply/1")["result"]
        self.scheduler.step()
        entries = self.control.get_application_report(task["task_id"])["entries"]
        categories = {(entry["semantic_key"], entry["visible_label"]): entry["category"]
                      for entry in entries}
        self.assertEqual(categories[("direct_source_url", "Supplied URL")], "technical")
        self.assertEqual(categories[("page_classification", "Page classification")], "technical")
        self.assertEqual(next(entry["category"] for entry in entries
                              if entry["visible_label"] == "First Name"), "application")
        self.assertEqual(next(entry["requiredness"] for entry in entries
                              if entry["visible_label"] == "First Name"), "required")

    def test_observed_job_metadata_replaces_url_fallback_and_survives_reload(self):
        self.observation = ApplicationObservation(
            "one", "https://example.test/apply/1", "My Information",
            questions=(QuestionObservation("First Name", ControlType.TEXT, required=True),),
            job_title="Software Development Engineer - US Federal", company="Example Company")
        task = self.intake("https://example.test/apply/1")["result"]
        self.assertIn("example.test", task["title"])
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertEqual((view["title"], view["company"]),
                         ("Software Development Engineer - US Federal", "Example Company"))
        self.assertEqual(view["source_url"], "https://example.test/apply/1")
        with BatchStore(self.db) as reopened:
            listing = reopened.get_listing(task["listing_id"])
            self.assertEqual((listing.title, listing.company), (view["title"], view["company"]))

    def test_snapshot_title_promotes_without_company_or_url_loss(self):
        snapshot = '''### Page
- Page URL: https://example.test/apply/1
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Software Development Engineer - US Federal" [level=2] [ref=e2]
  - heading "My Information" [level=2] [ref=e3]
  - textbox "First Name" [required] [ref=e4]
```
'''
        self.observation = SnapshotNormalizer().normalize(snapshot, "one").observation
        task = self.intake("https://example.test/apply/1")["result"]
        fallback = task["title"]
        self.assertEqual(task["company"], "example.test")
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertNotEqual(view["title"], fallback)
        self.assertEqual(view["title"], "Software Development Engineer - US Federal")
        self.assertEqual(view["company"], "")
        self.assertEqual(view["source_url"], "https://example.test/apply/1")
        self.assertEqual(view["live_url"], "https://example.test/apply/1")
        with BatchStore(self.db) as reopened:
            listing = reopened.get_listing(task["listing_id"])
            self.assertEqual(listing.title, view["title"])
            self.assertEqual(listing.application_url, view["source_url"])
            self.assertEqual(listing.company, "")

    def test_ats_brand_does_not_become_employer_but_explicit_employer_can(self):
        url = "https://workday.wd5.myworkdayjobs.com/apply/1"
        self.observation = ApplicationObservation(
            "one", url, "Software Development Engineer - US Federal",
            questions=(QuestionObservation("First Name", ControlType.TEXT, required=True),),
            job_title="Software Development Engineer - US Federal", company="Workday")
        task = self.intake(url)["result"]
        self.scheduler.step()
        view = self.control.get_application(task["task_id"])
        self.assertEqual(view["title"], "Software Development Engineer - US Federal")
        self.assertEqual(view["company"], "")
        self.assertEqual(view["source_url"], url)
        self.store.enrich_direct_listing(task["listing_id"], title=None, company="Example Employer")
        self.assertEqual(self.control.get_application(task["task_id"])["company"],
                         "Example Employer")

    def test_existing_title_with_ats_brand_company_is_corrected_on_observation(self):
        url = "https://workday.wd5.myworkdayjobs.com/apply/1"
        task = self.intake(url)["result"]
        with self.store.db:
            self.store.db.execute("""UPDATE listings SET title = ?, title_norm = ?,
                company = 'Workday', company_norm = 'workday' WHERE id = ?""",
                ("Software Development Engineer - US Federal",
                 "software development engineer - us federal", task["listing_id"]))
        self.store.enrich_direct_listing(task["listing_id"],
            title="Software Development Engineer - US Federal", company="Workday")
        view = self.control.get_application(task["task_id"])
        self.assertEqual(view["title"], "Software Development Engineer - US Federal")
        self.assertEqual(view["company"], "")
        self.assertEqual(view["source_url"], url)

    def test_auth_blocker_and_unknown_stop(self):
        cases = (
            ("Sign In", (QuestionObservation("Password", ControlType.SECRET),), "AUTH", "login_required"),
            ("Verify you are human", (), "BLOCKER", "captcha_required"),
            ("Welcome", (), "UNKNOWN", "other"),
        )
        for index, (heading, questions, category, blocker) in enumerate(cases):
            with self.subTest(category=category):
                self.observation = ApplicationObservation(str(index),
                    f"https://example.test/page/{index}", heading, questions=questions)
                result = self.intake(f"https://example.test/page/{index}")["result"]
                self.scheduler.step()
                view = self.control.get_application(result["task_id"])
                self.assertEqual((view["classification"], view["blocker"]), (category, blocker))
                self.assertEqual(view["status"], "human_paused")

    def test_classification_uses_fresh_observation_evidence(self):
        posting = self.observation
        self.assertEqual(classify_direct_page(posting), "JOB_POSTING")
        search_posting = replace(posting, questions=(QuestionObservation("Search jobs", ControlType.TEXT),))
        self.assertEqual(classify_direct_page(search_posting), "JOB_POSTING")
        self.assertEqual(classify_direct_page(replace(posting, heading="Welcome",
            navigation_controls=())), "UNKNOWN")
