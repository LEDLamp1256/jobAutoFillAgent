"""Durable batch concepts. Browser observations and action targets belong elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    LAUNCHING = "launching"
    AUTHENTICATING = "authenticating"
    FILLING = "filling"
    ADVANCING = "advancing"
    HUMAN_PAUSED = "human_paused"
    READY_FOR_REVIEW = "ready_for_review"
    SUBMITTED_BY_HUMAN = "submitted_by_human"
    FAILED = "failed"
    SKIPPED = "skipped"


class Ownership(str, Enum):
    AUTOMATION_OWNED = "automation_owned"
    HUMAN_OWNED = "human_owned"


class HumanActor(str, Enum):
    LOCAL_OWNER = "local_owner"


class HumanAction(str, Enum):
    ARCHIVE_APPLICATION = "archive_application"
    RESUME = "resume"
    RESOLVE_DUPLICATE = "resolve_duplicate"
    REVIEW_ENTRY = "review_entry"
    PROVIDE_ANSWER = "provide_answer"
    RESOLVE_FIELD = "resolve_field"
    UNDO_FIELD_RESOLUTION = "undo_field_resolution"
    RECOVER_APPLICATION = "recover_application"
    REPLACE_NARRATIVE = "replace_narrative"
    FINAL_REVIEW = "final_review"
    RECORD_SUBMISSION = "record_submission"


@dataclass(frozen=True)
class HumanAuthorization:
    """Explicit action from the trusted local control plane, not browser content."""

    actor: HumanActor
    action: HumanAction

    def __post_init__(self) -> None:
        if self.actor is not HumanActor.LOCAL_OWNER or not isinstance(self.action, HumanAction):
            raise ValueError("local owner and a defined human action are required")


@dataclass(frozen=True)
class HumanActionRecord:
    id: str
    actor: HumanActor
    action: HumanAction
    target_kind: str
    target_id: str
    occurred_at: str


class Blocker(str, Enum):
    NEEDS_ANSWER = "needs_answer"
    UNSUPPORTED_CONTROL = "unsupported_control"
    BROWSER_TIMEOUT = "browser_timeout"
    MFA_REQUIRED = "mfa_required"
    CAPTCHA_REQUIRED = "captcha_required"
    LOGIN_REQUIRED = "login_required"
    OTHER = "other"


class FailureReason(str, Enum):
    BROWSER_ERROR = "browser_error"
    SITE_ERROR = "site_error"
    UNSUPPORTED_FLOW = "unsupported_flow"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureDiagnostic:
    stage: str
    category: str
    detail: str
    mode: str = "run"


@dataclass(frozen=True)
class FailureEvent:
    id: str
    task_id: str
    reason: str
    stage: str
    category: str
    detail: str
    mode: str
    occurred_at: str


class Provenance(str, Enum):
    VERIFIED_PROFILE = "verified_profile"
    DETERMINISTIC = "deterministic"
    AI_DRAFT_REVIEW = "ai_draft_review"
    HUMAN_PROVIDED = "human_provided"
    UNRESOLVED = "unresolved"
    SKIPPED = "skipped"


class ReportKind(str, Enum):
    FIELD = "field"
    NARRATIVE = "narrative"


class ReviewState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"


class Verification(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobRun:
    id: str
    created_at: str
    status: RunStatus
    requested_sources: tuple[str, ...]
    requested_job_limit: int
    discovered_count: int = 0
    queued_count: int = 0
    completed_at: str | None = None


@dataclass(frozen=True)
class JobListing:
    id: str
    source: str
    source_listing_id: str | None
    company: str
    title: str
    location: str | None
    listing_url: str | None
    application_url: str | None
    canonical_listing_url: str | None
    canonical_application_url: str | None
    first_seen_at: str
    last_seen_at: str
    possible_duplicate_of: str | None = None


@dataclass(frozen=True)
class ApplicationTask:
    id: str
    job_listing_id: str
    run_id: str
    status: TaskStatus
    ownership: Ownership
    current_page_or_step: str | None
    browser_session_id: str | None  # Opaque managed-window identity, never a DOM ref or PID.
    created_at: str
    updated_at: str
    blocker: Blocker | None = None
    last_error: str | None = None
    resume_requested_at: str | None = None
    resume_requested_by: str | None = None
    review_checked_at: str | None = None
    review_checked_by: str | None = None
    submitted_at: str | None = None
    submitted_by: str | None = None


@dataclass(frozen=True)
class ApplicationReport:
    application_task_id: str
    entries: tuple[ReportEntry, ...]


@dataclass(frozen=True)
class ReportEntry:
    id: str
    application_task_id: str
    page_or_step: str | None
    visible_label: str
    semantic_key: str | None
    kind: ReportKind
    provenance: Provenance
    action: str
    verification: Verification
    review_state: ReviewState
    reason: str | None
    created_at: str
    narrative_text: str | None = None
    requiredness: str | None = None  # None is a non-question report entry.
    question_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.visible_label.strip() or not self.action.strip():
            raise ValueError("report label and action are required")
        if self.requiredness not in {None, "required", "optional", "unknown"}:
            raise ValueError("report requiredness must be a bounded state")
        if self.narrative_text is not None and self.kind is not ReportKind.NARRATIVE:
            raise ValueError("only narrative entries may retain text")
        if self.provenance is Provenance.AI_DRAFT_REVIEW:
            if self.kind is not ReportKind.NARRATIVE or self.review_state is ReviewState.NOT_REQUIRED:
                raise ValueError("AI drafts require narrative review")
        if self.kind is ReportKind.NARRATIVE and self.review_state is ReviewState.NOT_REQUIRED:
            raise ValueError("narratives require review")
