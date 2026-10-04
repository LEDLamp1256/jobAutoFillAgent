"""SQLite storage and guarded transitions for the local batch domain."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .batch_domain import (
    ApplicationReport, ApplicationTask, Blocker, FailureDiagnostic, FailureEvent, FailureReason,
    HumanAction, HumanActionRecord,
    HumanActor, HumanAuthorization, JobListing, JobRun, Ownership,
    Provenance, ReportEntry, ReportKind, ReviewState, RunStatus, TaskStatus, Verification,
)
from .domain import ApplicationObservation, ControlType, is_coarser_page_scope, is_selector_status_label
from .dedupe import DuplicateKind, DuplicateResult, ListingInput, canonicalize_url, identities, similarity_key


SCHEMA_VERSION = 4
_ACTIVE = {TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING, TaskStatus.FILLING, TaskStatus.ADVANCING}


def _require_human(authorization: HumanAuthorization | None, action: HumanAction) -> HumanActor:
    if (not isinstance(authorization, HumanAuthorization) or
            authorization.actor is not HumanActor.LOCAL_OWNER or authorization.action is not action):
        raise PermissionError(f"LOCAL_OWNER {action.value} authorization is required")
    return authorization.actor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return str(uuid4())


class BatchStore:
    """One local connection; methods commit complete state changes atomically."""

    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, SCHEMA_VERSION):
            self.db.close()
            raise RuntimeError(f"unsupported batch schema version {version}")
        if version == 0:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone():
                self.db.close()
                raise RuntimeError("unversioned nonempty database cannot be initialized")
            with self.db:
                self.db.executescript(_SCHEMA)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version in (1, 2, 3):
            with self.db:
                if version == 1:
                    self.db.execute("""ALTER TABLE report_entries ADD COLUMN requiredness TEXT
                        CHECK(requiredness IN ('required', 'optional', 'unknown') OR requiredness IS NULL)""")
                    # Older reports never retained the browser's requiredness evidence.
                    self.db.execute("""UPDATE report_entries SET requiredness = 'unknown'
                        WHERE kind = 'field' AND visible_label != 'Current page'
                          AND COALESCE(semantic_key, '') NOT IN
                              ('direct_source_url', 'page_classification')""")
                if version in (1, 2):
                    self.db.execute("ALTER TABLE report_entries ADD COLUMN question_identity TEXT")
                self.db.execute("""CREATE TABLE failure_events (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    reason TEXT NOT NULL, stage TEXT NOT NULL, category TEXT NOT NULL,
                    detail TEXT NOT NULL, mode TEXT NOT NULL, occurred_at TEXT NOT NULL)""")
                self.db.execute("CREATE INDEX failure_event_task ON failure_events(task_id, occurred_at)")
                self.db.execute("""INSERT INTO failure_events
                    SELECT lower(hex(randomblob(16))), id, COALESCE(last_error, 'unknown'),
                           'historical_unknown', 'not_recorded',
                           'Earlier failure did not retain browser operation detail',
                           'run', updated_at FROM tasks WHERE status = 'failed'""")
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> BatchStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_run(self, sources: tuple[str, ...], job_limit: int) -> JobRun:
        if job_limit < 1 or not sources or any(not source.strip() for source in sources):
            raise ValueError("run needs sources and a positive job limit")
        run = JobRun(_id(), _now(), RunStatus.CREATED, sources, job_limit)
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, 0, 0, NULL)",
                            (run.id, run.created_at, run.status.value, json.dumps(sources), job_limit))
        return run

    def get_run(self, run_id: str) -> JobRun:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return JobRun(row["id"], row["created_at"], RunStatus(row["status"]),
                      tuple(json.loads(row["requested_sources"])), row["requested_job_limit"],
                      row["discovered_count"], row["queued_count"], row["completed_at"])

    def list_runs(self) -> tuple[JobRun, ...]:
        rows = self.db.execute("SELECT id FROM runs ORDER BY created_at, id").fetchall()
        return tuple(self.get_run(row["id"]) for row in rows)

    def set_run_status(self, run_id: str, status: RunStatus) -> JobRun:
        current = self.get_run(run_id).status
        allowed = {
            RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.CANCELLED},
            RunStatus.RUNNING: {RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
            RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED},
        }
        if status not in allowed.get(current, set()):
            raise ValueError("invalid run transition")
        completed = _now() if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED} else None
        with self.db:
            self.db.execute("UPDATE runs SET status = ?, completed_at = ? WHERE id = ?",
                            (status.value, completed, run_id))
        return self.get_run(run_id)

    def check_duplicate(self, item: ListingInput) -> DuplicateResult:
        matches: list[tuple[str, str]] = []
        for kind, key in identities(item):
            row = self.db.execute(
                "SELECT listing_id FROM listing_identities WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            if row:
                matches.append((kind, row["listing_id"]))
        if len({listing_id for _, listing_id in matches}) > 1:
            return DuplicateResult(DuplicateKind.POSSIBLE_DUPLICATE, matches[0][1],
                                   any(self.was_submitted(listing_id) for _, listing_id in matches),
                                   "conflicting strong identities")
        if matches:
            kind, listing_id = matches[0]
            return DuplicateResult(DuplicateKind.EXACT_DUPLICATE, listing_id,
                                   self.was_submitted(listing_id), kind)
        company, title = similarity_key(item)
        rows = self.db.execute("SELECT id FROM listings WHERE company_norm = ? AND title_norm = ?",
                               (company, title)).fetchall()
        if rows:
            return DuplicateResult(DuplicateKind.POSSIBLE_DUPLICATE, rows[0]["id"],
                                   any(self.was_submitted(row["id"]) for row in rows),
                                   "same normalized company and title")
        return DuplicateResult(DuplicateKind.NEW)

    def listings_for_url(self, url: str) -> tuple[JobListing, ...]:
        """Find strong URL identities across posting and application URL fields."""
        canonical = canonicalize_url(url)
        rows = self.db.execute(
            """SELECT DISTINCT listing_id FROM listing_identities
               WHERE kind IN ('listing_url', 'application_url') AND key = ?""",
            (canonical,)).fetchall()
        return tuple(self.get_listing(row["listing_id"]) for row in rows)

    def register_listing(self, item: ListingInput) -> tuple[JobListing, DuplicateResult]:
        duplicate = self.check_duplicate(item)
        if duplicate.kind is DuplicateKind.EXACT_DUPLICATE:
            with self.db:
                self.db.execute("UPDATE listings SET last_seen_at = ? WHERE id = ?", (_now(), duplicate.listing_id))
                for kind, key in identities(item):
                    self.db.execute("INSERT OR IGNORE INTO listing_identities VALUES (?, ?, ?)",
                                    (kind, key, duplicate.listing_id))
            return self.get_listing(duplicate.listing_id or ""), duplicate
        now, listing_id = _now(), _id()
        company, title = similarity_key(item)
        with self.db:
            self.db.execute("""INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           """, (listing_id, item.source, item.source_listing_id, item.company, item.title,
                                 item.location, canonicalize_url(item.listing_url),
                                 canonicalize_url(item.application_url),
                                 canonicalize_url(item.listing_url), canonicalize_url(item.application_url),
                                 now, now, company, title,
                                 duplicate.listing_id if duplicate.kind is DuplicateKind.POSSIBLE_DUPLICATE else None))
            for kind, key in identities(item):
                self.db.execute("INSERT OR IGNORE INTO listing_identities VALUES (?, ?, ?)",
                                (kind, key, listing_id))
        return self.get_listing(listing_id), duplicate

    def set_direct_source_url(self, listing_id: str, url: str) -> JobListing:
        """Keep the owner's exact trimmed URL beside its canonical identity."""
        listing = self.get_listing(listing_id)
        if listing.source != "direct_url":
            raise ValueError("listing is not direct URL intake")
        with self.db:
            self.db.execute("UPDATE listings SET application_url = ? WHERE id = ?", (url, listing_id))
        return self.get_listing(listing_id)

    def enrich_direct_listing(self, listing_id: str, *, title: str | None,
                              company: str | None) -> JobListing:
        """Replace only URL-derived display metadata with observed page metadata."""
        listing = self.get_listing(listing_id)
        if listing.source != "direct_url":
            return listing
        new_title = (title.strip() if title and listing.title in {
            listing.listing_url, listing.application_url, listing.canonical_listing_url,
            listing.canonical_application_url} else listing.title)
        host = (urlsplit(listing.application_url or listing.listing_url or "").hostname or "").casefold()
        # Workday-branded chrome on a Workday-hosted application is not employer evidence.
        def platform_brand(name: str) -> bool:
            return name.casefold() == "workday" and "workday" in host

        observed_company = company.strip() if company else None
        if observed_company and platform_brand(observed_company):
            observed_company = None
        new_company = observed_company or listing.company
        if (not observed_company and (listing.company.casefold() == host or
                                      platform_brand(listing.company))):
            # Neither the intake hostname nor ATS branding establishes an employer.
            new_company = ""
        if new_title == listing.title and new_company == listing.company:
            return listing
        company_norm, title_norm = (("", " ".join(new_title.casefold().split())) if not new_company
                                    else similarity_key(ListingInput(
                                        listing.source, new_company, new_title,
                                        application_url=listing.application_url)))
        with self.db:
            self.db.execute("""UPDATE listings SET title = ?, company = ?, title_norm = ?,
                              company_norm = ? WHERE id = ?""",
                            (new_title, new_company, title_norm, company_norm, listing_id))
        return self.get_listing(listing_id)

    def record_discovery(self, run_id: str) -> JobRun:
        run = self.get_run(run_id)
        if run.status not in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise ValueError("run is not discovering listings")
        with self.db:
            self.db.execute("UPDATE runs SET discovered_count = discovered_count + 1 WHERE id = ?", (run_id,))
        return self.get_run(run_id)

    def get_listing(self, listing_id: str) -> JobListing:
        row = self._one("SELECT * FROM listings WHERE id = ?", (listing_id,))
        return JobListing(*(row[key] for key in (
            "id", "source", "source_listing_id", "company", "title", "location", "listing_url",
            "application_url", "canonical_listing_url", "canonical_application_url",
            "first_seen_at", "last_seen_at", "possible_duplicate_of")))

    def resolve_possible_duplicate_by_human(
            self, listing_id: str, *, authorization: HumanAuthorization) -> JobListing:
        actor = _require_human(authorization, HumanAction.RESOLVE_DUPLICATE)
        listing = self.get_listing(listing_id)
        if not listing.possible_duplicate_of:
            raise PermissionError("possible duplicate requires explicit human resolution")
        with self.db:
            self.db.execute("UPDATE listings SET possible_duplicate_of = NULL WHERE id = ?", (listing_id,))
            self._record_human_action(actor, HumanAction.RESOLVE_DUPLICATE, "listing", listing_id)
        return self.get_listing(listing_id)

    def was_submitted(self, listing_id: str) -> bool:
        return self.db.execute("""SELECT 1 FROM tasks WHERE job_listing_id = ?
                              AND status = ? LIMIT 1""",
                               (listing_id, TaskStatus.SUBMITTED_BY_HUMAN.value)).fetchone() is not None

    def queue_task(self, run_id: str, listing_id: str) -> ApplicationTask:
        run = self.get_run(run_id)
        listing = self.get_listing(listing_id)
        if listing.possible_duplicate_of:
            raise PermissionError("possible duplicate must be reviewed before queueing")
        if run.status not in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise ValueError("run is not accepting tasks")
        if self.was_submitted(listing_id):
            raise PermissionError("this listing was already submitted by a human")
        if self.db.execute("""SELECT 1 FROM tasks AS task
            WHERE task.job_listing_id = ? AND task.status != ?
              AND NOT EXISTS (SELECT 1 FROM human_actions AS action
                  WHERE action.target_kind = 'task' AND action.target_id = task.id
                    AND action.action = ?)
            LIMIT 1""", (listing_id, TaskStatus.SKIPPED.value,
                          HumanAction.ARCHIVE_APPLICATION.value)).fetchone():
            raise ValueError("listing already has an application task")
        now, task_id = _now(), _id()
        with self.db:
            self.db.execute("""INSERT INTO tasks (id, job_listing_id, run_id, status, ownership,
                           created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (task_id, listing_id, run_id, TaskStatus.QUEUED.value,
                             Ownership.AUTOMATION_OWNED.value, now, now))
            self.db.execute("UPDATE runs SET queued_count = queued_count + 1 WHERE id = ?", (run_id,))
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> ApplicationTask:
        row = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return ApplicationTask(row["id"], row["job_listing_id"], row["run_id"],
                               TaskStatus(row["status"]), Ownership(row["ownership"]),
                               row["current_page_or_step"], row["browser_session_id"],
                               row["created_at"], row["updated_at"],
                               Blocker(row["blocker"]) if row["blocker"] else None,
                               row["last_error"], row["resume_requested_at"], row["resume_requested_by"],
                               row["review_checked_at"], row["review_checked_by"],
                               row["submitted_at"], row["submitted_by"])

    def list_tasks(self, run_id: str | None = None) -> tuple[ApplicationTask, ...]:
        """Return durable tasks in queue order. A resume joins the back of the queue."""
        if run_id is not None:
            self.get_run(run_id)
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE (? IS NULL OR run_id = ?) ORDER BY updated_at, id",
            (run_id, run_id)).fetchall()
        return tuple(self.get_task(row["id"]) for row in rows)

    def is_archived(self, task_id: str) -> bool:
        return self.db.execute("""SELECT 1 FROM human_actions
            WHERE target_kind = 'task' AND target_id = ? AND action = ? LIMIT 1""",
            (task_id, HumanAction.ARCHIVE_APPLICATION.value)).fetchone() is not None

    def archive_by_human(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.ARCHIVE_APPLICATION)
        task = self.get_task(task_id)
        if task.status in _ACTIVE:
            raise ValueError("active automation cannot be archived")
        if self.is_archived(task_id):
            raise ValueError("application is already archived")
        with self.db:
            if task.status is TaskStatus.QUEUED:
                self.db.execute("""UPDATE tasks SET status = ?, ownership = ?, updated_at = ?
                    WHERE id = ?""", (TaskStatus.SKIPPED.value, Ownership.HUMAN_OWNED.value,
                                      _now(), task_id))
            self._record_human_action(actor, HumanAction.ARCHIVE_APPLICATION, "task", task_id)
        return self.get_task(task_id)

    def start(self, task_id: str, *, browser_session_id: str | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status is not TaskStatus.QUEUED:
            raise ValueError("only queued tasks can launch")
        if browser_session_id is not None and not browser_session_id.strip():
            raise ValueError("window identity must be nonempty")
        return self._transition(task_id, TaskStatus.LAUNCHING, Ownership.AUTOMATION_OWNED,
                                browser_session_id=browser_session_id or _id())

    def reassign_window(self, task_id: str, browser_session_id: str) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status not in _ACTIVE or task.ownership is not Ownership.AUTOMATION_OWNED:
            raise ValueError("only active automation can reassign a window")
        if not browser_session_id.strip():
            raise ValueError("window identity must be nonempty")
        return self._transition(task_id, task.status, task.ownership,
                                browser_session_id=browser_session_id)

    def clear_window(self, task_id: str, browser_session_id: str) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.browser_session_id != browser_session_id or task.status in _ACTIVE:
            raise ValueError("window association changed or task is active")
        return self._transition(task_id, task.status, task.ownership, browser_session_id=None)

    def advance_state(self, task_id: str, status: TaskStatus, *, page_or_step: str | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        allowed = {
            TaskStatus.LAUNCHING: {TaskStatus.AUTHENTICATING, TaskStatus.FILLING},
            TaskStatus.AUTHENTICATING: {TaskStatus.FILLING},
            TaskStatus.FILLING: {TaskStatus.ADVANCING},
            TaskStatus.ADVANCING: {TaskStatus.FILLING},
        }
        if status not in allowed.get(task.status, set()) or task.ownership is not Ownership.AUTOMATION_OWNED:
            raise ValueError("invalid automation transition")
        return self._transition(task_id, status, Ownership.AUTOMATION_OWNED,
                                current_page_or_step=page_or_step)

    def pause_for_human(self, task_id: str, blocker: Blocker, *, page_or_step: str | None = None,
                        diagnostic: FailureDiagnostic | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status not in _ACTIVE or task.ownership is not Ownership.AUTOMATION_OWNED:
            raise ValueError("only active automation can pause")
        if diagnostic is not None and (blocker is not Blocker.BROWSER_TIMEOUT or
                                       diagnostic.category != "timeout" or diagnostic.mode != "run"):
            raise ValueError("human pause diagnostic must describe a normal-run browser timeout")
        with self.db:
            paused = self._transition(task_id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                                      blocker=blocker.value, current_page_or_step=page_or_step)
            if diagnostic is not None:
                self._insert_failure(task_id, FailureReason.BROWSER_ERROR, diagnostic)
        return paused

    def resume_by_human(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.RESUME)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.HUMAN_PAUSED or task.ownership is not Ownership.HUMAN_OWNED:
            raise ValueError("only human-paused tasks can resume")
        # Retain the last observed blocker until a fresh worker result replaces it.
        # Queuing alone is not evidence that the human completed the field.
        return self._transition(task_id, TaskStatus.QUEUED, Ownership.AUTOMATION_OWNED,
                                browser_session_id=None, resume_requested_at=_now(),
                                resume_requested_by=actor.value, authorization=authorization)

    def clear_observed_blocker(self, task_id: str) -> ApplicationTask:
        """Clear a retained blocker only after successful fresh-page work."""
        task = self.get_task(task_id)
        if task.status not in _ACTIVE or task.ownership is not Ownership.AUTOMATION_OWNED:
            raise ValueError("only active automation can clear an observed blocker")
        return self._transition(task_id, task.status, task.ownership, blocker=None)

    def ready_for_review(self, task_id: str, *, page_or_step: str | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status not in _ACTIVE | {TaskStatus.HUMAN_PAUSED}:
            raise ValueError("task cannot enter final review")
        return self._transition(task_id, TaskStatus.READY_FOR_REVIEW, Ownership.HUMAN_OWNED,
                                blocker=None, current_page_or_step=page_or_step or task.current_page_or_step)

    def mark_review_checked(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.FINAL_REVIEW)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.READY_FOR_REVIEW:
            raise PermissionError("human final review checkoff is required")
        if self.pending_actionable_reviews(task_id):
            raise PermissionError("report still has pending review items")
        with self.db:
            self.db.execute("UPDATE tasks SET review_checked_at = ?, review_checked_by = ?, updated_at = ? WHERE id = ?",
                            (_now(), actor.value, _now(), task_id))
            self._record_human_action(actor, HumanAction.FINAL_REVIEW, "task", task_id)
        return self.get_task(task_id)

    def mark_submitted_by_human(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.RECORD_SUBMISSION)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.READY_FOR_REVIEW or task.ownership is not Ownership.HUMAN_OWNED:
            raise PermissionError("application must be in human final review")
        if not task.review_checked_at or not task.review_checked_by:
            raise PermissionError("human checkoff and submission authorization are required")
        if self.pending_actionable_reviews(task_id):
            raise PermissionError("report has pending review items")
        return self._transition(task_id, TaskStatus.SUBMITTED_BY_HUMAN, Ownership.HUMAN_OWNED,
                                submitted_at=_now(), submitted_by=actor.value, authorization=authorization)

    def fail(self, task_id: str, *, reason: FailureReason,
             diagnostic: FailureDiagnostic | None = None,
             page_or_step: str | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status in {TaskStatus.SUBMITTED_BY_HUMAN, TaskStatus.FAILED, TaskStatus.SKIPPED}:
            raise ValueError("task is terminal")
        detail = diagnostic or FailureDiagnostic("worker", "unspecified", "Worker reported a failure")
        page = page_or_step or task.current_page_or_step
        pending_required = bool(page and any(
            entry.page_or_step == page and entry.kind is ReportKind.FIELD and
            entry.requiredness == "required" and entry.review_state is ReviewState.PENDING
            for entry in self.current_report_entries(task_id)))
        blocker = Blocker.NEEDS_ANSWER if pending_required else task.blocker
        with self.db:
            self.db.execute("""UPDATE tasks SET status = ?, ownership = ?, last_error = ?,
                              current_page_or_step = ?, blocker = ?, updated_at = ?
                              WHERE id = ?""", (TaskStatus.FAILED.value, Ownership.HUMAN_OWNED.value,
                                                reason.value, page, blocker.value if blocker else None,
                                                _now(), task_id))
            self._insert_failure(task_id, reason, detail)
        return self.get_task(task_id)

    def record_recovery_failure(self, task_id: str, diagnostic: FailureDiagnostic) -> FailureEvent:
        if self.get_task(task_id).status is not TaskStatus.FAILED or diagnostic.mode != "recovery":
            raise ValueError("recovery failure requires a failed task")
        with self.db:
            self._insert_failure(task_id, FailureReason.BROWSER_ERROR, diagnostic)
        return self.failure_events(task_id)[-1]

    def _insert_failure(self, task_id: str, reason: FailureReason,
                        diagnostic: FailureDiagnostic) -> None:
        if (diagnostic.mode not in {"run", "recovery"} or
                any(not value or len(value) > limit for value, limit in (
                    (diagnostic.stage, 40), (diagnostic.category, 40), (diagnostic.detail, 120)))):
            raise ValueError("failure diagnostic must be bounded")
        self.db.execute("INSERT INTO failure_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (_id(), task_id, reason.value, diagnostic.stage, diagnostic.category,
                         diagnostic.detail, diagnostic.mode, _now()))

    def failure_events(self, task_id: str) -> tuple[FailureEvent, ...]:
        self.get_task(task_id)
        rows = self.db.execute("""SELECT * FROM failure_events WHERE task_id = ?
                                  ORDER BY occurred_at, rowid""", (task_id,)).fetchall()
        return tuple(FailureEvent(row["id"], row["task_id"], row["reason"], row["stage"],
                                  row["category"], row["detail"], row["mode"], row["occurred_at"])
                     for row in rows)

    def recover_failed(self, task_id: str, *, window_id: str, page: str, blocker: Blocker,
                       authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.RECOVER_APPLICATION)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.FAILED or task.ownership is not Ownership.HUMAN_OWNED:
            raise ValueError("only a failed application can be recovered")
        if not window_id or not page:
            raise ValueError("fresh recovery page and window are required")
        with self.db:
            self.db.execute("""UPDATE tasks SET status = ?, ownership = ?, blocker = ?,
                              current_page_or_step = ?, browser_session_id = ?, last_error = NULL,
                              updated_at = ? WHERE id = ?""",
                            (TaskStatus.HUMAN_PAUSED.value, Ownership.HUMAN_OWNED.value,
                             blocker.value, page, window_id, _now(), task_id))
            self._record_human_action(actor, HumanAction.RECOVER_APPLICATION, "task", task_id)
        return self.get_task(task_id)

    def skip(self, task_id: str) -> ApplicationTask:
        if self.get_task(task_id).status is not TaskStatus.QUEUED:
            raise ValueError("only queued tasks may be skipped")
        return self._transition(task_id, TaskStatus.SKIPPED, Ownership.HUMAN_OWNED)

    def add_report_entry(self, task_id: str, *, page_or_step: str | None, visible_label: str,
                         semantic_key: str | None, kind: ReportKind, provenance: Provenance,
                         action: str, verification: Verification, review_state: ReviewState,
                         reason: str | None = None, narrative_text: str | None = None,
                         requiredness: str | None = None,
                         question_identity: str | None = None,
                         authorization: HumanAuthorization | None = None) -> ReportEntry:
        self.get_task(task_id)
        if self.get_task(task_id).status is TaskStatus.SUBMITTED_BY_HUMAN:
            raise ValueError("submitted report is immutable")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", action):
            raise ValueError("report action must be a nonsecret code")
        if reason is not None and not re.fullmatch(r"[a-z][a-z0-9_]*", reason):
            raise ValueError("report reason must be a nonsecret code")
        human_action: HumanAction | None = None
        if provenance is Provenance.HUMAN_PROVIDED:
            human_action = (HumanAction.REPLACE_NARRATIVE if kind is ReportKind.NARRATIVE else
                            HumanAction.RESOLVE_FIELD if action == "human_resolved" else
                            HumanAction.PROVIDE_ANSWER)
        elif review_state is ReviewState.APPROVED:
            raise PermissionError("approval requires the human review-entry transition")
        if human_action is not None:
            _require_human(authorization, human_action)
        elif authorization is not None:
            raise ValueError("automation report entry cannot carry human authorization")
        entry = ReportEntry(_id(), task_id, page_or_step, visible_label, semantic_key, kind,
                            provenance, action, verification, review_state, reason, _now(),
                            narrative_text, requiredness, question_identity)
        if provenance is Provenance.AI_DRAFT_REVIEW and review_state is not ReviewState.PENDING:
            raise ValueError("new AI drafts must begin pending review")
        with self.db:
            self.db.execute("""INSERT INTO report_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (entry.id, task_id, page_or_step, visible_label, semantic_key, kind.value,
                             provenance.value, action, verification.value, review_state.value, reason,
                             entry.created_at, narrative_text, requiredness, question_identity))
            self.db.execute("UPDATE tasks SET review_checked_at = NULL, review_checked_by = NULL WHERE id = ?",
                            (task_id,))
            if human_action is not None:
                self._record_human_action(HumanActor.LOCAL_OWNER, human_action, "report_entry", entry.id)
        return entry

    def add_report_entry_once(self, task_id: str, *, page_or_step: str | None,
                              visible_label: str, semantic_key: str | None, kind: ReportKind,
                              provenance: Provenance, action: str, verification: Verification,
                              review_state: ReviewState, reason: str | None = None,
                              requiredness: str | None = None,
                              question_identity: str | None = None) -> ReportEntry:
        """Reuse an equivalent audit row, including a still-pending review issue."""
        entries = self.get_report(task_id).entries
        # A matching historical row is not the current state. Reusing it after
        # a later answer (or later unresolved state) would leave the newer row
        # authoritative in the normal report despite this fresh observation.
        current_ids = {entry.id for entry in self.current_report_entries(task_id)}
        if (action == "manual_complete" and kind is ReportKind.FIELD and
                provenance is Provenance.SKIPPED and review_state is ReviewState.NOT_REQUIRED):
            candidates = [entry for entry in entries
                          if (entry.id in current_ids and
                              entry.page_or_step == page_or_step and entry.kind is ReportKind.FIELD and
                              entry.provenance is Provenance.UNRESOLVED and
                              entry.review_state is ReviewState.PENDING and
                              ((question_identity is not None and
                                entry.question_identity == question_identity) or
                               (entry.question_identity is None and
                                entry.visible_label == visible_label and
                                entry.semantic_key == semantic_key)))]
            if len(candidates) == 1:
                entry = candidates[0]
                if requiredness not in {"required", "optional", "unknown"}:
                    raise ValueError("manual field requiredness must be known or explicitly unknown")
                with self.db:
                    self.db.execute("""UPDATE report_entries SET provenance = ?, action = ?,
                        verification = ?, review_state = ?, reason = ?, requiredness = ?,
                        question_identity = COALESCE(question_identity, ?)
                        WHERE id = ?""", (Provenance.SKIPPED.value, "manual_complete",
                                         Verification.NOT_ATTEMPTED.value,
                                         ReviewState.NOT_REQUIRED.value,
                                         ("unsupported_control" if entry.reason == "unsupported_control"
                                          else "human_entered_value"), requiredness,
                                         question_identity, entry.id))
                return self.get_report_entry(entry.id)
        for entry in entries:
            if entry.id not in current_ids:
                continue
            if (question_identity is not None and entry.question_identity is not None and
                    entry.question_identity != question_identity):
                continue
            if (action == "confirmed_trusted" and verification is Verification.VERIFIED and
                    entry.verification is Verification.VERIFIED and
                    entry.page_or_step == page_or_step and entry.visible_label == visible_label and
                    entry.semantic_key == semantic_key and entry.provenance is provenance and
                    entry.review_state is ReviewState.NOT_REQUIRED):
                return self._update_report_metadata(entry, requiredness, question_identity)
            if verification is Verification.VERIFIED and action != "confirmed_trusted":
                continue
            if (entry.page_or_step, entry.visible_label, entry.semantic_key, entry.kind,
                entry.provenance, entry.action, entry.verification, entry.review_state,
                entry.reason) == (page_or_step, visible_label, semantic_key, kind,
                                  provenance, action, verification, review_state, reason):
                return self._update_report_metadata(entry, requiredness, question_identity)
        return self.add_report_entry(task_id, page_or_step=page_or_step,
                                     visible_label=visible_label, semantic_key=semantic_key,
                                     kind=kind, provenance=provenance, action=action,
                                     verification=verification, review_state=review_state,
                                     reason=reason, requiredness=requiredness,
                                     question_identity=question_identity)

    def _update_report_metadata(self, entry: ReportEntry, requiredness: str | None,
                                question_identity: str | None) -> ReportEntry:
        if ((requiredness is None or requiredness == entry.requiredness) and
                (question_identity is None or question_identity == entry.question_identity)):
            return entry
        if requiredness not in {None, "required", "optional", "unknown"}:
            raise ValueError("report requiredness must be a bounded state")
        with self.db:
            self.db.execute("""UPDATE report_entries SET requiredness = COALESCE(?, requiredness),
                            question_identity = COALESCE(?, question_identity) WHERE id = ?""",
                            (requiredness, question_identity, entry.id))
        return self.get_report_entry(entry.id)

    def review_entry_by_human(self, entry_id: str, *, authorization: HumanAuthorization) -> ReportEntry:
        actor = _require_human(authorization, HumanAction.REVIEW_ENTRY)
        row = self._one("SELECT * FROM report_entries WHERE id = ?", (entry_id,))
        task = self.get_task(row["application_task_id"])
        if task.status is not TaskStatus.READY_FOR_REVIEW or row["review_state"] != ReviewState.PENDING.value:
            raise ValueError("entry is not awaiting final review")
        with self.db:
            self.db.execute("UPDATE report_entries SET review_state = ? WHERE id = ?",
                            (ReviewState.APPROVED.value, entry_id))
            self._record_human_action(actor, HumanAction.REVIEW_ENTRY, "report_entry", entry_id)
        return self._entry(self._one("SELECT * FROM report_entries WHERE id = ?", (entry_id,)))

    def resolve_field_by_human(self, entry_id: str, *, authorization: HumanAuthorization) -> ReportEntry:
        """Attest one current blocker without claiming a browser-verified answer."""
        _require_human(authorization, HumanAction.RESOLVE_FIELD)
        entry = self.get_report_entry(entry_id)
        task = self.get_task(entry.application_task_id)
        if (entry.id not in {item.id for item in self.current_report_entries(task.id)} or
                entry.kind is not ReportKind.FIELD or not entry.question_identity or
                entry.requiredness != "required" or entry.review_state is not ReviewState.PENDING or
                entry.semantic_key in {"direct_source_url", "page_classification"} or
                task.ownership is not Ownership.HUMAN_OWNED or
                task.status not in {TaskStatus.HUMAN_PAUSED, TaskStatus.FAILED} or
                (task.status is TaskStatus.FAILED and
                 (task.last_error != FailureReason.BROWSER_ERROR.value or
                  task.blocker not in {Blocker.NEEDS_ANSWER, Blocker.UNSUPPORTED_CONTROL,
                                       Blocker.OTHER}))):
            raise ValueError("field is not a current recoverable blocker")
        page = task.current_page_or_step
        if not page and task.status is TaskStatus.FAILED:
            pages = {item.page_or_step for item in self.current_report_entries(task.id)
                     if item.kind is ReportKind.FIELD and item.requiredness == "required" and
                     item.review_state is ReviewState.PENDING and item.question_identity}
            if len(pages) == 1:
                page = next(iter(pages))
        if not page or entry.page_or_step != page:
            raise ValueError("field is not on the current application page")
        resolved = self.add_report_entry(task.id, page_or_step=page,
            visible_label=entry.visible_label, semantic_key=entry.semantic_key,
            kind=ReportKind.FIELD, provenance=Provenance.HUMAN_PROVIDED,
            action="human_resolved", verification=Verification.NOT_ATTEMPTED,
            review_state=ReviewState.NOT_REQUIRED, reason="human_attested",
            requiredness=entry.requiredness, question_identity=entry.question_identity,
            authorization=authorization)
        if task.status is TaskStatus.FAILED:
            self._transition(task.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                             current_page_or_step=page, last_error=None)
        return resolved

    def undo_field_resolution_by_human(self, entry_id: str, *,
                                       authorization: HumanAuthorization) -> ReportEntry:
        """Reopen one current attestation; history keeps both human actions."""
        actor = _require_human(authorization, HumanAction.UNDO_FIELD_RESOLUTION)
        entry = self.get_report_entry(entry_id)
        task = self.get_task(entry.application_task_id)
        if (entry.id not in {item.id for item in self.current_report_entries(task.id)} or
                entry.kind is not ReportKind.FIELD or entry.action != "human_resolved" or
                entry.provenance is not Provenance.HUMAN_PROVIDED or not entry.question_identity or
                task.ownership is not Ownership.HUMAN_OWNED or
                task.status not in {TaskStatus.HUMAN_PAUSED, TaskStatus.FAILED}):
            raise ValueError("field is not a current manual resolution")
        if not task.current_page_or_step or entry.page_or_step != task.current_page_or_step:
            raise ValueError("manual resolution is not on the current application page")
        reopened = ReportEntry(_id(), task.id, entry.page_or_step, entry.visible_label,
                               entry.semantic_key, ReportKind.FIELD, Provenance.UNRESOLVED,
                               "deferred", Verification.NOT_ATTEMPTED, ReviewState.PENDING,
                               "human_attestation_undone", _now(), None, entry.requiredness,
                               entry.question_identity)
        with self.db:
            # Append, never rewrite: the attestation row and its resolve_field
            # action stay readable; the new row becomes the current state.
            self.db.execute("""INSERT INTO report_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (reopened.id, task.id, reopened.page_or_step, reopened.visible_label,
                             reopened.semantic_key, reopened.kind.value, reopened.provenance.value,
                             reopened.action, reopened.verification.value, reopened.review_state.value,
                             reopened.reason, reopened.created_at, None, reopened.requiredness,
                             reopened.question_identity))
            self.db.execute("UPDATE tasks SET review_checked_at = NULL, review_checked_by = NULL WHERE id = ?",
                            (task.id,))
            self._record_human_action(actor, HumanAction.UNDO_FIELD_RESOLUTION, "report_entry", entry.id)
        return reopened

    def active_manual_resolutions(self, task_id: str, page: str) -> frozenset[str]:
        return frozenset(entry.question_identity for entry in self.current_report_entries(task_id)
            if entry.kind is ReportKind.FIELD and entry.page_or_step == page and
            entry.question_identity and entry.action == "human_resolved" and
            entry.provenance is Provenance.HUMAN_PROVIDED)

    def expire_manual_resolutions(self, task_id: str, page: str) -> None:
        """A fresh forward transition ends attestations for the previous page."""
        ids = [entry.id for entry in self.current_report_entries(task_id)
               if entry.kind is ReportKind.FIELD and entry.page_or_step == page and
               entry.action == "human_resolved"]
        if ids:
            with self.db:
                self.db.executemany("UPDATE report_entries SET action = 'human_resolved_prior_page' WHERE id = ?",
                                    [(entry_id,) for entry_id in ids])

    def human_actions(self, target_kind: str, target_id: str) -> tuple[HumanActionRecord, ...]:
        rows = self.db.execute("""SELECT * FROM human_actions WHERE target_kind = ? AND target_id = ?
                               ORDER BY occurred_at, id""", (target_kind, target_id)).fetchall()
        return tuple(HumanActionRecord(row["id"], HumanActor(row["actor_kind"]),
                                       HumanAction(row["action"]), row["target_kind"],
                                       row["target_id"], row["occurred_at"]) for row in rows)

    def get_report(self, task_id: str) -> ApplicationReport:
        self.get_task(task_id)
        rows = self.db.execute("SELECT * FROM report_entries WHERE application_task_id = ? ORDER BY created_at, id",
                               (task_id,)).fetchall()
        return ApplicationReport(task_id, tuple(self._entry(row) for row in rows))

    def current_report_entries(self, task_id: str) -> tuple[ReportEntry, ...]:
        """Project the latest durable state per logical field; preserve raw history separately."""
        entries = self.get_report(task_id).entries
        field_ids: dict[tuple[str | None, str, str | None], set[str]] = {}
        for entry in entries:
            if entry.kind is ReportKind.FIELD and entry.question_identity:
                key = (entry.page_or_step, entry.visible_label, entry.semantic_key)
                field_ids.setdefault(key, set()).add(entry.question_identity)
        current: dict[tuple[object, ...], ReportEntry] = {}
        for entry in entries:
            if (entry.kind is ReportKind.FIELD and entry.question_identity is None and
                    entry.semantic_key is None and
                    " ".join(entry.visible_label.casefold().split()) == "items selected" and
                    entry.action == "deferred" and entry.reason == "unsupported_control"):
                # Legacy selector status leaked into old reports before the
                # structure-aware parser excluded it. Keep the raw audit row.
                continue
            if (entry.kind is ReportKind.FIELD and entry.provenance is Provenance.UNRESOLVED and
                    entry.action == "deferred" and is_selector_status_label(entry.visible_label)):
                # Open-popup status text (for example "Options Expanded") was
                # once parsed as a question. It is widget state, not a field
                # card; the raw audit row stays in history.
                continue
            if entry.kind is ReportKind.NARRATIVE:
                # Narrative drafts have their own explicit review/history surface.
                current[("narrative", entry.id)] = entry
            elif entry.semantic_key in {"direct_source_url", "page_classification"}:
                current[("technical", entry.semantic_key)] = entry
            elif entry.question_identity:
                current[("question", entry.page_or_step, entry.question_identity)] = entry
            else:
                legacy = (entry.page_or_step, entry.visible_label, entry.semantic_key)
                matches = field_ids.get(legacy, set())
                if len(matches) == 1:
                    current[("question", entry.page_or_step, next(iter(matches)))] = entry
                else:
                    # A legacy row cannot safely be assigned to one of several
                    # repeated fields with the same label.
                    current[("legacy", *legacy)] = entry
        # Rows stored under a pre-checkpoint page key (for example the job
        # title alone) are superseded once the same question is observed under
        # the refined checkpoint key. They stay in raw history but are neither
        # current cards nor effective attestations for the refined page.
        pages: dict[str, set[str | None]] = {}
        for entry in current.values():
            if entry.kind is ReportKind.FIELD and entry.question_identity:
                pages.setdefault(entry.question_identity, set()).add(entry.page_or_step)
        current = {key: entry for key, entry in current.items()
                   if not (entry.kind is ReportKind.FIELD and entry.question_identity and
                           any(is_coarser_page_scope(entry.page_or_step, page)
                               for page in pages.get(entry.question_identity, ())))}
        return tuple(sorted(current.values(), key=lambda entry: (entry.created_at, entry.id)))

    def reconcile_fresh_observation(self, task_id: str, observation: ApplicationObservation) -> None:
        """Project observed satisfaction into current field rows without storing values or refs."""
        page = observation.progress_text or observation.heading or "application page"
        current = {(entry.page_or_step, entry.question_identity): entry
                   for entry in self.current_report_entries(task_id)
                   if entry.kind is ReportKind.FIELD and entry.question_identity}
        for question in observation.questions:
            if question.control_type is ControlType.SECRET:
                continue
            identity = question.report_identity()
            prior = current.get((page, identity))
            satisfied = question.answer_state().satisfied
            if satisfied:
                if (prior and prior.review_state is ReviewState.NOT_REQUIRED and
                        prior.action not in {"optional_skipped", "human_resolved"}):
                    continue
                self.add_report_entry(
                    task_id, page_or_step=page, visible_label=question.label,
                    semantic_key=question.semantic_key, kind=ReportKind.FIELD,
                    provenance=Provenance.SKIPPED, action="manual_complete",
                    verification=Verification.NOT_ATTEMPTED,
                    review_state=ReviewState.NOT_REQUIRED, reason="human_entered_value",
                    requiredness=("required" if question.required is True else
                                  "optional" if question.required is False else "unknown"),
                    question_identity=identity)
            elif (question.required is True and
                  not (question.control_type is ControlType.TOGGLE and
                       question.current_value == "unchecked" and prior is not None and
                       prior.verification is Verification.VERIFIED) and
                  (prior is None or (prior.review_state is not ReviewState.PENDING and
                                     prior.action != "human_resolved"))):
                self.add_report_entry(
                    task_id, page_or_step=page, visible_label=question.label,
                    semantic_key=question.semantic_key, kind=ReportKind.FIELD,
                    provenance=Provenance.UNRESOLVED, action="deferred",
                    verification=Verification.NOT_ATTEMPTED,
                    review_state=ReviewState.PENDING, reason="fresh_observation_unanswered",
                    requiredness="required", question_identity=identity)

    def pending_actionable_reviews(self, task_id: str) -> tuple[ReportEntry, ...]:
        """Current visible application/narrative review, excluding diagnostics."""
        return tuple(entry for entry in self.current_report_entries(task_id)
                     if entry.review_state is ReviewState.PENDING and
                     entry.semantic_key not in {"direct_source_url", "page_classification"})

    def get_report_entry(self, entry_id: str) -> ReportEntry:
        return self._entry(self._one("SELECT * FROM report_entries WHERE id = ?", (entry_id,)))

    @staticmethod
    def _entry(row: sqlite3.Row) -> ReportEntry:
        return ReportEntry(row["id"], row["application_task_id"], row["page_or_step"], row["visible_label"],
                           row["semantic_key"], ReportKind(row["kind"]), Provenance(row["provenance"]),
                           row["action"], Verification(row["verification"]), ReviewState(row["review_state"]),
                           row["reason"], row["created_at"], row["narrative_text"], row["requiredness"],
                           row["question_identity"])

    def _transition(self, task_id: str, status: TaskStatus, ownership: Ownership,
                    *, authorization: HumanAuthorization | None = None, **fields: object) -> ApplicationTask:
        updates = {"status": status.value, "ownership": ownership.value, "updated_at": _now(), **fields}
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.db:
            self.db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*updates.values(), task_id))
            if authorization is not None:
                self._record_human_action(authorization.actor, authorization.action, "task", task_id)
        return self.get_task(task_id)

    def _record_human_action(self, actor: HumanActor, action: HumanAction,
                             target_kind: str, target_id: str) -> None:
        self.db.execute("INSERT INTO human_actions VALUES (?, ?, ?, ?, ?, ?)",
                        (_id(), actor.value, action.value, target_kind, target_id, _now()))

    def _one(self, sql: str, parameters: tuple[object, ...]) -> sqlite3.Row:
        row = self.db.execute(sql, parameters).fetchone()
        if row is None:
            raise KeyError("batch record not found")
        return row


_SCHEMA = """
CREATE TABLE runs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
 requested_sources TEXT NOT NULL, requested_job_limit INTEGER NOT NULL CHECK(requested_job_limit > 0),
 discovered_count INTEGER NOT NULL DEFAULT 0, queued_count INTEGER NOT NULL DEFAULT 0,
 completed_at TEXT
);
CREATE TABLE listings (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, source_listing_id TEXT,
 company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, listing_url TEXT,
 application_url TEXT, canonical_listing_url TEXT, canonical_application_url TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 company_norm TEXT NOT NULL, title_norm TEXT NOT NULL,
 possible_duplicate_of TEXT REFERENCES listings(id)
);
CREATE TABLE listing_identities (
 kind TEXT NOT NULL, key TEXT NOT NULL, listing_id TEXT NOT NULL REFERENCES listings(id),
 PRIMARY KEY(kind, key)
);
CREATE INDEX listing_similarity ON listings(company_norm, title_norm);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, job_listing_id TEXT NOT NULL REFERENCES listings(id),
 run_id TEXT NOT NULL REFERENCES runs(id), status TEXT NOT NULL, ownership TEXT NOT NULL,
 current_page_or_step TEXT, browser_session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 blocker TEXT, last_error TEXT, resume_requested_at TEXT, resume_requested_by TEXT,
 review_checked_at TEXT, review_checked_by TEXT,
 submitted_at TEXT, submitted_by TEXT,
 CHECK (status IN ('queued', 'launching', 'authenticating', 'filling', 'advancing',
                   'human_paused', 'ready_for_review', 'submitted_by_human', 'failed', 'skipped')),
 CHECK (ownership IN ('automation_owned', 'human_owned')),
 CHECK (status NOT IN ('launching', 'authenticating', 'filling', 'advancing')
        OR (ownership = 'automation_owned' AND browser_session_id IS NOT NULL)),
 CHECK (status != 'queued' OR (ownership = 'automation_owned' AND browser_session_id IS NULL)),
 CHECK (status != 'human_paused' OR (ownership = 'human_owned' AND blocker IS NOT NULL)),
 CHECK (status != 'ready_for_review' OR ownership = 'human_owned'),
 CHECK (status != 'submitted_by_human' OR (ownership = 'human_owned' AND review_checked_at IS NOT NULL
        AND review_checked_by IS NOT NULL AND submitted_at IS NOT NULL AND submitted_by IS NOT NULL))
);
CREATE INDEX task_listing_status ON tasks(job_listing_id, status);
CREATE TABLE report_entries (
 id TEXT PRIMARY KEY, application_task_id TEXT NOT NULL REFERENCES tasks(id),
 page_or_step TEXT, visible_label TEXT NOT NULL, semantic_key TEXT,
 kind TEXT NOT NULL, provenance TEXT NOT NULL, action TEXT NOT NULL, verification TEXT NOT NULL,
 review_state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, narrative_text TEXT,
 requiredness TEXT CHECK(requiredness IN ('required', 'optional', 'unknown') OR requiredness IS NULL),
 question_identity TEXT,
 CHECK (kind = 'narrative' OR narrative_text IS NULL),
 CHECK (provenance != 'ai_draft_review' OR (kind = 'narrative' AND review_state != 'not_required'))
);
CREATE INDEX report_task_page ON report_entries(application_task_id, page_or_step);
CREATE TABLE failure_events (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 reason TEXT NOT NULL, stage TEXT NOT NULL, category TEXT NOT NULL,
 detail TEXT NOT NULL, mode TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX failure_event_task ON failure_events(task_id, occurred_at);
CREATE TABLE human_actions (
 id TEXT PRIMARY KEY, actor_kind TEXT NOT NULL CHECK(actor_kind = 'local_owner'),
 action TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL
);
CREATE INDEX human_action_target ON human_actions(target_kind, target_id);
"""
