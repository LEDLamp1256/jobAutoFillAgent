"""Owner-gated, headed observation and bounded no-submit ATS acceptance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import shutil
import sys
import tempfile
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from .authentication import (
    CredentialProvider, InterventionReason, LoginIdentity, LoginOrchestrator,
    LoginStatus, PageKind, classify_page, resume_after_human,
)
from .controller import ApplicationController, ControllerStop, step_signature
from .domain import ApplicationObservation, ApplicationSession, ControlType, NavigationKind
from .mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from .resolution import CandidateProfile, DeterministicAnswerResolver, ProfileError
from .semantic_llm import GroundedSemanticResolver, OllamaConfig, OllamaSemanticMapper
from .snapshot import SnapshotAccessChallenge, SnapshotEmpty, SnapshotFormatError, SnapshotNormalizer


GATE = "JOB_AGENT_RUN_REAL_ATS_ACCEPTANCE"
CLI_ENV = "JOB_AGENT_PLAYWRIGHT_MCP_CLI"
URL_ENV = "JOB_AGENT_ACCEPTANCE_URL"


class AcceptanceStage(str, Enum):
    OBSERVE = "observe"
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"
    TRAVERSE = "traverse"


def validate_inputs(url: str | None, config: str | None, cli: str | None,
                    stage: AcceptanceStage) -> tuple[str, CandidateProfile | None, Path]:
    if os.environ.get(GATE) != "1":
        raise ValueError(f"set {GATE}=1 to enable real-site acceptance")
    if not url or not cli:
        raise ValueError(f"URL and {CLI_ENV} are required")
    if stage is not AcceptanceStage.OBSERVE and not config:
        raise ValueError("--config is required before filling or traversal")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" and not
            (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})):
        raise ValueError("acceptance URL must use HTTPS or local loopback HTTP")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("acceptance URL must have a host and no embedded credentials")
    profile = CandidateProfile.from_json(config) if config else None
    cli_path = Path(cli).expanduser()
    if not cli_path.is_file():
        raise ValueError("Playwright MCP CLI path does not exist")
    if not shutil.which("node"):
        raise ValueError("Node is unavailable")
    return url, profile, cli_path


def server_command(cli: Path, cwd: Path) -> MCPServerCommand:
    """Headed Chrome in an isolated context; never the owner's normal profile."""
    return MCPServerCommand(shutil.which("node") or "node",
                            (str(cli), "--isolated", "--no-webmcp", "--browser", "chrome",
                             "--codegen", "none"), cwd)


def _location_pattern(url: str) -> str:
    parsed = urlsplit(url)
    path = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b", ":id", parsed.path, flags=re.I)
    return f"{parsed.hostname or '?'}{path[:160]}"


def observation_summary(observation: ApplicationObservation) -> dict[str, object]:
    signature = hashlib.sha256(repr(step_signature(observation)).encode()).hexdigest()[:12]
    return {
        "location_pattern": _location_pattern(observation.location),
        "heading": _safe_observation_label(observation.heading) if observation.heading else None,
        "progress": (_safe_observation_label(observation.progress_text)
                     if observation.progress_text else None),
        "step_signature": signature,
        "question_count": len(observation.questions),
        "questions": [(_safe_observation_label(q.label), q.control_type.value, q.required)
                      for q in observation.questions[:12]],
        "questions_truncated": len(observation.questions) > 12,
        "controls": [(_safe_observation_label(c.label), c.kind.value)
                     for c in observation.navigation_controls[:12]],
        "controls_truncated": len(observation.navigation_controls) > 12,
        "validation_count": len(observation.validation_messages),
        "review_like": observation.review_like,
    }


def _safe_observation_label(label: str) -> str:
    """Keep a short form label while removing common embedded candidate values."""
    label = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email]", label)
    label = re.sub(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)", "[phone]", label)
    label = re.sub(r"\b\d+\s+[\w .'-]{1,60}\s+(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd)\b",
                   "[address]", label, flags=re.I)
    label = re.sub(r"https?://\S+|\b[0-9a-f]{12,}\b|\b\d{5,}\b", "[value]", label, flags=re.I)
    if re.search(r"\b(password|passphrase|secret|token|cookie)\s*:", label, re.I):
        return "[sensitive-label]"
    return label[:100]


