"""V2-10 launch/login followed by one bounded current-page fill pass."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .application_launcher import ApplicationLauncher
from .application_worker import FieldAudit, ReviewIssue, WorkRequest, WorkerOutcome, WorkerYield
from .authentication import (
    InterventionReason, LoginIdentity, LoginOrchestrator, LoginStatus, PageKind, classify_page,
)
from .batch_domain import (Blocker, FailureReason, Provenance, ReportKind,
                           ReviewState, TaskStatus, Verification)
from .controller import ApplicationController, ControllerStop
from .domain import AnswerSource, ApplicationSession, ControlType, page_scope
from .persistence import BatchStore
from .failure_diagnostics import BrowserStageError, is_recoverable_page_timeout, safe_failure
from .resolution import CandidateProfile, DeterministicAnswerResolver, ProfileError, ResolutionStatus
from .snapshot import SnapshotAccessChallenge


def _requiredness(required: bool | None) -> str:
    return "required" if required is True else "optional" if required is False else "unknown"


def classify_direct_page(observation):
    """Classify a fresh direct-intake observation without retaining target refs."""
    extra_text = " ".join((observation.progress_text or "", *observation.validation_messages)).casefold()
    if any(term in extra_text for term in ("captcha", "verify you are human", "unusual traffic")):
        return "BLOCKER"
    state = classify_page(observation)
    if state.kind is PageKind.HUMAN_INTERVENTION_REQUIRED:
        return "BLOCKER" if state.reason is InterventionReason.ACCESS_CHALLENGE else "AUTH"
    if state.kind is PageKind.LOGIN:
        return "AUTH"
    heading = (observation.heading or "").casefold()
    path = urlsplit(observation.location).path.casefold()
    controls = " ".join(control.label.casefold() for control in observation.navigation_controls)
    form_controls = [question for question in observation.questions
                     if question.control_type is not ControlType.UNKNOWN and
                     "search" not in question.label.casefold()]
    application_field = any(any(term in question.label.casefold() for term in (
        "name", "email", "phone", "resume", "cv", "cover letter", "work experience",
        "education", "authorization")) for question in form_controls)
    if ((form_controls and (application_field or len(form_controls) >= 2 or
                            "apply" in path or "application" in path or
                            "application" in heading)) or observation.review_like):
        return "APPLICATION"
    posting_text = " ".join((heading, path, controls))
    if (observation.heading and any(word in posting_text for word in
            ("job", "career", "position", "opening", "role", "apply")) and
            ("apply" in controls or "job" in heading or "position" in heading or
             "opening" in heading or "role" in heading)):
        return "JOB_POSTING"
    return "UNKNOWN"


@dataclass(frozen=True)
class RecoverySnapshot:
    page: str
    blocker: Blocker
    category: str
    location: str


class LocalLoginConfiguration:
    """Read optional login_accounts from the existing gitignored config.json."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _account(self, account_id: str) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as source:
                data = json.load(source)
            account = data.get("login_accounts", {}).get(account_id)
            return account if isinstance(account, dict) else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def identity(self, account_id: str) -> LoginIdentity | None:
        account = self._account(account_id)
        username = account.get("username") if account else None
        if not isinstance(username, str) or not username.strip():
            return None
        return LoginIdentity(account_id, username)

    def destination_is_authorized(self, location: str, identity: LoginIdentity) -> bool:
        try:
            url = urlsplit(location)
            if (url.scheme != "https" or not url.hostname or url.username or url.password or
                    url.hostname != identity.account_id):
                return False
            current = self.identity(url.hostname)
            return current == identity
        except ValueError:
            return False

    async def get_password(self, account_id: str) -> str | None:
        account = self._account(account_id)
        password = account.get("password") if account else None
        return password if isinstance(password, str) and password else None


_BLOCKER = {
    InterventionReason.ACCESS_CHALLENGE: Blocker.CAPTCHA_REQUIRED,
    InterventionReason.MFA_REQUIRED: Blocker.MFA_REQUIRED,
    InterventionReason.SSO_REQUIRED: Blocker.LOGIN_REQUIRED,
    InterventionReason.EMAIL_VERIFICATION_REQUIRED: Blocker.LOGIN_REQUIRED,
    InterventionReason.HUMAN_JUDGMENT_REQUIRED: Blocker.LOGIN_REQUIRED,
}


