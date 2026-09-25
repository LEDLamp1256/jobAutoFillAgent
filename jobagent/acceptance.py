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

from .controller import ApplicationController, ControllerStop, step_signature
from .domain import ApplicationObservation, ApplicationSession, NavigationKind
from .mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from .resolution import CandidateProfile, DeterministicAnswerResolver, ProfileError
from .semantic_llm import GroundedSemanticResolver, OllamaConfig, OllamaSemanticMapper
from .snapshot import SnapshotAccessChallenge, SnapshotEmpty, SnapshotNormalizer


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


def authentication_required(observation: ApplicationObservation) -> bool:
    heading = " ".join((observation.heading or "").casefold().split())
    labels = " ".join(question.label.casefold() for question in observation.questions)
    path = urlsplit(observation.location).path.casefold()
    return (any(term in heading for term in ("sign in", "log in", "login", "create account",
                                             "verify your email", "verification code")) or
            ("password" in labels and any(term in path for term in ("login", "signin", "account"))))


def _location_pattern(url: str) -> str:
    parsed = urlsplit(url)
    path = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b", ":id", parsed.path, flags=re.I)
    return f"{parsed.hostname or '?'}{path[:160]}"


def observation_summary(observation: ApplicationObservation) -> dict[str, object]:
    signature = hashlib.sha256(repr(step_signature(observation)).encode()).hexdigest()[:12]
    return {
        "location_pattern": _location_pattern(observation.location),
        "heading": observation.heading,
        "progress": observation.progress_text,
        "step_signature": signature,
        "question_count": len(observation.questions),
        "questions": [(q.label[:120], q.control_type.value, q.required) for q in observation.questions],
        "controls": [(c.label[:120], c.kind.value) for c in observation.navigation_controls],
        "validation_count": len(observation.validation_messages),
        "review_like": observation.review_like,
    }


async def run_acceptance(url: str, profile: CandidateProfile | None, cli: Path, stage: AcceptanceStage,
                         *, hold: bool = False) -> str:
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
                if hold:
                    await asyncio.to_thread(input, "Inspect the blocked page, then press Enter to close: ")
                return "ACCESS_CHALLENGE"
            print(f"initial_observation={observation_summary(initial)}")
            if authentication_required(initial):
                if not hold:
                    print("classification=AUTH_REQUIRED; manual login is needed")
                    return "AUTH_REQUIRED"
                await asyncio.to_thread(input, "Complete authentication in the visible browser, then press Enter to re-observe: ")
                initial = await browser.observe()
                print(f"after_owner_action={observation_summary(initial)}")
                if authentication_required(initial):
                    print("classification=AUTH_REQUIRED")
                    return "AUTH_REQUIRED"
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
        return 2
    return 2 if classification in {"FAILED", "OBSERVATION_FAILURE", "ACCESS_CHALLENGE"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
