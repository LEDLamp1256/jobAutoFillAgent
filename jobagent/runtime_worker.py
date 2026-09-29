"""V2-10 launch/login followed by one bounded current-page fill pass."""

from __future__ import annotations

import json
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
from .domain import AnswerSource, ApplicationSession, ControlType
from .persistence import BatchStore
from .resolution import CandidateProfile, DeterministicAnswerResolver, ProfileError
from .snapshot import SnapshotAccessChallenge


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
        browser = self.launcher.windows.browser_for(request.window_id)
        session = ApplicationSession(request.task.id, observation.location)
        result = self.launcher.windows.run_browser(request.window_id,
            ApplicationController(browser, DeterministicAnswerResolver(self._profile())).run(
                session, allow_advance=False, initial_observation=observation,
                current_page_only=True))
        if result.stop is ControllerStop.FAILED:
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR)
        final = session.current_observation
        if final is None:
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR)
        page = final.progress_text or final.heading or "application page"
        audits = tuple(FieldAudit(
            field.question.label, page, field.semantic_key, field.action,
            Provenance.VERIFIED_PROFILE if field.source is AnswerSource.CANDIDATE_PROFILE
            else Provenance.DETERMINISTIC, Verification.VERIFIED)
            for field in result.fields if field.verified)
        issues = tuple(ReviewIssue(field.question.label, field.reason or "needs_review",
                                   page, field.semantic_key)
                       for field in result.fields if field.needs_review)
        if result.stop is ControllerStop.STOPPED_BEFORE_ADVANCE:
            boundary_reason = ("automatic_advancement_outside_v2_11" if
                               result.reason.startswith("page complete;") else
                               "ambiguous_navigation")
            issues = (*issues, ReviewIssue("Current page", boundary_reason, page))
        for audit in audits:
            self.store.add_report_entry_once(request.task.id, page_or_step=audit.page_or_step,
                                             visible_label=audit.visible_label,
                                             semantic_key=audit.semantic_key, kind=ReportKind.FIELD,
                                             provenance=audit.provenance, action=audit.action,
                                             verification=audit.verification,
                                             review_state=ReviewState.NOT_REQUIRED)
        for issue in issues:
            self.store.add_report_entry_once(request.task.id, page_or_step=issue.page_or_step,
                                             visible_label=issue.visible_label,
                                             semantic_key=issue.semantic_key, kind=ReportKind.FIELD,
                                             provenance=Provenance.UNRESOLVED, action="deferred",
                                             verification=Verification.NOT_ATTEMPTED,
                                             review_state=ReviewState.PENDING, reason=issue.reason)
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
        if result.stop is ControllerStop.NEEDS_REVIEW:
            blocker = (Blocker.UNSUPPORTED_CONTROL if issues and all(
                issue.reason == "unsupported_control" for issue in issues) else Blocker.NEEDS_ANSWER)
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues=issues,
                                 blocker=blocker, page_or_step=page, audits=audits,
                                 report_persisted=True)
        return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues=issues,
                             blocker=Blocker.OTHER, page_or_step=page, audits=audits,
                             report_persisted=True)

    def work_until_yield(self, request: WorkRequest) -> WorkerOutcome:
        try:
            listing = self.store.get_listing(request.task.job_listing_id)
            observation = self.launcher.open(request.task, listing, request.window_id)
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
            result = self.launcher.windows.run_browser(request.window_id,
                LoginOrchestrator(browser, self.credentials).attempt(observation, identity))
            if result.status is LoginStatus.AUTHENTICATED:
                self.store.advance_state(request.task.id, TaskStatus.FILLING)
                return self._current_page(request, result.observation)
            blocker = (_BLOCKER.get(result.reason, Blocker.LOGIN_REQUIRED)
                       if result.status is LoginStatus.HUMAN_INTERVENTION_REQUIRED
                       else Blocker.LOGIN_REQUIRED)
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=blocker,
                                 page_or_step="authentication")
        except SnapshotAccessChallenge:
            return WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.CAPTCHA_REQUIRED,
                                 page_or_step="authentication")
        except Exception:
            # Browser/credential exceptions may contain sensitive tool arguments.
            return WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.BROWSER_ERROR)
