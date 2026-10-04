"""Deterministic, single-worker orchestration over persisted application tasks."""

from __future__ import annotations

from dataclasses import dataclass

from .application_worker import ApplicationWorkerPort, WorkRequest, WorkerOutcome, WorkerYield
from .batch_domain import (
    ApplicationReport, ApplicationTask, Blocker, HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance,
    ReportKind, ReviewState, TaskStatus, Verification,
)
from .persistence import BatchStore
from .failure_diagnostics import safe_failure
from .windowing import ApplicationWindowPort


_ACTIVE = {TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING, TaskStatus.FILLING, TaskStatus.ADVANCING}
_ATTENTION = {TaskStatus.HUMAN_PAUSED, TaskStatus.READY_FOR_REVIEW, TaskStatus.FAILED}
_REFRESH_PENDING = _ACTIVE | {TaskStatus.QUEUED, TaskStatus.FAILED}


def retains_prior_attention(task: ApplicationTask) -> bool:
    return (task.resume_requested_at is not None and task.blocker is not None and
            task.status in _REFRESH_PENDING)


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
        return tuple(task for task in self.store.list_tasks(run_id)
                     if not self.store.is_archived(task.id))

    def attention(self, run_id: str | None = None) -> tuple[AttentionItem, ...]:
        return tuple(AttentionItem(task, self.store.get_report(task.id))
                     for task in self.tasks(run_id)
                     if task.status in _ATTENTION or retains_prior_attention(task))

    def resume_application(self, task_id: str, authorization: HumanAuthorization) -> ApplicationTask:
        # BatchStore validates the exact LOCAL_OWNER RESUME authorization and audits it.
        if self.store.is_archived(task_id):
            raise ValueError("archived application cannot resume")
        return self.store.resume_by_human(task_id, authorization=authorization)

    def recover_application(self, task_id: str, authorization: HumanAuthorization) -> ApplicationTask:
        """Explicitly restore an observed browser, stopping before any form action."""
        task = self.store.get_task(task_id)
        if self.store.is_archived(task_id):
            raise ValueError("archived application cannot recover")
        if (authorization.actor is not HumanActor.LOCAL_OWNER or
                authorization.action is not HumanAction.RECOVER_APPLICATION):
            raise PermissionError("local owner recovery authorization is required")
        if task.status is not TaskStatus.FAILED or task.ownership is not Ownership.HUMAN_OWNED:
            raise ValueError("only failed applications can be recovered")
        window_id = self.windows.window_for_task(task_id)
        stage = "allocate"
        try:
            if window_id is None and self.windows.open_count() >= self.max_open_applications:
                raise RuntimeError("managed browser capacity is full")
            if window_id is None:
                window_id = self.windows.allocate(task_id)
            stage = "observe_snapshot"
            snapshot = self.worker.recover_snapshot(task, window_id)
            if task.current_page_or_step and task.current_page_or_step != snapshot.page:
                self.store.expire_manual_resolutions(task_id, task.current_page_or_step)
            return self.store.recover_failed(task_id, window_id=window_id, page=snapshot.page,
                                             blocker=snapshot.blocker, authorization=authorization)
        except Exception as exc:
            self.store.record_recovery_failure(task_id, safe_failure(stage, exc, mode="recovery"))
            if window_id is not None:
                try:
                    self.windows.release(window_id)
                except Exception:
                    pass
            raise

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
                if getattr(self.windows, "recoverable_closed", lambda _id: False)(window_id):
                    try:
                        self.windows.release(window_id)
                    except RuntimeError:
                        # The known-dead process may fail to close cleanly; the
                        # runtime removes its task association before cleanup.
                        pass
                    window_id = None
                else:
                    # Preserve an ambiguous live session for the owner. Closing
                    # it could discard an unexpected human/site tab.
                    if task.status is TaskStatus.QUEUED:
                        task = self.store.start(task.id, browser_session_id=window_id)
                    outcome = WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.OTHER,
                                            page_or_step="managed_window")
                    task = self.store.pause_for_human(task.id, Blocker.OTHER,
                                                      page_or_step="managed_window")
                    return SchedulerStep(task, outcome)
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
            task = self.store.get_task(task.id)
            if not outcome.report_persisted:
                for audit in outcome.audits:
                    self.store.add_report_entry_once(task.id, page_or_step=audit.page_or_step,
                                                     visible_label=audit.visible_label,
                                                     semantic_key=audit.semantic_key,
                                                     kind=ReportKind.FIELD,
                                                     provenance=audit.provenance, action=audit.action,
                                                     verification=audit.verification,
                                                     review_state=ReviewState.NOT_REQUIRED,
                                                     requiredness=audit.requiredness,
                                                     question_identity=audit.question_identity)
                for issue in outcome.issues:
                    self.store.add_report_entry_once(task.id, page_or_step=issue.page_or_step,
                                                visible_label=issue.visible_label, semantic_key=issue.semantic_key,
                                                kind=ReportKind.FIELD, provenance=Provenance.UNRESOLVED,
                                                action="deferred", verification=Verification.NOT_ATTEMPTED,
                                                review_state=ReviewState.PENDING, reason=issue.reason,
                                                requiredness=issue.requiredness,
                                                question_identity=issue.question_identity)
            if outcome.kind is WorkerYield.HUMAN_BLOCKED:
                task = self.store.pause_for_human(task.id, outcome.blocker,
                                                  page_or_step=outcome.page_or_step,
                                                  diagnostic=outcome.failure_diagnostic)
            elif outcome.kind is WorkerYield.READY_FOR_REVIEW:
                task = self.store.ready_for_review(task.id, page_or_step=outcome.page_or_step)
            elif outcome.kind is WorkerYield.PROGRESS and task.blocker is not None:
                # PROGRESS follows a fresh successful observation. Failed or
                # empty observations leave the last known blocker intact.
                task = self.store.clear_observed_blocker(task.id)
            elif outcome.kind is WorkerYield.FAILED:
                task = self.store.fail(task.id, reason=outcome.failure,
                                       diagnostic=outcome.failure_diagnostic,
                                       page_or_step=outcome.page_or_step)
                task = self.release_window(task.id)
            return SchedulerStep(task, outcome)
        finally:
            self._working = False
