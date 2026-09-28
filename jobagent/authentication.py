"""Deterministic login and human-intervention boundaries, separate from applications."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from .domain import ApplicationObservation, ControlType
from .snapshot import SnapshotAccessChallenge


class InterventionReason(str, Enum):
    ACCESS_CHALLENGE = "access_challenge"
    MFA_REQUIRED = "mfa_required"
    SSO_REQUIRED = "sso_required"
    EMAIL_VERIFICATION_REQUIRED = "email_verification_required"
    HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"


class PageKind(str, Enum):
    APPLICATION = "application"
    LOGIN = "login"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginTargets:
    observation_id: str
    identity_ref: str
    password_ref: str
    sign_in_ref: str


@dataclass(frozen=True)
class PageState:
    kind: PageKind
    reason: InterventionReason | None = None
    login: LoginTargets | None = None


def classify_page(observation: ApplicationObservation) -> PageState:
    """Use visible roles and labels; uncertainty never becomes a login action."""
    heading = (observation.heading or "").casefold()
    labels = " ".join(q.label.casefold() for q in observation.questions)
    controls = " ".join(c.label.casefold() for c in observation.navigation_controls)
    combined = " ".join((heading, labels, controls))
    if any(term in combined for term in ("captcha", "verify you are human", "unusual traffic",
                                         "i'm not a robot")):
        return PageState(PageKind.HUMAN_INTERVENTION_REQUIRED, InterventionReason.ACCESS_CHALLENGE)
    if "verify your email" in combined or "email verification" in combined:
        return PageState(PageKind.HUMAN_INTERVENTION_REQUIRED,
                         InterventionReason.EMAIL_VERIFICATION_REQUIRED)
    if any(term in combined for term in ("mfa", "two-factor", "two factor", "authenticator",
                                         "one-time code", "verification code", "security code",
                                         "push approval", "device confirmation")):
        return PageState(PageKind.HUMAN_INTERVENTION_REQUIRED, InterventionReason.MFA_REQUIRED)
    if any(term in controls for term in ("continue with google", "continue with microsoft",
                                         "continue with okta", "single sign-on", "sign in with sso")):
        return PageState(PageKind.HUMAN_INTERVENTION_REQUIRED, InterventionReason.SSO_REQUIRED)
    login_heading = any(term in heading for term in ("sign in", "log in", "login"))
    passwords = [q for q in observation.questions if q.control_type is ControlType.SECRET and q.target_ref]
    identities = [q for q in observation.questions if q.control_type is ControlType.TEXT and q.target_ref
                  and q.label.strip().casefold() in {"email", "email address", "username", "user name"}]
    sign_ins = [c for c in observation.navigation_controls if c.target_ref and
                c.label.strip().casefold() in {"sign in", "log in", "login"}]
    if login_heading or passwords or sign_ins:
        if (login_heading and len(passwords) == len(identities) == len(sign_ins) == 1 and
                not observation.review_like and not observation.validation_messages):
            return PageState(PageKind.LOGIN, login=LoginTargets(
                observation.observation_id, identities[0].target_ref, passwords[0].target_ref,
                sign_ins[0].target_ref))
        return PageState(PageKind.HUMAN_INTERVENTION_REQUIRED,
                         InterventionReason.HUMAN_JUDGMENT_REQUIRED)
    if not observation.heading or (not observation.questions and not observation.navigation_controls):
        return PageState(PageKind.UNKNOWN)
    return PageState(PageKind.APPLICATION)


@dataclass(frozen=True)
class LoginIdentity:
    """Non-secret account metadata supplied by the runtime, not candidate facts."""

    account_id: str
    username: str


class CredentialProvider(Protocol):
    async def get_password(self, account_id: str) -> str | None: ...


class AuthenticationPort(Protocol):
    async def fill_login_identity(self, target_ref: str, observation_id: str,
                                  username: str) -> ApplicationObservation: ...
    async def fill_login_password(self, target_ref: str, observation_id: str,
                                  password: str) -> ApplicationObservation: ...
    async def activate_login(self, target_ref: str, observation_id: str) -> ApplicationObservation: ...
    async def observe(self) -> ApplicationObservation: ...


class LoginStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    LOGIN_FAILED = "login_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginResult:
    status: LoginStatus
    observation: ApplicationObservation
    reason: InterventionReason | None = None


class LoginOrchestrator:
    """One credential attempt; no retries, application session, or model calls."""

    def __init__(self, browser: AuthenticationPort, credentials: CredentialProvider):
        self.browser = browser
        self.credentials = credentials

    async def attempt(self, observation: ApplicationObservation, identity: LoginIdentity) -> LoginResult:
        state = classify_page(observation)
        if state.kind is PageKind.HUMAN_INTERVENTION_REQUIRED:
            return LoginResult(LoginStatus.HUMAN_INTERVENTION_REQUIRED, observation, state.reason)
        if state.kind is not PageKind.LOGIN or state.login is None:
            return LoginResult(LoginStatus.UNKNOWN, observation)
        if not identity.account_id.strip() or not identity.username.strip():
            return LoginResult(LoginStatus.CREDENTIALS_UNAVAILABLE, observation)
        password = await self.credentials.get_password(identity.account_id)
        if not password:
            return LoginResult(LoginStatus.CREDENTIALS_UNAVAILABLE, observation)
        try:
            # Reclassify after every action: no pre-action browser reference survives.
            observation = await self.browser.fill_login_identity(
                state.login.identity_ref, observation.observation_id, identity.username)
            state = classify_page(observation)
            if state.kind is not PageKind.LOGIN or state.login is None:
                return self._post_action(state, observation)
            observation = await self.browser.fill_login_password(
                state.login.password_ref, observation.observation_id, password)
            state = classify_page(observation)
            if state.kind is not PageKind.LOGIN or state.login is None:
                return self._post_action(state, observation)
            observation = await self.browser.activate_login(
                state.login.sign_in_ref, observation.observation_id)
        except SnapshotAccessChallenge:
            return LoginResult(LoginStatus.HUMAN_INTERVENTION_REQUIRED, observation,
                               InterventionReason.ACCESS_CHALLENGE)
        except Exception:
            # MCP errors can contain tool arguments; never copy one into diagnostics.
            return LoginResult(LoginStatus.LOGIN_FAILED, observation)
        return self._post_action(classify_page(observation), observation)

    @staticmethod
    def _post_action(state: PageState, observation: ApplicationObservation) -> LoginResult:
        if observation.validation_messages and any(
                term in (observation.heading or "").casefold() for term in ("sign in", "log in", "login")):
            return LoginResult(LoginStatus.LOGIN_FAILED, observation)
        if state.kind is PageKind.HUMAN_INTERVENTION_REQUIRED:
            return LoginResult(LoginStatus.HUMAN_INTERVENTION_REQUIRED, observation, state.reason)
        if state.kind is PageKind.LOGIN:
            return LoginResult(LoginStatus.LOGIN_FAILED, observation)
        if state.kind is PageKind.APPLICATION:
            return LoginResult(LoginStatus.AUTHENTICATED, observation)
        return LoginResult(LoginStatus.UNKNOWN, observation)


async def resume_after_human(browser: AuthenticationPort,
                             prompt: Callable[[str], object] | None = None) -> ApplicationObservation:
    """Only explicit owner input triggers a fresh observation; no old ref is used."""
    await asyncio.to_thread(prompt or input,
                            "Complete the action in the visible browser, then press Enter to re-observe: ")
    return await browser.observe()
