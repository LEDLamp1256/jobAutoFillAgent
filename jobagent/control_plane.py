"""Trusted local UI boundary and narrow JSON-safe application views."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from .batch_domain import (
    Blocker, HumanAction, HumanActor, HumanAuthorization, Ownership, Provenance, ReportEntry,
    ReportKind, ReviewState, RunStatus, TaskStatus, Verification,
)
from .persistence import BatchStore
from .scheduler import ApplicationScheduler, retains_prior_attention
from .dedupe import DuplicateKind, ListingInput, canonicalize_url
from .domain import ControlType, current_field_diagnostic
from urllib.parse import urlsplit


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

    def _run_view(self, run) -> dict:
        tasks = self.store.list_tasks(run.id)
        display_name = None
        if len(tasks) == 1:
            listing = self.store.get_listing(tasks[0].job_listing_id)
            if listing.title and listing.title != "Direct URL application":
                display_name = (f"{listing.title} · {listing.company}" if listing.company
                                else listing.title)
        return {
            "run_id": run.id, "status": run.status.value,
            "created_at": run.created_at, "completed_at": run.completed_at,
            "requested_sources": list(run.requested_sources),
            "requested_job_limit": run.requested_job_limit,
            "discovered_count": run.discovered_count, "queued_count": run.queued_count,
            "display_name": display_name,
        }

    @staticmethod
    def _entry_view(entry: ReportEntry, *, task=None, blocking_page: str | None = None) -> dict:
        technical = entry.semantic_key in {"direct_source_url", "page_classification"}
        current_page = task is not None and entry.page_or_step == task.current_page_or_step
        retaining_prior = bool(task is not None and task.resume_requested_at is not None and
                               task.status in {TaskStatus.QUEUED, TaskStatus.LAUNCHING,
                                               TaskStatus.AUTHENTICATING, TaskStatus.FILLING,
                                               TaskStatus.ADVANCING, TaskStatus.FAILED})
        retaining_prior = retaining_prior or bool(task is not None and task.status is TaskStatus.FAILED)
        blocking = bool(task is not None and
                        (task.status is TaskStatus.HUMAN_PAUSED or retaining_prior) and
                        task.blocker in {Blocker.NEEDS_ANSWER, Blocker.UNSUPPORTED_CONTROL,
                                         Blocker.BROWSER_TIMEOUT, Blocker.OTHER} and
                        entry.page_or_step == (blocking_page or task.current_page_or_step) and
                        entry.review_state is ReviewState.PENDING and
                        (entry.requiredness == "required" or
                         (entry.visible_label == "Current page" and entry.reason in {
                             "site_validation_blocked", "site_validation_required",
                             "ambiguous_navigation"})))
        if technical:
            group = "technical"
        elif blocking:
            group = "needs_attention"
        elif entry.review_state is ReviewState.PENDING or entry.action == "optional_skipped":
            group = "needs_review"
        else:
            group = "completed"
        return {
            "entry_id": entry.id, "task_id": entry.application_task_id,
            "page_or_step": entry.page_or_step, "visible_label": entry.visible_label,
            "semantic_key": entry.semantic_key, "kind": entry.kind.value,
            "provenance": entry.provenance.value, "action": entry.action,
            "verification": entry.verification.value, "review_state": entry.review_state.value,
            "reason": entry.reason, "created_at": entry.created_at,
            "narrative_text": entry.narrative_text,
            "requiredness": entry.requiredness,
            "category": "technical" if technical else "application",
            "report_group": group, "is_blocking": blocking,
            "requires_user_action": blocking,
            "is_current_page": current_page,
        }

    def _application_view(self, task) -> dict:
        listing = self.store.get_listing(task.job_listing_id)
        report = self.store.get_report(task.id)
        supplied = next((entry.page_or_step for entry in report.entries
                         if entry.semantic_key == "direct_source_url"), None)
        classifications = [entry for entry in report.entries if entry.semantic_key == "page_classification"]
        latest_classification = classifications[-1] if classifications else None
        window_id = self.scheduler.windows.window_for_task(task.id)
        # MCP browser_tabs/list may create a blank tab when none remain. Passive
        # views use only process-local knowledge; Bring validates the live page.
        if window_id is None:
            window_available = False
        elif hasattr(self.scheduler.windows, "passive_available"):
            window_available = self.scheduler.windows.passive_available(window_id)
        else:
            window_available = self.scheduler.windows.exists(window_id)
        # Count the same canonical field cards returned to the main report.
        # Raw history, technical details, and the separate narrative surface
        # must not inflate the number beside the application details.
        pending_review_count = self.get_application_report(task.id)["pending_review_count"]
        pending_narrative_count = sum(
            entry.kind is ReportKind.NARRATIVE and entry.review_state is ReviewState.PENDING
            for entry in self.store.current_report_entries(task.id))
        diagnostic_events = self.store.failure_events(task.id)
        timeout_stage = diagnostic_events[-1].stage if diagnostic_events else ""
        timeout_action = {
            "locate_control": "locating a control",
            "fill": "filling a field",
            "select_option": "selecting an option",
            "select_option_search": "searching choices",
            "verify": "verifying a field",
            "observe_snapshot": "observing the page",
            "page_advance": "advancing the page",
        }.get(timeout_stage, "working on this page")
        review_phase = None
        if task.status is TaskStatus.SUBMITTED_BY_HUMAN:
            review_phase = "submitted"
        elif task.status is TaskStatus.READY_FOR_REVIEW:
            if pending_review_count or pending_narrative_count:
                review_phase = "needs_review"
            elif task.review_checked_at is None:
                review_phase = "ready_for_final_review"
            else:
                review_phase = "ready_to_submit"
        return {
            "task_id": task.id, "run_id": task.run_id,
            "listing_id": listing.id, "company": listing.company, "title": listing.title,
            "source_url": supplied or (listing.application_url if listing.source == "direct_url"
                                       else listing.listing_url),
            "live_url": latest_classification.page_or_step if latest_classification else None,
            "classification": latest_classification.action.upper() if latest_classification else None,
            "status": task.status.value, "ownership": task.ownership.value,
            "current_page_or_step": task.current_page_or_step,
            "attention_category": task.status.value if task.status in {
                TaskStatus.HUMAN_PAUSED, TaskStatus.READY_FOR_REVIEW} or
                retains_prior_attention(task) else None,
            "blocker": task.blocker.value if task.blocker else None,
            "blocker_label": {
                "needs_answer": "Required answer needed",
                "unsupported_control": "Required control needs manual input",
                "browser_timeout": (f"Automation timed out while {timeout_action}. "
                                    "Check the employer page, then Resume when ready."),
                "login_required": "Authentication required",
                "mfa_required": "Authentication required",
                "captcha_required": "Human verification required",
                "other": "Application needs attention",
            }.get(task.blocker.value) if task.blocker else None,
            "failure_reason": task.last_error,
            "failure_events": [{"reason": event.reason, "stage": event.stage,
                                "category": event.category, "detail": event.detail,
                                "mode": event.mode, "occurred_at": event.occurred_at}
                               for event in diagnostic_events],
            "recovery_available": task.status is TaskStatus.FAILED,
            "window_associated": task.browser_session_id is not None,
            "window_available": window_available,
            "resume_available": task.status is TaskStatus.HUMAN_PAUSED and
                                task.ownership is Ownership.HUMAN_OWNED,
            "ready_for_review": task.status is TaskStatus.READY_FOR_REVIEW,
            "review_phase": review_phase,
            "final_review_available": task.status is TaskStatus.READY_FOR_REVIEW and
                                      pending_review_count == 0 and pending_narrative_count == 0 and
                                      task.review_checked_at is None,
            "final_review_checked": task.review_checked_at is not None,
            "record_submission_available": review_phase == "ready_to_submit" and
                                           task.ownership is Ownership.HUMAN_OWNED and
                                           task.review_checked_by is not None,
            "pending_review_count": pending_review_count,
            "pending_narrative_count": pending_narrative_count,
            "created_at": task.created_at, "updated_at": task.updated_at,
        }

    def list_runs(self) -> list[dict]:
        return [self._run_view(run) for run in self.store.list_runs()]

    def start_application_url(self, url: str) -> dict:
        url = url.strip()
        try:
            parts = urlsplit(url)
            if (not url or any(char.isspace() or ord(char) < 32 for char in url) or
                    (parts.scheme.lower() != "https" and not (
                        parts.scheme.lower() == "http" and
                        parts.hostname in {"localhost", "127.0.0.1"})) or not parts.hostname or
                    parts.username or parts.password or parts.port == 0):
                raise ValueError("invalid URL")
            canonical = canonicalize_url(url)
        except ValueError:
            raise ControlPlaneError("BAD_REQUEST", "Enter an HTTPS job URL or a loopback HTTP URL.") from None
        item = ListingInput("direct_url", parts.hostname or "Job", canonical or url,
                            application_url=url)
        matches = self.store.listings_for_url(url)
        if len(matches) > 1:
            raise ControlPlaneError("POSSIBLE_DUPLICATE", "This URL matches multiple listings.")
        if matches:
            listing = matches[0]
            if self.store.was_submitted(listing.id):
                raise ControlPlaneError("ALREADY_SUBMITTED", "This listing was already submitted by the owner.")
            existing = [task for task in self.store.list_tasks()
                        if task.job_listing_id == listing.id and task.status is not TaskStatus.SKIPPED
                        and not self.store.is_archived(task.id)]
            if existing:
                return self.get_application(existing[-1].id)
            if listing.possible_duplicate_of:
                raise ControlPlaneError("POSSIBLE_DUPLICATE", "This listing needs duplicate review before intake.")
            if listing.source == "direct_url":
                listing = self.store.set_direct_source_url(listing.id, url)
        else:
            duplicate = self.store.check_duplicate(item)
            if duplicate.kind is not DuplicateKind.NEW:
                raise ControlPlaneError("POSSIBLE_DUPLICATE", "This URL needs duplicate review before intake.")
            listing, _ = self.store.register_listing(item)
            listing = self.store.set_direct_source_url(listing.id, url)
        run = self.store.create_run(("direct_url",), 1)
        self.store.set_run_status(run.id, RunStatus.RUNNING)
        task = self.store.queue_task(run.id, listing.id)
        self.store.add_report_entry(task.id, page_or_step=url, visible_label="Supplied URL",
                                    semantic_key="direct_source_url", kind=ReportKind.FIELD,
                                    provenance=Provenance.DETERMINISTIC, action="supplied",
                                    verification=Verification.VERIFIED,
                                    review_state=ReviewState.NOT_REQUIRED)
        return self.get_application(task.id)

    def get_run(self, run_id: str) -> dict:
        return self._run_view(self.store.get_run(run_id))

    def list_applications(self, run_id: str | None = None) -> list[dict]:
        return [self._application_view(task) for task in self.scheduler.tasks(run_id)]

    def get_application(self, task_id: str) -> dict:
        return self._application_view(self.store.get_task(task_id))

    def archive_application(self, task_id: str) -> dict:
        task = self.store.archive_by_human(
            task_id, authorization=self._owner(HumanAction.ARCHIVE_APPLICATION))
        return {"task_id": task.id, "archived": True,
                "removed_from_queue": task.status is TaskStatus.SKIPPED}

    def list_attention_required(self, run_id: str | None = None) -> list[dict]:
        return [self._application_view(item.task) for item in self.scheduler.attention(run_id)]

    def get_application_report(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        current = self.store.current_report_entries(task_id)
        blocking_page = task.current_page_or_step
        if (blocking_page in {None, "UNKNOWN"} and
                ((task.status is TaskStatus.HUMAN_PAUSED and task.blocker is Blocker.OTHER) or
                 retains_prior_attention(task))):
            # UNKNOWN is a classification stop, not a form-page label. Until
            # another form page is observed, retain the last observed required
            # questions as attention items without claiming their refs are live.
            form_fields = [entry for entry in current if entry.kind is ReportKind.FIELD and
                           entry.semantic_key not in {"direct_source_url", "page_classification"} and
                           entry.visible_label != "Current page" and entry.page_or_step]
            if form_fields:
                blocking_page = max(form_fields, key=lambda entry: (entry.created_at, entry.id)).page_or_step
        entries = [self._entry_view(entry, task=task, blocking_page=blocking_page)
                   for entry in current]
        priority = {"needs_attention": 0, "needs_review": 1, "completed": 2,
                    "technical": 3}
        entries.sort(key=lambda entry: (priority[entry["report_group"]],
                                        not entry["is_current_page"], entry["visible_label"].casefold()))
        attention = sum(entry["kind"] == ReportKind.FIELD.value and
                        entry["report_group"] == "needs_attention" for entry in entries)
        review = sum(entry["kind"] == ReportKind.FIELD.value and
                     entry["report_group"] == "needs_review" for entry in entries)
        return {"task_id": task_id, "entries": entries,
                "needs_attention_count": attention, "needs_review_count": review,
                "pending_review_count": attention + review}

    def diagnose_current_fields(self, task_id: str) -> dict:
        """Explicit read-only fresh observation; values and browser refs never leave Python."""
        task = self.store.get_task(task_id)
        windows = self.scheduler.windows
        window_id = windows.window_for_task(task_id)
        if window_id is None or not windows.exists(window_id):
            raise ControlPlaneError("WINDOW_UNAVAILABLE", "Open the application browser first.")
        observation = windows.run_browser(window_id, windows.browser_for(window_id).observe())
        self.store.reconcile_fresh_observation(task_id, observation)
        reports: dict[str, list] = {}
        for entry in self.store.current_report_entries(task_id):
            if entry.question_identity and entry.page_or_step == (
                    observation.progress_text or observation.heading or "application page"):
                reports.setdefault(entry.question_identity, []).append(entry)
        fields = []
        for question in observation.questions:
            if len(fields) == 64:
                break
            if question.control_type is ControlType.SECRET or re.search(
                    r"password|passphrase|secret|access token|api token|cookie", question.label, re.I):
                continue
            history = reports.get(question.report_identity(), [])
            report_group = (self._entry_view(history[0], task=task)[
                "report_group"] if len(history) == 1 else None)
            manual = ("human_attested" if len(history) == 1 and
                      history[0].action == "human_resolved" and
                      history[0].provenance is Provenance.HUMAN_PROVIDED else None)
            supported = bool((question.target_ref or
                              question.discovery_source in {"dom_fallback", "merged"} and
                              question.control_type in {ControlType.CHOICE, ControlType.TYPEAHEAD}) and (
                question.control_type in {ControlType.TEXT, ControlType.DATE,
                                          ControlType.TYPEAHEAD, ControlType.TOGGLE,
                                          ControlType.FILE} or
                question.control_type is ControlType.CHOICE))
            fields.append(current_field_diagnostic(
                question, automation_supported=supported,
                blocking=question.required is True and not question.answer_state().satisfied,
                report_group=report_group, manual_resolution=manual))
        summary = observation.discovery_summary
        return {"task_id": task_id, "captured_at": datetime.now(timezone.utc).isoformat(),
                "diagnostic_generation": uuid4().hex,
                "field_count": len(observation.questions),
                "truncated": len(observation.questions) > 64, "fields": fields,
                "discovery_summary": {
                    "normalized_field_count": len(observation.questions),
                    "accessibility_question_count": (summary.accessibility_question_count
                                                     if summary else len(observation.questions)),
                    "dom_recovered_field_count": summary.dom_recovered_field_count if summary else 0,
                    "raw_actionable_count": summary.raw_actionable_count if summary else 0,
                    "ignored_actionable_count": (sum(count for _, count in summary.ignored_reasons)
                                                 if summary else 0),
                    "ignored_reasons": dict(summary.ignored_reasons) if summary else {},
                    "navigation_count": len(observation.navigation_controls),
                    "section_action_count": len(observation.section_actions),
                    "truncated": summary.truncated if summary else False}}

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

    def resume_and_reconcile(self, task_id: str) -> dict:
        task = self.resume_application(task_id)
        # Explicit native Resume includes one bounded worker pass. The worker
        # persists its final fresh observation before this response is sent.
        self.scheduler.step(task["run_id"])
        return self.get_application(task_id)

    def open_application(self, task_id: str) -> dict:
        """Explicit owner request to restore a paused task's browser."""
        application = self.get_application(task_id)
        if application["window_available"]:
            raise ControlPlaneError("WINDOW_AVAILABLE", "Use Bring Window to Front for the live browser.")
        return self.resume_application(task_id)

    def recover_application(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if task.status is not TaskStatus.FAILED:
            raise ControlPlaneError("INVALID_TRANSITION", "Only failed applications can be recovered.")
        self.scheduler.recover_application(task_id, self._owner(HumanAction.RECOVER_APPLICATION))
        return self.get_application(task_id)

    def bring_window_to_front(self, task_id: str) -> dict:
        self.scheduler.bring_window_to_front(task_id)
        return {"task_id": task_id, "foregrounded": True}

    def review_report_entry(self, entry_id: str) -> dict:
        entry = self.store.review_entry_by_human(
            entry_id, authorization=self._owner(HumanAction.REVIEW_ENTRY))
        return self._entry_view(entry)

    def resolve_field(self, entry_id: str) -> dict:
        current = self.store.get_report_entry(entry_id)
        task = self.store.get_task(current.application_task_id)
        visible = next((item for item in self.get_application_report(task.id)["entries"]
                        if item["entry_id"] == entry_id), None)
        if visible is None or visible["report_group"] != "needs_attention":
            raise ControlPlaneError("INVALID_TRANSITION", "field is not awaiting your attention")
        entry = self.store.resolve_field_by_human(
            entry_id, authorization=self._owner(HumanAction.RESOLVE_FIELD))
        return self._entry_view(entry, task=self.store.get_task(task.id))

    def undo_field_resolution(self, entry_id: str) -> dict:
        """Reopen one attested field; never touches the employer page."""
        current = self.store.get_report_entry(entry_id)
        task = self.store.get_task(current.application_task_id)
        visible = next((item for item in self.get_application_report(task.id)["entries"]
                        if item["entry_id"] == entry_id), None)
        if visible is None or visible["action"] != "human_resolved":
            raise ControlPlaneError("INVALID_TRANSITION", "field is not manually resolved")
        try:
            entry = self.store.undo_field_resolution_by_human(
                entry_id, authorization=self._owner(HumanAction.UNDO_FIELD_RESOLUTION))
        except ValueError as exc:
            raise ControlPlaneError("INVALID_TRANSITION", str(exc)) from None
        return self._entry_view(entry, task=self.store.get_task(task.id))

    def mark_final_review_checked(self, task_id: str) -> dict:
        self.store.mark_review_checked(task_id, authorization=self._owner(HumanAction.FINAL_REVIEW))
        return self.get_application(task_id)

    def record_submission(self, task_id: str) -> dict:
        self.store.mark_submitted_by_human(
            task_id, authorization=self._owner(HumanAction.RECORD_SUBMISSION))
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
