"""Offline tests for durable batch state. Every disk database is temporary."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from jobagent.batch_domain import (
    Blocker, FailureDiagnostic, FailureReason, HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance,
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

    def test_archive_queued_task_removes_executable_work_and_keeps_audit(self):
        task = self.task()
        archived = self.store.archive_by_human(
            task.id, authorization=owner(HumanAction.ARCHIVE_APPLICATION))
        self.assertEqual(archived.status, TaskStatus.SKIPPED)
        self.assertTrue(self.store.is_archived(task.id))
        self.assertEqual(self.store.human_actions("task", task.id)[0].action,
                         HumanAction.ARCHIVE_APPLICATION)
        self.store.close()
        self.store = BatchStore(self.path)
        self.assertTrue(self.store.is_archived(task.id))

    def test_archive_paused_task_keeps_state_report_and_history(self):
        task = self.active_task()
        paused = self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        self.store.add_report_entry(task.id, page_or_step="page 1", visible_label="Question",
            semantic_key=None, kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
            action="deferred", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.PENDING, requiredness="required")
        archived = self.store.archive_by_human(
            task.id, authorization=owner(HumanAction.ARCHIVE_APPLICATION))
        self.assertEqual(archived.status, paused.status)
        self.assertEqual(len(self.store.get_report(task.id).entries), 1)
        self.assertEqual(self.store.get_task(task.id).current_page_or_step, "page 1")

    def active_task(self):
        task = self.task()
        return self.store.start(task.id)

    def test_human_field_resolution_persists_and_is_scoped(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        identity = '["","prior work","","","0"]'
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity=identity)
        with self.assertRaises(PermissionError):
            self.store.resolve_field_by_human(pending.id, authorization=owner(HumanAction.RESUME))
        resolved = self.store.resolve_field_by_human(
            pending.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual((resolved.action, resolved.provenance, resolved.verification),
                         ("human_resolved", Provenance.HUMAN_PROVIDED, Verification.NOT_ATTEMPTED))
        self.assertEqual(self.store.human_actions("report_entry", resolved.id)[0].action,
                         HumanAction.RESOLVE_FIELD)
        self.assertEqual(self.store.active_manual_resolutions(task.id, "page 1"), frozenset({identity}))
        self.assertFalse(self.store.active_manual_resolutions(task.id, "page 2"))
        other_listing, _ = self.store.register_listing(ListingInput(
            "board", "Other Company", "Engineer", "https://jobs.test/other", "other"))
        other = self.store.queue_task(self.run.id, other_listing.id)
        self.assertFalse(self.store.active_manual_resolutions(other.id, "page 1"))
        self.store.close()
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.active_manual_resolutions(task.id, "page 1"), frozenset({identity}))
        self.assertEqual(len(self.store.current_report_entries(task.id)), 1)
        with self.assertRaises(ValueError):
            self.store.resolve_field_by_human(pending.id,
                                              authorization=owner(HumanAction.RESOLVE_FIELD))
        self.store.expire_manual_resolutions(task.id, "page 1")
        self.assertFalse(self.store.active_manual_resolutions(task.id, "page 1"))
        self.assertEqual(self.store.get_report_entry(resolved.id).action,
                         "human_resolved_prior_page")

    def pending_field(self, task_id, label, identity, page="page 1"):
        return self.store.add_report_entry(task_id, page_or_step=page,
            visible_label=label, semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity=identity)

    def attestations(self, task_id):
        resolved = [entry for entry in self.store.get_report(task_id).entries
                    if entry.action.startswith("human_resolved")]
        actions = [action for entry in self.store.get_report(task_id).entries
                   for action in self.store.human_actions("report_entry", entry.id)
                   if action.action is HumanAction.RESOLVE_FIELD]
        return resolved, actions

    def test_mark_resolved_attests_exactly_one_field_on_one_page(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        city = '["","city","application","address","0"]'
        first = '["","first name","application","legal name","0"]'
        # Same label in another record context, and a near-identical label.
        first_other = '["","first name","application","preferred name","0"]'
        first_similar = '["","first name (optional)","application","legal name","0"]'
        target = self.pending_field(task.id, "City", city)
        others = [self.pending_field(task.id, "First Name", first),
                  self.pending_field(task.id, "First Name", first_other),
                  self.pending_field(task.id, "First Name (optional)", first_similar)]
        # The same identity on another page is a different checkpoint.
        later_page = self.pending_field(task.id, "City", city, page="page 2")
        self.store.resolve_field_by_human(target.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual(self.store.active_manual_resolutions(task.id, "page 1"), frozenset({city}))
        self.assertFalse(self.store.active_manual_resolutions(task.id, "page 2"))
        current = {entry.id: entry for entry in self.store.current_report_entries(task.id)}
        for entry in [*others, later_page]:
            self.assertIn(entry.id, current)
            self.assertEqual(current[entry.id].review_state, ReviewState.PENDING)
            self.assertEqual(current[entry.id].provenance, Provenance.UNRESOLVED)
        resolved, actions = self.attestations(task.id)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(actions), 1)
        self.assertNotEqual(self.store.get_task(task.id).status, TaskStatus.SUBMITTED_BY_HUMAN)

    def test_recovery_and_resume_alone_create_no_attestation(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        for index, label in enumerate(("City", "State", "Phone Number")):
            self.pending_field(task.id, label, f'["","{label.casefold()}","","","{index}"]')
        self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR,
                        diagnostic=FailureDiagnostic("locate_control", "timeout",
                                                     "Managed browser operation timed out"),
                        page_or_step="page 1")
        self.store.recover_failed(task.id, window_id="fresh-window", page="page 1",
                                  blocker=Blocker.NEEDS_ANSWER,
                                  authorization=owner(HumanAction.RECOVER_APPLICATION))
        self.assertEqual(self.attestations(task.id), ([], []))
        self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.assertEqual(self.attestations(task.id), ([], []))
        self.assertFalse(self.store.active_manual_resolutions(task.id, "page 1"))
        self.assertTrue(all(entry.review_state is ReviewState.PENDING
                            for entry in self.store.current_report_entries(task.id)))
        # Failure provenance from the timeout is preserved alongside.
        self.assertEqual([event.stage for event in self.store.failure_events(task.id)],
                         ["locate_control"])

    def test_selector_status_rows_are_history_not_field_cards(self):
        task = self.active_task()
        phantom = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Options Expanded", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="no_safe_answer", requiredness="unknown",
            question_identity='["","options expanded","application","follow us","0"]')
        real = self.pending_field(task.id, "How Did You Hear About Us?",
                                  '["","how did you hear about us?","application","follow us","0"]')
        self.assertEqual([entry.id for entry in self.store.current_report_entries(task.id)], [real.id])
        self.assertIn(phantom.id, [entry.id for entry in self.store.get_report(task.id).entries])

    def test_undo_reopens_exactly_one_attestation_and_keeps_audit(self):
        task = self.active_task()
        page = "Job Title › My Information"
        self.store.pause_for_human(task.id, Blocker.OTHER, page_or_step=page)
        city = '["","city","job title","address","0"]'
        city_other = '["","city","job title","mailing address","0"]'
        first = '["","first name","job title","legal name","0"]'
        targets = {key: self.pending_field(task.id, label, key, page=page)
                   for label, key in (("City", city), ("City", city_other), ("First Name", first))}
        resolved = {key: self.store.resolve_field_by_human(
            entry.id, authorization=owner(HumanAction.RESOLVE_FIELD)) for key, entry in targets.items()}
        elsewhere = self.pending_field(task.id, "City", city, page="Job Title › Experience")
        before = len(self.store.get_report(task.id).entries)
        with self.assertRaises(PermissionError):
            self.store.undo_field_resolution_by_human(
                resolved[city].id, authorization=owner(HumanAction.RESOLVE_FIELD))
        reopened = self.store.undo_field_resolution_by_human(
            resolved[city].id, authorization=owner(HumanAction.UNDO_FIELD_RESOLUTION))
        self.assertEqual((reopened.provenance, reopened.action, reopened.review_state,
                          reopened.reason, reopened.requiredness, reopened.page_or_step,
                          reopened.question_identity),
                         (Provenance.UNRESOLVED, "deferred", ReviewState.PENDING,
                          "human_attestation_undone", "required", page, city))
        # Exactly one attestation reopened; the similar-label and other
        # fields on the page, and the same identity elsewhere, are unchanged.
        self.assertEqual(self.store.active_manual_resolutions(task.id, page),
                         frozenset({city_other, first}))
        current = {entry.id for entry in self.store.current_report_entries(task.id)}
        self.assertIn(reopened.id, current)
        self.assertNotIn(resolved[city].id, current)
        self.assertTrue({resolved[city_other].id, resolved[first].id, elsewhere.id} <= current)
        self.assertEqual(self.store.get_report_entry(elsewhere.id).review_state, ReviewState.PENDING)
        # Append-only: the attestation row and both human actions remain readable.
        self.assertEqual(len(self.store.get_report(task.id).entries), before + 1)
        self.assertEqual(self.store.get_report_entry(resolved[city].id).action, "human_resolved")
        self.assertEqual([action.action for action in
                          self.store.human_actions("report_entry", resolved[city].id)],
                         [HumanAction.RESOLVE_FIELD, HumanAction.UNDO_FIELD_RESOLUTION])
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.HUMAN_PAUSED)
        with self.assertRaises(ValueError):
            self.store.undo_field_resolution_by_human(
                resolved[city].id, authorization=owner(HumanAction.UNDO_FIELD_RESOLUTION))
        with self.assertRaises(ValueError):
            self.store.undo_field_resolution_by_human(
                elsewhere.id, authorization=owner(HumanAction.UNDO_FIELD_RESOLUTION))
        # The reopened field can be attested again, explicitly.
        again = self.store.resolve_field_by_human(
            reopened.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertIn(city, self.store.active_manual_resolutions(task.id, page))
        self.assertEqual(again.action, "human_resolved")

    def test_undo_is_refused_off_the_current_page(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.OTHER, page_or_step="Title › Step A")
        key = '["","city","","","0"]'
        resolved = self.store.resolve_field_by_human(
            self.pending_field(task.id, "City", key, page="Title › Step A").id,
            authorization=owner(HumanAction.RESOLVE_FIELD))
        self.store._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                               current_page_or_step="Title › Step B")
        with self.assertRaises(ValueError):
            self.store.undo_field_resolution_by_human(
                resolved.id, authorization=owner(HumanAction.UNDO_FIELD_RESOLUTION))

    def test_legacy_coarse_scope_attestation_never_satisfies_refined_checkpoint(self):
        task = self.active_task()
        legacy, refined = "Software Engineer", "Software Engineer › My Information"
        self.store.pause_for_human(task.id, Blocker.OTHER, page_or_step=legacy)
        key = '["","city","software engineer","address","0"]'
        unrelated = '["","middle name","software engineer","legal name","0"]'
        old = self.store.resolve_field_by_human(
            self.pending_field(task.id, "City", key, page=legacy).id,
            authorization=owner(HumanAction.RESOLVE_FIELD))
        untouched = self.store.add_report_entry(task.id, page_or_step=legacy,
            visible_label="Middle Name", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.SKIPPED, action="optional_skipped",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.NOT_REQUIRED,
            requiredness="optional", question_identity=unrelated)
        # Before any refined observation the old row is still current and undoable.
        self.assertIn(old.id, {entry.id for entry in self.store.current_report_entries(task.id)})
        fresh = self.pending_field(task.id, "City", key, page=refined)
        current = {entry.id for entry in self.store.current_report_entries(task.id)}
        self.assertIn(fresh.id, current)
        self.assertNotIn(old.id, current)
        self.assertIn(untouched.id, current)
        self.assertFalse(self.store.active_manual_resolutions(task.id, refined))
        self.assertIn(old.id, {entry.id for entry in self.store.get_report(task.id).entries})
        self.assertEqual(self.store.human_actions("report_entry", old.id)[0].action,
                         HumanAction.RESOLVE_FIELD)

    def test_failed_browser_task_reopens_only_by_explicit_field_attestation(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity='["","prior work","","","0"]')
        self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR)
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.FAILED)
        self.store.resolve_field_by_human(pending.id,
                                          authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.store.get_task(task.id).current_page_or_step, "page 1")
        self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.QUEUED)

    def test_genuine_site_failure_is_not_reclassified_by_field_resolution(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity='["","prior work","","","0"]')
        self.store.fail(task.id, reason=FailureReason.SITE_ERROR)
        with self.assertRaises(ValueError):
            self.store.resolve_field_by_human(pending.id,
                                              authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.FAILED)

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

    def test_v1_report_migration_preserves_history_without_inventing_requiredness(self):
        task = self.task()
        field = self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="First Name",
            semantic_key="personal.first_name", kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING)
        metadata = self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="Current page",
            semantic_key="page_classification", kind=ReportKind.FIELD,
            provenance=Provenance.DETERMINISTIC, action="classified",
            verification=Verification.UNKNOWN, review_state=ReviewState.NOT_REQUIRED)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE failure_events")
            db.execute("ALTER TABLE report_entries DROP COLUMN question_identity")
            db.execute("ALTER TABLE report_entries DROP COLUMN requiredness")
            db.execute("PRAGMA user_version = 1")
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.store.get_report_entry(field.id).requiredness, "unknown")
        self.assertIsNone(self.store.get_report_entry(metadata.id).requiredness)
        self.assertEqual(self.store.get_listing(self.listing.id).title, "Engineer")
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v2_report_migration_preserves_pending_rows_for_scoped_reconciliation(self):
        task = self.task()
        pending = self.store.add_report_entry(
            task.id, page_or_step="page 1", visible_label="Custom choice",
            semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="unsupported_control", requiredness="required")
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE failure_events")
            db.execute("ALTER TABLE report_entries DROP COLUMN question_identity")
            db.execute("PRAGMA user_version = 2")
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertIsNone(self.store.get_report_entry(pending.id).question_identity)
        self.assertEqual(self.store.get_report_entry(pending.id).review_state, ReviewState.PENDING)

    def test_v3_migration_adds_failure_history_without_changing_task_or_report(self):
        task = self.task()
        self.store.start(task.id)
        self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR)
        entry = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE failure_events")
            db.execute("PRAGMA user_version = 3")
        self.store = BatchStore(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.FAILED)
        self.assertEqual(self.store.get_report_entry(entry.id).visible_label, "Prior work")
        old_event = self.store.failure_events(task.id)
        self.assertEqual(len(old_event), 1)
        self.assertEqual((old_event[0].stage, old_event[0].category),
                         ("historical_unknown", "not_recorded"))

    def test_failure_event_persists_and_recovery_does_not_erase_it(self):
        task = self.active_task()
        self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR,
                        diagnostic=FailureDiagnostic("observe_snapshot", "timeout",
                                                     "Managed browser operation timed out"))
        event = self.store.failure_events(task.id)[0]
        self.assertEqual((event.stage, event.category, event.mode),
                         ("observe_snapshot", "timeout", "run"))
        self.assertIsNotNone(datetime.fromisoformat(event.occurred_at).tzinfo)
        self.store.clear_window(task.id, self.store.get_task(task.id).browser_session_id)
        self.store.recover_failed(task.id, window_id="new-window", page="page 1",
                                  blocker=Blocker.NEEDS_ANSWER,
                                  authorization=owner(HumanAction.RECOVER_APPLICATION))
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.store.failure_events(task.id), (event,))
        with BatchStore(self.path) as reopened:
            self.assertEqual(reopened.failure_events(task.id), (event,))

    def test_failure_keeps_existing_human_resolution_and_field_history(self):
        task = self.active_task()
        self.store.pause_for_human(task.id, Blocker.NEEDS_ANSWER, page_or_step="page 1")
        pending = self.store.add_report_entry(task.id, page_or_step="page 1",
            visible_label="Prior work", semantic_key=None, kind=ReportKind.FIELD,
            provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            requiredness="required", question_identity='["","prior work","","","0"]')
        resolved = self.store.resolve_field_by_human(
            pending.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.store.resume_by_human(task.id, authorization=owner(HumanAction.RESUME))
        self.store.start(task.id)
        self.store.fail(task.id, reason=FailureReason.BROWSER_ERROR,
                        diagnostic=FailureDiagnostic("verify", "timeout", "Managed browser operation timed out"),
                        page_or_step="page 1")
        self.assertEqual(self.store.current_report_entries(task.id)[0].id, resolved.id)
        self.assertEqual(self.store.current_report_entries(task.id)[0].action, "human_resolved")
        self.assertEqual(len(self.store.get_report(task.id).entries), 2)
        self.assertEqual(len(self.store.failure_events(task.id)), 1)

    def test_repeated_questions_reconcile_only_their_scoped_identity(self):
        task = self.task()
        keys = ('["","job title","experience","record one","0"]',
                '["","job title","experience","record two","0"]')
        pending = [self.store.add_report_entry_once(
            task.id, page_or_step="Experience", visible_label="Job Title", semantic_key=None,
            kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED, action="deferred",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.PENDING,
            reason="unsupported_control", requiredness="required", question_identity=key)
                   for key in keys]
        self.assertNotEqual(pending[0].id, pending[1].id)
        completed = self.store.add_report_entry_once(
            task.id, page_or_step="Experience", visible_label="Job Title", semantic_key=None,
            kind=ReportKind.FIELD, provenance=Provenance.SKIPPED, action="manual_complete",
            verification=Verification.NOT_ATTEMPTED, review_state=ReviewState.NOT_REQUIRED,
            reason="human_entered_value", requiredness="required", question_identity=keys[1])
        self.assertEqual(completed.id, pending[1].id)
        self.assertEqual(self.store.get_report_entry(pending[0].id).review_state, ReviewState.PENDING)
        self.assertEqual(self.store.get_report_entry(pending[1].id).review_state, ReviewState.NOT_REQUIRED)

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
        columns = {row[1] for table in ("runs", "listings", "tasks", "report_entries", "failure_events")
                   for row in self.store.db.execute(f"PRAGMA table_info({table})")}
        for forbidden in ("target_ref", "observation_id", "password", "cookie", "auth_token", "snapshot"):
            self.assertNotIn(forbidden, columns)
        self.assertIn("browser_session_id", columns)


if __name__ == "__main__":
    unittest.main()