def post_resume_diagnostic(before: ApplicationObservation, fresh: ApplicationObservation,
                           previous_kind: PageKind, previous_reason: InterventionReason | None,
                           fresh_kind: PageKind, fresh_reason: InterventionReason | None,
                           comparison_reason: InterventionReason,
                           same_state_stop: bool) -> dict[str, object]:
    """Bounded evidence from normalized data; no field values or raw snapshot."""
    heading = (fresh.heading or "").casefold()
    identity_labels = {"email", "email address", "username", "user name"}
    sign_in_labels = {"sign in", "log in", "login"}
    return {
        "phase": "after_owner_resume",
        "location_pattern": _location_pattern(fresh.location),
        "fresh_observation_id_differs": fresh.observation_id != before.observation_id,
        "heading": _safe_observation_label(fresh.heading or ""),
        "progress": _safe_observation_label(fresh.progress_text or ""),
        "questions": len(fresh.questions),
        "question_labels": tuple(_safe_observation_label(q.label) for q in fresh.questions[:12]),
        "question_labels_truncated": len(fresh.questions) > 12,
        "question_types": tuple(sorted({kind.value: sum(q.control_type is kind for q in fresh.questions)
                                        for kind in ControlType if any(q.control_type is kind for q in fresh.questions)}.items())),
        "required_true": sum(q.required is True for q in fresh.questions),
        "navigation_controls": len(fresh.navigation_controls),
        "navigation_labels": tuple(_safe_observation_label(c.label)
                                   for c in fresh.navigation_controls[:12]),
        "navigation_labels_truncated": len(fresh.navigation_controls) > 12,
        "validation_count": len(fresh.validation_messages),
        "review_like": fresh.review_like,
        "login_heading": any(term in heading for term in ("sign in", "log in", "login")),
        "secret_controls": sum(q.control_type is ControlType.SECRET for q in fresh.questions),
        "login_identity_controls": sum(q.control_type is ControlType.TEXT and
                                       q.label.strip().casefold() in identity_labels
                                       for q in fresh.questions),
        "sign_in_controls": sum(c.label.strip().casefold() in sign_in_labels
                                for c in fresh.navigation_controls),
        "previous_classification": (previous_kind.value,
                                    previous_reason.value if previous_reason else None),
        "fresh_classification": (fresh_kind.value, fresh_reason.value if fresh_reason else None),
        "comparison_reason": comparison_reason.value,
        "same_state_stop": same_state_stop,
    }


