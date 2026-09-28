"""Offline tests for durable batch state. Every disk database is temporary."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobagent.batch_domain import (
    Blocker, HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance,
    ReportKind, ReviewState, RunStatus, TaskStatus, Verification,
)
from jobagent.dedupe import DuplicateKind, ListingInput, canonicalize_url
from jobagent.persistence import BatchStore


def owner(action: HumanAction) -> HumanAuthorization:
    return HumanAuthorization(HumanActor.LOCAL_OWNER, action)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "batch.sqlite3"
        self.store = BatchStore(self.path)
        self.run = self.store.create_run(("board",), 5)
        self.item = ListingInput("board", "Acme", "Engineer", "https://jobs.test/123?utm_source=mail",
                                 "123", "Remote", "https://ats.test/apply?req=7")
        self.listing, _ = self.store.register_listing(self.item)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self):
        return self.store.queue_task(self.run.id, self.listing.id)

    def active_task(self):
        task = self.task()
        return self.store.start(task.id)

    def test_create_reload_entities_and_report_across_reopen(self):
        self.store.record_discovery(self.run.id)
        task = self.task()
        entry = self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="Email", semantic_key="personal.email",
            kind=ReportKind.FIELD, provenance=Provenance.VERIFIED_PROFILE, action="filled",
            verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED)
        self.store.close()
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.get_run(self.run.id).requested_sources, ("board",))
        self.assertEqual(self.store.get_run(self.run.id).discovered_count, 1)
        self.assertEqual(self.store.get_run(self.run.id).queued_count, 1)
        self.assertEqual(self.store.get_listing(self.listing.id).canonical_application_url,
                         "https://ats.test/apply?req=7")
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.QUEUED)
        self.assertEqual(self.store.get_report(task.id).entries, (entry,))

    def test_run_transitions_and_unknown_schema_rejected(self):
        self.assertEqual(self.store.set_run_status(self.run.id, RunStatus.RUNNING).status, RunStatus.RUNNING)
        self.assertEqual(self.store.set_run_status(self.run.id, RunStatus.COMPLETED).status, RunStatus.COMPLETED)
        with self.assertRaises(ValueError):
            self.store.set_run_status(self.run.id, RunStatus.RUNNING)
        self.store.db.execute("PRAGMA user_version = 99")
        self.store.close()
        with self.assertRaises(RuntimeError):
            BatchStore(self.path)
        self.store = BatchStore(":memory:")

    def test_pause_resume_requires_human_and_discards_session_identity(self):
        task = self.active_task()
        paused = self.store.pause_for_human(task.id, Blocker.MFA_REQUIRED, page_or_step="page 2")
        self.assertEqual((paused.status, paused.ownership, paused.blocker),
                         (TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED, Blocker.MFA_REQUIRED))
        self.assertEqual(paused.current_page_or_step, "page 2")
        with self.assertRaises(PermissionError):
            self.store.resume_by_human(task.id, authorization="owner")
        with self.assertRaises(PermissionError):
            self.store.resume_by_human(task.id, authorization=owner(HumanAction.FINAL_REVIEW))
        resumed = self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.assertEqual((resumed.status, resumed.ownership),
                         (TaskStatus.QUEUED, Ownership.AUTOMATION_OWNED))
        self.assertIsNone(resumed.browser_session_id)
        self.assertIsNotNone(resumed.resume_requested_at)
        self.assertEqual(resumed.resume_requested_by, HumanActor.LOCAL_OWNER.value)
        self.assertEqual(self.store.human_actions("task", task.id)[0].action, HumanAction.RESUME)
        with self.assertRaises(ValueError):
            self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.assertNotEqual(self.store.start(task.id).browser_session_id, task.browser_session_id)

    def test_human_authorization_is_typed_and_audited_after_reopen(self):
        with self.assertRaises(ValueError):
            HumanAuthorization("local_owner", HumanAction.RESUME)
        with self.assertRaises(ValueError):
            HumanAuthorization(HumanActor.LOCAL_OWNER, "resume")
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER)
        self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.store.close()
        self.store = BatchStore(self.path)
        event, = self.store.human_actions("task", task.id)
        self.assertEqual((event.actor, event.action, event.target_id),
                         (HumanActor.LOCAL_OWNER, HumanAction.RESUME, task.id))
        self.assertTrue(event.id and event.occurred_at)

    def test_review_and_submission_require_checkoff_and_authorization(self):
        task = self.active_task()
        self.store.advance_state(task.id, TaskStatus.FILLING, page_or_step="page 1")
        self.store.ready_for_review(task.id)
        with self.assertRaises(PermissionError):
            self.store.mark_submitted_by_human(task.id, authorization=owner(HumanAction.RECORD_SUBMISSION))
        with self.assertRaises(PermissionError):
            self.store.mark_review_checked(task.id, authorization="owner")
        self.store.mark_review_checked(task.id, authorization=owner(HumanAction.FINAL_REVIEW))
        with self.assertRaises(PermissionError):
            self.store.mark_submitted_by_human(task.id, authorization=owner(HumanAction.RESUME))
        submitted = self.store.mark_submitted_by_human(
            task.id, authorization=owner(HumanAction.RECORD_SUBMISSION))
        self.assertEqual(submitted.status, TaskStatus.SUBMITTED_BY_HUMAN)
        self.assertEqual(submitted.ownership, Ownership.HUMAN_OWNED)
        self.assertTrue(submitted.submitted_at and submitted.submitted_by)
        self.assertEqual(submitted.review_checked_by, HumanActor.LOCAL_OWNER.value)
        self.assertEqual([event.action for event in self.store.human_actions("task", task.id)],
                         [HumanAction.FINAL_REVIEW, HumanAction.RECORD_SUBMISSION])
        with self.assertRaises(ValueError):
            self.store.ready_for_review(task.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE tasks SET submitted_by = NULL WHERE id = ?", (task.id,))

    def test_page_report_narrative_review_and_human_revision(self):
        task = self.active_task()
        unresolved = self.store.add_report_entry(
            task.id, page_or_step="step 2", visible_label="Work authorization",
            semantic_key="employment.us_authorized", kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="candidate_fact_missing")
        draft = self.store.add_report_entry(
            task.id, page_or_step="step 2", visible_label="Why this company?",
            semantic_key="why_this_company", kind=ReportKind.NARRATIVE,
            provenance=Provenance.AI_DRAFT_REVIEW, action="drafted",
            verification=Verification.UNKNOWN, review_state=ReviewState.PENDING,
            narrative_text="A synthetic draft")
        self.assertEqual(unresolved.page_or_step, "step 2")
        self.assertNotEqual(unresolved.provenance, draft.provenance)
        self.assertEqual(draft.narrative_text, "A synthetic draft")
        self.store.ready_for_review(task.id)
        with self.assertRaises(PermissionError):
            self.store.mark_review_checked(task.id, authorization=owner(HumanAction.FINAL_REVIEW))
        self.store.review_entry_by_human(unresolved.id, authorization=owner(HumanAction.REVIEW_ENTRY))
        self.store.review_entry_by_human(draft.id, authorization=owner(HumanAction.REVIEW_ENTRY))
        self.assertEqual(self.store.get_report(task.id).entries[1].review_state, ReviewState.APPROVED)
        revised = self.store.add_report_entry(
            task.id, page_or_step="step 2", visible_label="Why this company?",
            semantic_key="why_this_company", kind=ReportKind.NARRATIVE,
            provenance=Provenance.HUMAN_PROVIDED, action="replaced",
            verification=Verification.VERIFIED, review_state=ReviewState.APPROVED,
            narrative_text="Human revision", authorization=owner(HumanAction.REPLACE_NARRATIVE))
        self.assertEqual(self.store.get_report(task.id).entries[-1], revised)
        self.assertEqual(self.store.human_actions("report_entry", revised.id)[0].action,
                         HumanAction.REPLACE_NARRATIVE)
        self.store.mark_review_checked(task.id, authorization=owner(HumanAction.FINAL_REVIEW))

    def test_report_never_accepts_field_value_or_ai_fact(self):
        task = self.task()
        with self.assertRaises(ValueError):
            self.store.add_report_entry(
                task.id, page_or_step=None, visible_label="Email", semantic_key="personal.email",
                kind=ReportKind.FIELD, provenance=Provenance.VERIFIED_PROFILE, action="filled",
                verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED,
                narrative_text="private@example.test")
        with self.assertRaises(ValueError):
            self.store.add_report_entry(
                task.id, page_or_step=None, visible_label="Email", semantic_key="personal.email",
                kind=ReportKind.FIELD, provenance=Provenance.AI_DRAFT_REVIEW, action="filled",
                verification=Verification.UNKNOWN, review_state=ReviewState.PENDING)
        with self.assertRaises(PermissionError):
            self.store.add_report_entry(
                task.id, page_or_step=None, visible_label="Email", semantic_key="personal.email",
                kind=ReportKind.FIELD, provenance=Provenance.HUMAN_PROVIDED, action="changed",
                verification=Verification.VERIFIED, review_state=ReviewState.APPROVED)
        with self.assertRaises(PermissionError):
            self.store.add_report_entry(
                task.id, page_or_step=None, visible_label="Email", semantic_key="personal.email",
                kind=ReportKind.FIELD, provenance=Provenance.HUMAN_PROVIDED, action="changed",
                verification=Verification.VERIFIED, review_state=ReviewState.APPROVED,
                authorization=owner(HumanAction.RESUME))
        with self.assertRaises(PermissionError):
            self.store.add_report_entry(
                task.id, page_or_step=None, visible_label="Email", semantic_key="personal.email",
                kind=ReportKind.FIELD, provenance=Provenance.DETERMINISTIC, action="filled",
                verification=Verification.VERIFIED, review_state=ReviewState.APPROVED)

    def test_dedupe_source_listing_application_and_fallback(self):
        source = ListingInput("board", "Changed", "Changed", "https://else.test/2", "123")
        self.assertEqual(self.store.check_duplicate(source).reason, "source_id")
        url = ListingInput("other", "Other", "Role", "https://jobs.test/123#section")
        self.assertEqual(self.store.check_duplicate(url).reason, "listing_url")
        application = ListingInput("other", "Other", "Role", "https://else.test/3",
                                   application_url="https://ats.test/apply?req=7&utm_campaign=x")
        self.assertEqual(self.store.check_duplicate(application).reason, "application_url")
        fallback = ListingInput("board", "Beta", "Designer", location="Remote")
        listed, _ = self.store.register_listing(fallback)
        same = ListingInput("another", " beta ", " DESIGNER ", location=" remote ")
        result = self.store.check_duplicate(same)
        self.assertEqual((result.kind, result.listing_id), (DuplicateKind.EXACT_DUPLICATE, listed.id))
        possible = ListingInput("other", "Acme", "Engineer", "https://else.test/4", location="Boston")
        self.assertEqual(self.store.check_duplicate(possible).kind, DuplicateKind.POSSIBLE_DUPLICATE)
        new_listing, result = self.store.register_listing(possible)
        self.assertNotEqual(new_listing.id, self.listing.id)
        self.assertEqual(result.kind, DuplicateKind.POSSIBLE_DUPLICATE)
        with self.assertRaises(PermissionError):
            self.store.queue_task(self.run.id, new_listing.id)
        with self.assertRaises(PermissionError):
            self.store.resolve_possible_duplicate_by_human(new_listing.id, authorization="owner")
        self.store.resolve_possible_duplicate_by_human(
            new_listing.id, authorization=owner(HumanAction.RESOLVE_DUPLICATE))
        self.assertEqual(self.store.human_actions("listing", new_listing.id)[0].actor,
                         HumanActor.LOCAL_OWNER)
        self.assertEqual(self.store.queue_task(self.run.id, new_listing.id).status, TaskStatus.QUEUED)

    def test_submitted_rediscovery_is_exact_and_cannot_queue(self):
        task = self.active_task()
        self.store.ready_for_review(task.id)
        self.store.mark_review_checked(task.id, authorization=owner(HumanAction.FINAL_REVIEW))
        self.store.mark_submitted_by_human(task.id, authorization=owner(HumanAction.RECORD_SUBMISSION))
        duplicate = self.store.check_duplicate(self.item)
        self.assertEqual(duplicate.kind, DuplicateKind.EXACT_DUPLICATE)
        self.assertTrue(duplicate.submitted)
        another_run = self.store.create_run(("board",), 2)
        with self.assertRaises(PermissionError):
            self.store.queue_task(another_run.id, self.listing.id)

    def test_conflicting_strong_identities_are_not_silently_merged(self):
        other, _ = self.store.register_listing(ListingInput(
            "board", "Beta", "Designer", "https://jobs.test/999", "999", "Boston"))
        conflict = ListingInput("board", "Beta", "Designer", "https://jobs.test/999", "123", "Boston")
        result = self.store.check_duplicate(conflict)
        self.assertEqual(result.kind, DuplicateKind.POSSIBLE_DUPLICATE)
        self.assertEqual(result.reason, "conflicting strong identities")
        new, _ = self.store.register_listing(conflict)
        self.assertNotIn(new.id, {other.id, self.listing.id})
        with self.assertRaises(PermissionError):
            self.store.queue_task(self.run.id, new.id)

    def test_url_canonicalization_preserves_requisition_queries(self):
        self.assertEqual(canonicalize_url("HTTPS://ATS.TEST:443/apply/?req=7&utm_source=x#top"),
                         "https://ats.test/apply?req=7")
        self.assertNotEqual(canonicalize_url("https://ats.test/apply?req=7"),
                            canonicalize_url("https://ats.test/apply?req=8"))
        self.assertEqual(canonicalize_url("https://ats.test/apply?unknown=kept&utm_term=removed"),
                         "https://ats.test/apply?unknown=kept")
        with self.assertRaises(ValueError):
            canonicalize_url("https://ats.test/apply?token=one")

    def test_schema_excludes_browser_refs_and_secrets(self):
        columns = {row[1] for table in ("runs", "listings", "tasks", "report_entries")
                   for row in self.store.db.execute(f"PRAGMA table_info({table})")}
        for forbidden in ("target_ref", "observation_id", "password", "cookie", "auth_token", "snapshot"):
            self.assertNotIn(forbidden, columns)
        self.assertIn("browser_session_id", columns)


if __name__ == "__main__":
    unittest.main()
