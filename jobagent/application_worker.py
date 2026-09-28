"""Bounded application work contract. Implementations own all page interaction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .batch_domain import ApplicationTask, Blocker, FailureReason


class WorkerYield(str, Enum):
    PROGRESS = "progress"
    HUMAN_BLOCKED = "human_blocked"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewIssue:
    """A nonsecret issue collected during a page or application pass."""

    visible_label: str
    reason: str
    page_or_step: str | None = None


@dataclass(frozen=True)
class WorkerOutcome:
    kind: WorkerYield
    issues: tuple[ReviewIssue, ...] = ()
    blocker: Blocker | None = None
    failure: FailureReason | None = None
    page_or_step: str | None = None

    def __post_init__(self) -> None:
        if self.kind is WorkerYield.HUMAN_BLOCKED and self.blocker is None:
            raise ValueError("human blocker outcome requires a blocker")
        if self.kind is WorkerYield.FAILED and self.failure is None:
            raise ValueError("failure outcome requires a reason")
        if self.kind is not WorkerYield.HUMAN_BLOCKED and self.blocker is not None:
            raise ValueError("blocker only belongs to human-blocked outcome")
        if self.kind is not WorkerYield.FAILED and self.failure is not None:
            raise ValueError("failure reason only belongs to failed outcome")
        if self.kind not in {WorkerYield.HUMAN_BLOCKED, WorkerYield.READY_FOR_REVIEW} and self.issues:
            raise ValueError("issues require a human attention outcome")


@dataclass(frozen=True)
class WorkRequest:
    task: ApplicationTask
    window_id: str
    fresh_observation_required: bool = True
    resumed_by_human: bool = False


class ApplicationWorkerPort(Protocol):
    def work_until_yield(self, request: WorkRequest) -> WorkerOutcome: ...
