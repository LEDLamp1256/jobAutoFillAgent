"""Trusted local UI boundary and narrow JSON-safe application views."""

from __future__ import annotations

from .batch_domain import (
    HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance, ReportEntry,
    ReportKind, ReviewState, TaskStatus, Verification,
)
from .persistence import BatchStore
from .scheduler import ApplicationScheduler


class ControlPlaneError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LocalControlPlane:
    """Only explicit UI commands here may create LOCAL_OWNER authorization."""

    def __init__(self, store: BatchStore, scheduler: ApplicationScheduler):
        if scheduler.store is not store:
            raise ValueError("scheduler and control plane must share a store")
        self.store = store
        self.scheduler = scheduler

    @staticmethod
    def _owner(action: HumanAction) -> HumanAuthorization:
        return HumanAuthorization(HumanActor.LOCAL_OWNER, action)

    @staticmethod
    def _run_view(run) -> dict:
        return {
            "run_id": run.id, "status": run.status.value,
            "created_at": run.created_at, "completed_at": run.completed_at,
            "requested_sources": list(run.requested_sources),
            "requested_job_limit": run.requested_job_limit,
            "discovered_count": run.discovered_count, "queued_count": run.queued_count,
        }

    @staticmethod
    def _entry_view(entry: ReportEntry) -> dict:
        return {
            "entry_id": entry.id, "task_id": entry.application_task_id,
            "page_or_step": entry.page_or_step, "visible_label": entry.visible_label,
            "semantic_key": entry.semantic_key, "kind": entry.kind.value,
            "provenance": entry.provenance.value, "action": entry.action,
            "verification": entry.verification.value, "review_state": entry.review_state.value,
            "reason": entry.reason, "created_at": entry.created_at,
            "narrative_text": entry.narrative_text,
        }

    def _application_view(self, task) -> dict:
        listing = self.store.get_listing(task.job_listing_id)
        report = self.store.get_report(task.id)
        window_id = self.scheduler.windows.window_for_task(task.id)
        # MCP browser_tabs/list may create a blank tab when none remain. Passive
        # views use only process-local knowledge; Bring validates the live page.
        if window_id is None:
            window_available = False
        elif hasattr(self.scheduler.windows, "passive_available"):
            window_available = self.scheduler.windows.passive_available(window_id)
        else:
            window_available = self.scheduler.windows.exists(window_id)
        pending_review_count = sum(e.review_state is ReviewState.PENDING for e in report.entries)
        return {
            "task_id": task.id, "run_id": task.run_id,
            "listing_id": listing.id, "company": listing.company, "title": listing.title,
            "status": task.status.value, "ownership": task.ownership.value,
            "current_page_or_step": task.current_page_or_step,
            "attention_category": task.status.value if task.status in {
                TaskStatus.HUMAN_PAUSED, TaskStatus.READY_FOR_REVIEW} else None,
            "blocker": task.blocker.value if task.blocker else None,
            "failure_reason": task.last_error,
            "window_associated": task.browser_session_id is not None,
            "window_available": window_available,
            "resume_available": task.status is TaskStatus.HUMAN_PAUSED and
                                task.ownership is Ownership.HUMAN_OWNED,
            "ready_for_review": task.status is TaskStatus.READY_FOR_REVIEW,
            "final_review_available": task.status is TaskStatus.READY_FOR_REVIEW and
                                      pending_review_count == 0 and task.review_checked_at is None,
            "final_review_checked": task.review_checked_at is not None,
            "pending_review_count": pending_review_count,
            "pending_narrative_count": sum(e.kind is ReportKind.NARRATIVE and
                                           e.review_state is ReviewState.PENDING for e in report.entries),
            "created_at": task.created_at, "updated_at": task.updated_at,
        }

    def list_runs(self) -> list[dict]:
        return [self._run_view(run) for run in self.store.list_runs()]

    def get_run(self, run_id: str) -> dict:
        return self._run_view(self.store.get_run(run_id))

    def list_applications(self, run_id: str | None = None) -> list[dict]:
        return [self._application_view(task) for task in self.scheduler.tasks(run_id)]

    def get_application(self, task_id: str) -> dict:
        return self._application_view(self.store.get_task(task_id))

    def list_attention_required(self, run_id: str | None = None) -> list[dict]:
        return [self._application_view(item.task) for item in self.scheduler.attention(run_id)]

    def get_application_report(self, task_id: str) -> dict:
        report = self.store.get_report(task_id)
        return {"task_id": task_id, "entries": [self._entry_view(e) for e in report.entries]}

    def get_narrative_entries(self, task_id: str) -> dict:
        report = self.store.get_report(task_id)
        return {"task_id": task_id, "entries": [self._entry_view(e) for e in report.entries
                                                 if e.kind is ReportKind.NARRATIVE]}

    def resume_application(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if task.status is not TaskStatus.HUMAN_PAUSED or task.ownership is not Ownership.HUMAN_OWNED:
            raise ControlPlaneError("NOT_RESUMABLE", "application is not human-paused")
        self.scheduler.resume_application(task_id, self._owner(HumanAction.RESUME))
        return self.get_application(task_id)

    def bring_window_to_front(self, task_id: str) -> dict:
        self.scheduler.bring_window_to_front(task_id)
        return {"task_id": task_id, "foregrounded": True}

    def review_report_entry(self, entry_id: str) -> dict:
        entry = self.store.review_entry_by_human(
            entry_id, authorization=self._owner(HumanAction.REVIEW_ENTRY))
        return self._entry_view(entry)

    def mark_final_review_checked(self, task_id: str) -> dict:
        self.store.mark_review_checked(task_id, authorization=self._owner(HumanAction.FINAL_REVIEW))
        return self.get_application(task_id)

    def replace_narrative(self, entry_id: str, text: str) -> dict:
        original = self.store.get_report_entry(entry_id)
        task = self.store.get_task(original.application_task_id)
        if original.kind is not ReportKind.NARRATIVE or task.status is not TaskStatus.READY_FOR_REVIEW:
            raise ControlPlaneError("INVALID_TRANSITION", "narrative is not in final review")
        if original.review_state is not ReviewState.PENDING:
            raise ControlPlaneError("INVALID_TRANSITION", "narrative is not pending review")
        if not text.strip():
            raise ControlPlaneError("BAD_REQUEST", "narrative text must be nonempty")
        replacement = self.store.add_report_entry(
            task.id, page_or_step=original.page_or_step, visible_label=original.visible_label,
            semantic_key=original.semantic_key, kind=ReportKind.NARRATIVE,
            provenance=Provenance.HUMAN_PROVIDED, action="replaced",
            verification=Verification.VERIFIED, review_state=ReviewState.APPROVED,
            narrative_text=text, authorization=self._owner(HumanAction.REPLACE_NARRATIVE))
        self.store.review_entry_by_human(entry_id, authorization=self._owner(HumanAction.REVIEW_ENTRY))
        return self._entry_view(replacement)
