"""Deterministic, single-worker orchestration over persisted application tasks."""

from __future__ import annotations

from dataclasses import dataclass

from .application_worker import ApplicationWorkerPort, WorkRequest, WorkerOutcome, WorkerYield
from .batch_domain import (
    ApplicationReport, ApplicationTask, HumanAuthorization, Ownership, Provenance,
    ReportKind, ReviewState, TaskStatus, Verification,
)
from .persistence import BatchStore
from .windowing import ApplicationWindowPort


_ACTIVE = {TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING, TaskStatus.FILLING, TaskStatus.ADVANCING}
_ATTENTION = {TaskStatus.HUMAN_PAUSED, TaskStatus.READY_FOR_REVIEW}


@dataclass(frozen=True)
class AttentionItem:
    task: ApplicationTask
    report: ApplicationReport


@dataclass(frozen=True)
class SchedulerStep:
    task: ApplicationTask
    outcome: WorkerOutcome


class ApplicationScheduler:
    """One synchronous worker invocation at a time; windows may outlive a yield."""

    def __init__(self, store: BatchStore, worker: ApplicationWorkerPort,
                 windows: ApplicationWindowPort, *, max_open_applications: int):
        if max_open_applications < 1:
            raise ValueError("max_open_applications must be positive")
        self.store = store
        self.worker = worker
        self.windows = windows
        self.max_open_applications = max_open_applications
        self._working = False

    def tasks(self, run_id: str | None = None) -> tuple[ApplicationTask, ...]:
        return self.store.list_tasks(run_id)

    def attention(self, run_id: str | None = None) -> tuple[AttentionItem, ...]:
        return tuple(AttentionItem(task, self.store.get_report(task.id))
                     for task in self.tasks(run_id) if task.status in _ATTENTION)

    def resume_application(self, task_id: str, authorization: HumanAuthorization) -> ApplicationTask:
        # BatchStore validates the exact LOCAL_OWNER RESUME authorization and audits it.
        return self.store.resume_by_human(task_id, authorization=authorization)

    def bring_window_to_front(self, task_id: str) -> None:
        self.store.get_task(task_id)
        window_id = self.windows.window_for_task(task_id)
        if window_id is None or not self.windows.exists(window_id):
            raise LookupError("task has no live managed window")
        self.windows.bring_to_front(window_id)

    def release_window(self, task_id: str) -> ApplicationTask:
        task = self.store.get_task(task_id)
        if task.status in _ACTIVE:
            raise ValueError("cannot release an active task window")
        window_id = self.windows.window_for_task(task_id)
        if window_id is not None and self.windows.exists(window_id):
            self.windows.release(window_id)
        if task.browser_session_id is not None:
            return self.store.clear_window(task_id, task.browser_session_id)
        return task

    def step(self, run_id: str | None = None) -> SchedulerStep | None:
        """Perform at most one bounded worker call. None means no task can run now."""
        if self._working:
            raise RuntimeError("an application worker is already active")
        if run_id is not None:
            self.store.get_run(run_id)
        all_tasks = self.tasks()
        active = [task for task in all_tasks if task.status in _ACTIVE]
        if len(active) > 1:
            raise RuntimeError("multiple persisted active tasks require recovery")
        if run_id is not None and active and active[0].run_id != run_id:
            return None
        tasks = all_tasks if run_id is None else tuple(task for task in all_tasks if task.run_id == run_id)
        candidates = active or [task for task in tasks if task.status is TaskStatus.QUEUED]
        for task in candidates:
            if task.ownership is not Ownership.AUTOMATION_OWNED:
                continue
            window_id = self.windows.window_for_task(task.id)
            if window_id is not None and not self.windows.exists(window_id):
                window_id = None
            if window_id is None:
                if self.windows.open_count() >= self.max_open_applications:
                    if active:
                        return None
                    continue
                window_id = self.windows.allocate(task.id)
            return self._run(task, window_id)
        return None

    def _run(self, task: ApplicationTask, window_id: str) -> SchedulerStep:
        if task.status is TaskStatus.QUEUED:
            task = self.store.start(task.id, browser_session_id=window_id)
        elif task.browser_session_id != window_id:
            task = self.store.reassign_window(task.id, window_id)
        request = WorkRequest(task, window_id, resumed_by_human=task.resume_requested_at is not None)
        self._working = True
        try:
            outcome = self.worker.work_until_yield(request)
            if not isinstance(outcome, WorkerOutcome):
                raise TypeError("worker must return WorkerOutcome")
            for issue in outcome.issues:
                self.store.add_report_entry(task.id, page_or_step=issue.page_or_step,
                                            visible_label=issue.visible_label, semantic_key=None,
                                            kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
                                            action="deferred", verification=Verification.NOT_ATTEMPTED,
                                            review_state=ReviewState.PENDING, reason=issue.reason)
            if outcome.kind is WorkerYield.HUMAN_BLOCKED:
                task = self.store.pause_for_human(task.id, outcome.blocker, page_or_step=outcome.page_or_step)
            elif outcome.kind is WorkerYield.READY_FOR_REVIEW:
                task = self.store.ready_for_review(task.id)
            elif outcome.kind is WorkerYield.FAILED:
                task = self.store.fail(task.id, reason=outcome.failure)
                task = self.release_window(task.id)
            return SchedulerStep(task, outcome)
        finally:
            self._working = False