class LaunchAndLoginWorker:
    def __init__(self, store: BatchStore, launcher: ApplicationLauncher,
                 credentials: LocalLoginConfiguration):
        self.store = store
        self.launcher = launcher
        self.credentials = credentials

    def recover_snapshot(self, task, window_id: str) -> RecoverySnapshot:
        """Open the durable URL and observe only; never invoke the fill controller."""
        listing = self.store.get_listing(task.job_listing_id)
        direct = "direct_url" in self.store.get_run(task.run_id).requested_sources
        if direct:
            supplied = next((entry.page_or_step for entry in self.store.get_report(task.id).entries
                             if entry.semantic_key == "direct_source_url"), None)
            if supplied is None and listing.source == "direct_url":
                supplied = listing.application_url
            if supplied is None:
                raise BrowserStageError("recovery_url", ValueError("missing durable URL"))
            listing = replace(listing, application_url=supplied)
        observation = self.launcher.open(task, listing, window_id)
        try:
            state = None if direct else classify_page(observation)
            category = classify_direct_page(observation) if direct else state.kind.value.upper()
        except Exception as exc:
            raise BrowserStageError("recovery_classify", exc) from None
        if direct:
            self._record_direct_classification(task.id, observation, category)
        page = page_scope(observation) or category
        if category == "APPLICATION":
            self._record_observed_fields(task.id, page, observation,
                                         pending_reason="awaiting_resume")
        blocker = (Blocker.LOGIN_REQUIRED if category in {"AUTH", "LOGIN"} else
                   Blocker.CAPTCHA_REQUIRED if category == "BLOCKER" else
                   _BLOCKER.get(state.reason, Blocker.OTHER)
                   if state is not None and state.kind is PageKind.HUMAN_INTERVENTION_REQUIRED else
                   task.blocker if category == "APPLICATION" and task.blocker in {
                       Blocker.NEEDS_ANSWER, Blocker.UNSUPPORTED_CONTROL, Blocker.OTHER}
                   else Blocker.OTHER)
        return RecoverySnapshot(page, blocker, category, observation.location)

    def _profile(self) -> CandidateProfile:
        try:
            return CandidateProfile.from_json(self.credentials.path)
        except (ProfileError, ValueError):
            # Missing candidate facts leave fields for the owner; login-only
            # configuration remains valid for the accepted V2-10 path.
            return CandidateProfile.from_mapping({
                "personal_info": {}, "work_history": [], "education": [],
                "skills": {}, "qa_bank": {}, "documents": {},
                "application_preferences": {},
            })

    def _current_page(self, request: WorkRequest, observation) -> WorkerOutcome:
        page = page_scope(observation) or "application page"
        controller = None
        session = ApplicationSession(request.task.id, observation.location)
        try:
            browser = self.launcher.windows.browser_for(request.window_id)
            resolved = self.store.active_manual_resolutions(request.task.id, page)
            controller = ApplicationController(browser, DeterministicAnswerResolver(self._profile()))
            result = self.launcher.windows.run_browser(request.window_id,
                controller.run(
                    session, allow_advance=False, initial_observation=observation,
                    current_page_only=True, require_trusted_location=True,
                    manually_resolved=resolved))
        except Exception as exc:
            # The managed operation has settled before a timeout is raised.
            # Keep only the last completed fresh observation; an interrupted
            # mutation without a post-action observation remains unverified.
            latest = session.current_observation or observation
            latest_page = page_scope(latest) or page
            self._record_observed_fields(request.task.id, latest_page, latest,
                                         pending_reason="interrupted_before_completion")
            self.store.reconcile_fresh_observation(request.task.id, latest)
            diagnostic = safe_failure(controller.active_stage if controller else "locate_browser", exc)
            if is_recoverable_page_timeout(diagnostic):
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.BROWSER_TIMEOUT,
                                     failure_diagnostic=diagnostic, page_or_step=latest_page,
                                     report_persisted=True)
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR,
                                 failure_diagnostic=diagnostic,
                                 page_or_step=latest_page, report_persisted=True)
        final = session.current_observation
        if final is None:
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR,
                                 failure_diagnostic=safe_failure("verify"))
        page_observation = observation if result.stop is ControllerStop.PAGE_ADVANCED else final
        page = page_scope(page_observation) or "application page"
        audits = tuple(FieldAudit(
            field.question.label, page, field.semantic_key, field.action,
            Provenance.VERIFIED_PROFILE if field.source is AnswerSource.CANDIDATE_PROFILE
            else Provenance.DETERMINISTIC, Verification.VERIFIED,
            _requiredness(field.question.required), field.question.report_identity())
            for field in result.fields if field.verified)
        issues = tuple(ReviewIssue(field.question.label, field.reason or "needs_review",
                                   page, field.semantic_key, _requiredness(field.question.required),
                                   field.question.report_identity())
                       for field in result.fields if field.needs_review)
        if result.stop is ControllerStop.STOPPED_BEFORE_ADVANCE:
            boundary_reason = "ambiguous_navigation"
            issues = (*issues, ReviewIssue("Current page", boundary_reason, page))
        if result.stop is ControllerStop.NEEDS_REVIEW and result.reason == "terminal navigation is ambiguous":
            issues = (*issues, ReviewIssue("Current page", "ambiguous_navigation", page))
        if result.stop is ControllerStop.VALIDATION_BLOCKED:
            issues = (*issues, ReviewIssue("Current page", "site_validation_blocked", page))
        for audit in audits:
            self.store.add_report_entry_once(request.task.id, page_or_step=audit.page_or_step,
                                             visible_label=audit.visible_label,
                                             semantic_key=audit.semantic_key, kind=ReportKind.FIELD,
                                             provenance=audit.provenance, action=audit.action,
                                             verification=audit.verification,
                                             review_state=ReviewState.NOT_REQUIRED,
                                             requiredness=audit.requiredness,
                                             question_identity=audit.question_identity)
        for issue in issues:
            self.store.add_report_entry_once(request.task.id, page_or_step=issue.page_or_step,
                                             visible_label=issue.visible_label,
                                             semantic_key=issue.semantic_key, kind=ReportKind.FIELD,
                                             provenance=Provenance.UNRESOLVED, action="deferred",
                                             verification=Verification.NOT_ATTEMPTED,
                                             review_state=ReviewState.PENDING, reason=issue.reason,
                                             requiredness=issue.requiredness,
                                             question_identity=issue.question_identity)
        for field in result.fields:
            if field.verified or field.needs_review or field.action == "human_resolved":
                continue
            if result.stop is ControllerStop.PAGE_ADVANCED and field.action != "manual_complete":
                continue
            self.store.add_report_entry_once(
                request.task.id, page_or_step=page, visible_label=field.question.label,
                semantic_key=field.semantic_key, kind=ReportKind.FIELD,
                provenance=Provenance.SKIPPED, action=field.action,
                verification=Verification.NOT_ATTEMPTED,
                review_state=ReviewState.NOT_REQUIRED,
                reason=("human_entered_value" if field.action == "manual_complete" else
                        "optional_without_trusted_answer"),
                requiredness=_requiredness(field.question.required),
                question_identity=field.question.report_identity())
        self.store.reconcile_fresh_observation(request.task.id, page_observation)
        if result.stop is ControllerStop.FAILED:
            diagnostic = result.failure_diagnostic or safe_failure(result.failure_stage or "controller")
            if is_recoverable_page_timeout(diagnostic):
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.BROWSER_TIMEOUT,
                                     failure_diagnostic=diagnostic, page_or_step=page,
                                     report_persisted=True)
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR,
                                 failure_diagnostic=diagnostic,
                                 page_or_step=page, report_persisted=True)
        # This is the final observation. Presentation calls do not change form
        # values, and no later form mutation consumes these refs.
        for field in result.fields:
            ref = field.question.target_ref
            if not ref:
                continue
            state = ("verified" if field.verified else
                     "needs-review" if field.needs_review else "clear")
            try:
                self.launcher.windows.run_browser(request.window_id,
                    browser.annotate_field(ref, final.observation_id, state))
            except Exception:
                # A missing/ambiguous visual target cannot authorize a fill or
                # invalidate the durable Python decision.
                pass
        if result.stop is ControllerStop.READY_FOR_REVIEW:
            return WorkerOutcome(WorkerYield.READY_FOR_REVIEW, page_or_step=page,
                                 audits=audits, report_persisted=True)
        if result.stop is ControllerStop.PAGE_ADVANCED:
            self.store.expire_manual_resolutions(request.task.id, page)
            return WorkerOutcome(WorkerYield.PROGRESS, page_or_step="page_advanced",
                                 audits=audits, report_persisted=True)
        if result.stop is ControllerStop.NEEDS_REVIEW:
            required_issues = [field for field in result.fields
                               if field.needs_review and field.question.required is True]
            blocker = (Blocker.UNSUPPORTED_CONTROL if required_issues and all(
                field.reason == "unsupported_control" for field in required_issues) else
                Blocker.NEEDS_ANSWER if required_issues else Blocker.OTHER)
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues=issues,
                                 blocker=blocker, page_or_step=page, audits=audits,
                                 report_persisted=True)
        if result.stop is ControllerStop.VALIDATION_BLOCKED:
            blocker = (Blocker.NEEDS_ANSWER if any(
                field.needs_review and field.question.required is True for field in result.fields)
                else Blocker.OTHER)
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues=issues,
                                 blocker=blocker, page_or_step=page, audits=audits,
                                 report_persisted=True)
        return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues=issues,
                             blocker=Blocker.OTHER, page_or_step=page, audits=audits,
                             report_persisted=True)

    def _record_observed_fields(self, task_id: str, page: str, observation,
                                *, pending_reason: str) -> None:
        """Save value-free field presence when no controller decision can be trusted."""
        current = {(entry.page_or_step, entry.question_identity): entry
                   for entry in self.store.current_report_entries(task_id)
                   if entry.question_identity}
        resolver = DeterministicAnswerResolver(self._profile())
        for question in observation.questions:
            identity = question.report_identity()
            prior = current.get((page, identity))
            if prior and prior.action == "human_resolved":
                continue
            answered = question.answer_state().satisfied
            if prior and answered and (prior.verification is Verification.VERIFIED or
                                       prior.action == "manual_complete"):
                continue
            if prior and not answered and prior.review_state is ReviewState.PENDING:
                continue
            required = question.required is True
            reason = pending_reason
            if required and not answered:
                try:
                    if resolver.resolve(question, task_id).status is not ResolutionStatus.SAFE_TO_FILL:
                        reason = "no_safe_answer"
                except Exception:
                    pass
            self.store.add_report_entry_once(
                task_id, page_or_step=page, visible_label=question.label,
                semantic_key=question.semantic_key, kind=ReportKind.FIELD,
                provenance=Provenance.SKIPPED if answered or not required else Provenance.UNRESOLVED,
                action="manual_complete" if answered else "deferred" if required else "optional_skipped",
                verification=Verification.NOT_ATTEMPTED,
                review_state=ReviewState.PENDING if required and not answered else ReviewState.NOT_REQUIRED,
                reason="human_entered_value" if answered else reason
                       if required else "optional_without_trusted_answer",
                requiredness=_requiredness(question.required), question_identity=identity)

    def _record_direct_classification(self, task_id: str, observation, category: str) -> None:
        if category in {"APPLICATION", "JOB_POSTING"}:
            task = self.store.get_task(task_id)
            self.store.enrich_direct_listing(task.job_listing_id, title=observation.job_title,
                                             company=observation.company)
        self.store.add_report_entry_once(
            task_id, page_or_step=observation.location, visible_label="Page classification",
            semantic_key="page_classification", kind=ReportKind.FIELD,
            provenance=Provenance.DETERMINISTIC, action=category.lower(),
            verification=Verification.VERIFIED, review_state=ReviewState.NOT_REQUIRED)

    def work_until_yield(self, request: WorkRequest) -> WorkerOutcome:
        stage = "navigate"
        try:
            listing = self.store.get_listing(request.task.job_listing_id)
            direct_run = "direct_url" in self.store.get_run(request.task.run_id).requested_sources
            if direct_run:
                supplied = next((entry.page_or_step for entry in self.store.get_report(request.task.id).entries
                                 if entry.semantic_key == "direct_source_url"), None)
                if supplied is None and listing.source == "direct_url":
                    supplied = listing.application_url
                if supplied is None:
                    raise ValueError("direct URL task has no supplied URL")
                listing = replace(listing, application_url=supplied)
            observation = self.launcher.open(request.task, listing, request.window_id)
            stage = "classify"
            live = urlsplit(observation.location)
            if live.scheme.lower() != "https" and not (live.scheme.lower() == "http" and
                    live.hostname in {"localhost", "127.0.0.1"}):
                if direct_run:
                    self._record_direct_classification(request.task.id, observation, "BLOCKER")
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED,
                                     blocker=Blocker.OTHER if direct_run else Blocker.LOGIN_REQUIRED,
                                     page_or_step="insecure_page")
            if direct_run:
                category = classify_direct_page(observation)
                self._record_direct_classification(request.task.id, observation, category)
                if category == "APPLICATION":
                    if request.task.status in {TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING}:
                        self.store.advance_state(request.task.id, TaskStatus.FILLING)
                    stage = "controller"
                    return self._current_page(request, observation)
                blocker = (Blocker.LOGIN_REQUIRED if category == "AUTH" else
                           Blocker.CAPTCHA_REQUIRED if category == "BLOCKER" else Blocker.OTHER)
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=blocker,
                                     page_or_step=category)
            state = classify_page(observation)
            if state.kind is PageKind.HUMAN_INTERVENTION_REQUIRED:
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED,
                                     blocker=_BLOCKER.get(state.reason, Blocker.OTHER),
                                     page_or_step="authentication")
            if state.kind is PageKind.UNKNOWN:
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.OTHER,
                                     page_or_step="authentication")
            if state.kind is PageKind.APPLICATION:
                if request.task.status in {TaskStatus.LAUNCHING, TaskStatus.AUTHENTICATING}:
                    self.store.advance_state(request.task.id, TaskStatus.FILLING)
                stage = "controller"
                return self._current_page(request, observation)
            if request.task.status is not TaskStatus.LAUNCHING:
                # AUTHENTICATING survived a restart or uncertain browser outcome.
                # Only an explicit human Resume returns it to LAUNCHING.
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.LOGIN_REQUIRED,
                                     page_or_step="authentication")
            login_url = urlsplit(observation.location)
            if (not login_url.hostname or login_url.username or login_url.password or
                    login_url.scheme != "https"):
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.LOGIN_REQUIRED,
                                     page_or_step="authentication")
            account_id = login_url.hostname
            identity = self.credentials.identity(account_id or "")
            if identity is None:
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.LOGIN_REQUIRED,
                                     page_or_step="authentication")
            # Durable pre-action marker prevents a backend restart from
            # blindly repeating an uncertain credential attempt.
            self.store.advance_state(request.task.id, TaskStatus.AUTHENTICATING)
            browser = self.launcher.windows.browser_for(request.window_id)
            stage = "login"
            result = self.launcher.windows.run_browser(request.window_id,
                LoginOrchestrator(browser, self.credentials).attempt(observation, identity))
            stage = "login_verify"
            if result.status is LoginStatus.AUTHENTICATED:
                self.store.advance_state(request.task.id, TaskStatus.FILLING)
                stage = "controller"
                return self._current_page(request, result.observation)
            blocker = (_BLOCKER.get(result.reason, Blocker.LOGIN_REQUIRED)
                       if result.status is LoginStatus.HUMAN_INTERVENTION_REQUIRED
                       else Blocker.LOGIN_REQUIRED)
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=blocker,
                                 page_or_step="authentication")
        except SnapshotAccessChallenge:
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.CAPTCHA_REQUIRED,
                                 page_or_step="authentication")
        except Exception as exc:
            # Browser/credential exceptions may contain sensitive tool arguments.
            diagnostic = safe_failure(stage, exc)
            if is_recoverable_page_timeout(diagnostic):
                return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.BROWSER_TIMEOUT,
                                     failure_diagnostic=diagnostic,
                                     page_or_step=request.task.current_page_or_step)
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR,
                                 failure_diagnostic=diagnostic)
