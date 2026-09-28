"""SQLite storage and guarded transitions for the local batch domain."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .batch_domain import (
    ApplicationReport, ApplicationTask, Blocker, FailureReason, HumanAction, HumanActionRecord,
    HumanActor, HumanAuthorization, JobListing, JobRun, Ownership,
    Provenance, ReportEntry, ReportKind, ReviewState, RunStatus, TaskStatus, Verification,
)
from .dedupe import DuplicateKind, DuplicateResult, ListingInput, canonicalize_url, identities, similarity_key


SCHEMA_VERSION = 1
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
        if version not in (0, SCHEMA_VERSION):
            self.db.close()
            raise RuntimeError(f"unsupported batch schema version {version}")
        if version == 0:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone():
                self.db.close()
                raise RuntimeError("unversioned nonempty database cannot be initialized")
            with self.db:
                self.db.executescript(_SCHEMA)
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
        if self.db.execute("SELECT 1 FROM tasks WHERE job_listing_id = ? AND status != ? LIMIT 1",
                           (listing_id, TaskStatus.SKIPPED.value)).fetchone():
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

    def pause_for_human(self, task_id: str, blocker: Blocker, *, page_or_step: str | None = None) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status not in _ACTIVE or task.ownership is not Ownership.AUTOMATION_OWNED:
            raise ValueError("only active automation can pause")
        return self._transition(task_id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED,
                                blocker=blocker.value, current_page_or_step=page_or_step)

    def resume_by_human(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.RESUME)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.HUMAN_PAUSED or task.ownership is not Ownership.HUMAN_OWNED:
            raise ValueError("only human-paused tasks can resume")
        # Queued for a later fresh observation; no old browser target is replayed.
        return self._transition(task_id, TaskStatus.QUEUED, Ownership.AUTOMATION_OWNED,
                                blocker=None, browser_session_id=None, resume_requested_at=_now(),
                                resume_requested_by=actor.value, authorization=authorization)

    def ready_for_review(self, task_id: str) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status not in _ACTIVE | {TaskStatus.HUMAN_PAUSED}:
            raise ValueError("task cannot enter final review")
        return self._transition(task_id, TaskStatus.READY_FOR_REVIEW, Ownership.HUMAN_OWNED,
                                blocker=None)

    def mark_review_checked(self, task_id: str, *, authorization: HumanAuthorization) -> ApplicationTask:
        actor = _require_human(authorization, HumanAction.FINAL_REVIEW)
        task = self.get_task(task_id)
        if task.status is not TaskStatus.READY_FOR_REVIEW:
            raise PermissionError("human final review checkoff is required")
        if self.db.execute("""SELECT 1 FROM report_entries WHERE application_task_id = ?
                           AND review_state = ? LIMIT 1""", (task_id, ReviewState.PENDING.value)).fetchone():
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
        if self.db.execute("""SELECT 1 FROM report_entries WHERE application_task_id = ?
                           AND review_state = ? LIMIT 1""", (task_id, ReviewState.PENDING.value)).fetchone():
            raise PermissionError("report has pending review items")
        return self._transition(task_id, TaskStatus.SUBMITTED_BY_HUMAN, Ownership.HUMAN_OWNED,
                                submitted_at=_now(), submitted_by=actor.value, authorization=authorization)

    def fail(self, task_id: str, *, reason: FailureReason) -> ApplicationTask:
        task = self.get_task(task_id)
        if task.status in {TaskStatus.SUBMITTED_BY_HUMAN, TaskStatus.FAILED, TaskStatus.SKIPPED}:
            raise ValueError("task is terminal")
        return self._transition(task_id, TaskStatus.FAILED, Ownership.HUMAN_OWNED,
                                last_error=reason.value)

    def skip(self, task_id: str) -> ApplicationTask:
        if self.get_task(task_id).status is not TaskStatus.QUEUED:
            raise ValueError("only queued tasks may be skipped")
        return self._transition(task_id, TaskStatus.SKIPPED, Ownership.HUMAN_OWNED)

    def add_report_entry(self, task_id: str, *, page_or_step: str | None, visible_label: str,
                         semantic_key: str | None, kind: ReportKind, provenance: Provenance,
                         action: str, verification: Verification, review_state: ReviewState,
                         reason: str | None = None, narrative_text: str | None = None,
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
            human_action = (HumanAction.REPLACE_NARRATIVE if kind is ReportKind.NARRATIVE
                            else HumanAction.PROVIDE_ANSWER)
        elif review_state is ReviewState.APPROVED:
            raise PermissionError("approval requires the human review-entry transition")
        if human_action is not None:
            _require_human(authorization, human_action)
        elif authorization is not None:
            raise ValueError("automation report entry cannot carry human authorization")
        entry = ReportEntry(_id(), task_id, page_or_step, visible_label, semantic_key, kind,
                            provenance, action, verification, review_state, reason, _now(), narrative_text)
        if provenance is Provenance.AI_DRAFT_REVIEW and review_state is not ReviewState.PENDING:
            raise ValueError("new AI drafts must begin pending review")
        with self.db:
            self.db.execute("""INSERT INTO report_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (entry.id, task_id, page_or_step, visible_label, semantic_key, kind.value,
                             provenance.value, action, verification.value, review_state.value, reason,
                             entry.created_at, narrative_text))
            self.db.execute("UPDATE tasks SET review_checked_at = NULL, review_checked_by = NULL WHERE id = ?",
                            (task_id,))
            if human_action is not None:
                self._record_human_action(HumanActor.LOCAL_OWNER, human_action, "report_entry", entry.id)
        return entry

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

    def get_report_entry(self, entry_id: str) -> ReportEntry:
        return self._entry(self._one("SELECT * FROM report_entries WHERE id = ?", (entry_id,)))

    @staticmethod
    def _entry(row: sqlite3.Row) -> ReportEntry:
        return ReportEntry(row["id"], row["application_task_id"], row["page_or_step"], row["visible_label"],
                           row["semantic_key"], ReportKind(row["kind"]), Provenance(row["provenance"]),
                           row["action"], Verification(row["verification"]), ReviewState(row["review_state"]),
                           row["reason"], row["created_at"], row["narrative_text"])

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
 CHECK (kind = 'narrative' OR narrative_text IS NULL),
 CHECK (provenance != 'ai_draft_review' OR (kind = 'narrative' AND review_state != 'not_required'))
);
CREATE INDEX report_task_page ON report_entries(application_task_id, page_or_step);
CREATE TABLE human_actions (
 id TEXT PRIMARY KEY, actor_kind TEXT NOT NULL CHECK(actor_kind = 'local_owner'),
 action TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL
);
CREATE INDEX human_action_target ON human_actions(target_kind, target_id);
"""