async def run_acceptance(url: str, profile: CandidateProfile | None, cli: Path, stage: AcceptanceStage,
                         *, hold: bool = False, login_identity: LoginIdentity | None = None,
                         credentials: CredentialProvider | None = None) -> str:
    app_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    # The server's temporary working directory disappears after this invocation.
    with tempfile.TemporaryDirectory(prefix="jobagent-real-ats-") as directory:
        normalizer = SnapshotNormalizer(navigation_kinds={
            "Next": NavigationKind.ADVANCE, "Continue": NavigationKind.ADVANCE,
            "Save and Continue": NavigationKind.ADVANCE,
            "Save & Continue": NavigationKind.ADVANCE, "Back": NavigationKind.BACK,
        })
        async with PlaywrightMCPAdapter(server_command(cli, Path(directory)),
                                       normalizer=normalizer) as browser:
            # Every invocation begins with an observation-only preflight.
            handoffs = 0
            try:
                try:
                    initial = await browser.navigate(url)
                except SnapshotEmpty:
                    # One bounded observation retry allows a loading shell to settle.
                    # It never clicks, reloads, or interacts with a challenge.
                    await asyncio.sleep(1)
                    initial = await browser.observe()
            except SnapshotAccessChallenge as exc:
                print(f"classification=ACCESS_CHALLENGE; reason={exc}")
                if not hold:
                    return "HUMAN_INTERVENTION_REQUIRED"
                handoffs += 1
                try:
                    initial = await resume_after_human(browser)
                except SnapshotAccessChallenge:
                    print("classification=HUMAN_INTERVENTION_REQUIRED; reason=access_challenge")
                    return "HUMAN_INTERVENTION_REQUIRED"
            print(f"initial_observation={observation_summary(initial)}")
            login_attempted = False
            for _ in range(3):  # At most three distinct, explicitly resumed human states.
                state = classify_page(initial)
                if state.kind is PageKind.LOGIN and login_attempted:
                    print("classification=LOGIN_FAILED; repeated credential attempt is forbidden")
                    return "LOGIN_FAILED"
                if state.kind is PageKind.LOGIN and login_identity and credentials:
                    login_attempted = True
                    outcome = await LoginOrchestrator(browser, credentials).attempt(initial, login_identity)
                    print(f"login_status={outcome.status.value}")
                    if outcome.status is LoginStatus.AUTHENTICATED:
                        initial = outcome.observation
                        print(f"after_login={observation_summary(initial)}")
                        break
                    if outcome.status is LoginStatus.LOGIN_FAILED:
                        return "LOGIN_FAILED"
                    if outcome.status is LoginStatus.CREDENTIALS_UNAVAILABLE:
                        return "CREDENTIALS_UNAVAILABLE"
                    initial = outcome.observation
                    if outcome.status is LoginStatus.HUMAN_INTERVENTION_REQUIRED:
                        state = classify_page(initial)
                        if outcome.reason is InterventionReason.ACCESS_CHALLENGE:
                            state_reason = InterventionReason.ACCESS_CHALLENGE
                        else:
                            state_reason = outcome.reason
                    else:
                        state_reason = InterventionReason.HUMAN_JUDGMENT_REQUIRED
                else:
                    state_reason = state.reason or InterventionReason.HUMAN_JUDGMENT_REQUIRED
                if state.kind is PageKind.APPLICATION:
                    break
                if state.kind is PageKind.UNKNOWN:
                    if stage is AcceptanceStage.OBSERVE:
                        break
                    print("classification=UNKNOWN; no safe application or login state")
                    return "UNKNOWN"
                if not hold or handoffs >= 3:
                    print(f"classification=HUMAN_INTERVENTION_REQUIRED; reason={state_reason.value}")
                    return "HUMAN_INTERVENTION_REQUIRED"
                handoffs += 1
                print(f"classification=HUMAN_INTERVENTION_REQUIRED; reason={state_reason.value}")
                try:
                    fresh = await resume_after_human(browser)
                except SnapshotAccessChallenge:
                    print("classification=HUMAN_INTERVENTION_REQUIRED; reason=access_challenge")
                    return "HUMAN_INTERVENTION_REQUIRED"
                fresh_state = classify_page(fresh)
                same_state_stop = (state_reason is not InterventionReason.ACCESS_CHALLENGE and
                                   fresh_state.kind is state.kind and fresh_state.reason is state.reason)
                print(f"post_resume_observation={post_resume_diagnostic(initial, fresh, state.kind, state.reason, fresh_state.kind, fresh_state.reason, state_reason, same_state_stop)}")
                if fresh_state.kind is PageKind.UNKNOWN and not fresh.questions and not fresh.navigation_controls:
                    diagnostic_for = getattr(browser, "diagnostic_for", None)
                    diagnostic = diagnostic_for(fresh.observation_id) if callable(diagnostic_for) else None
                    if diagnostic is not None:
                        print(f"post_resume_control_diagnostic={{'roles': {diagnostic.roles!r}, "
                              f"'controls': {diagnostic.control_predicates!r}, "
                              f"'truncated': {diagnostic.truncated!r}}}")
                if same_state_stop:
                    print("classification=HUMAN_INTERVENTION_REQUIRED; state unchanged after resume")
                    return "HUMAN_INTERVENTION_REQUIRED"
                initial = fresh
                print(f"after_owner_action={observation_summary(initial)}")
            else:
                return "HUMAN_INTERVENTION_REQUIRED"
            if stage is AcceptanceStage.OBSERVE:
                classification = "FINAL_REVIEW" if initial.review_like else "OBSERVED"
                print(f"classification={classification}")
            else:
                if profile is None:
                    raise ValueError("candidate profile is required before filling or traversal")
                deterministic = DeterministicAnswerResolver(profile)
                resolver = (deterministic if stage is AcceptanceStage.DETERMINISTIC else
                            GroundedSemanticResolver(deterministic, OllamaSemanticMapper(
                                OllamaConfig(model="llama3.1:8b", timeout_seconds=60))))
                session = ApplicationSession(app_id, url)
                result = await ApplicationController(browser, resolver).run(
                    session, allow_advance=stage is AcceptanceStage.TRAVERSE,
                    initial_observation=initial)
                for observation in session.observations:
                    print(f"observation={observation_summary(observation)}")
                for trace in session.resolution_history:
                    print(f"resolution={trace.semantic_key or 'unknown'}:{trace.status}:"
                          f"{trace.mapping_source.value}:"
                          f"{trace.answer_source.value if trace.answer_source else 'none'}")
                print(f"stop={result.stop.value}; reason={result.reason}; "
                      f"actions={len(session.action_history)}; steps={len(session.step_history)}; "
                      f"submission_permission={session.submission_permission.value}")
                classification = "FINAL_REVIEW" if result.stop is ControllerStop.READY_FOR_REVIEW else result.stop.value.upper()
                print(f"classification={classification}")
            if hold:
                await asyncio.to_thread(input, "Inspect the visible browser, then press Enter to close this isolated session: ")
            return classification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get(URL_ENV))
    parser.add_argument("--config", help="required for filling and traversal")
    parser.add_argument("--stage", choices=[stage.value for stage in AcceptanceStage],
                        default=AcceptanceStage.OBSERVE.value)
    parser.add_argument("--hold", action="store_true", help="pause for owner inspection or manual login")
    args = parser.parse_args(argv)
    if args.hold and not sys.stdin.isatty():
        parser.error("--hold requires an interactive terminal")
    try:
        url, profile, cli = validate_inputs(args.url, args.config, os.environ.get(CLI_ENV),
                                            AcceptanceStage(args.stage))
    except (ValueError, ProfileError) as exc:
        parser.error(str(exc))
    try:
        classification = asyncio.run(run_acceptance(
            url, profile, cli, AcceptanceStage(args.stage), hold=args.hold))
    except Exception as exc:
        print(f"classification=OBSERVATION_FAILURE; error={type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(exc, SnapshotFormatError) and exc.diagnostic is not None:
            print(f"sanitized_snapshot_diagnostic={exc.diagnostic!r}", file=sys.stderr)
        return 2
    return 2 if classification in {"FAILED", "OBSERVATION_FAILURE", "ACCESS_CHALLENGE"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
