"""One gated real MCP/browser walkthrough. Never activates Submit."""

import os
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jobagent.domain import (
    ActionStatus, Advance, Answer, AnswerScope, AnswerSource, ApplicationSession,
    ChooseOption, FillText, NavigationKind, SubmissionPermission, semantic_fingerprint,
)
from jobagent.mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from jobagent.snapshot import SnapshotNormalizer


FIXTURE_DIR = Path(__file__).parent / "fixtures"
RUN_BROWSER = os.environ.get("JOB_AGENT_RUN_MCP_BROWSER_TESTS") == "1"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(RUN_BROWSER, "set JOB_AGENT_RUN_MCP_BROWSER_TESTS=1 for real MCP browser test")
class RealMCPFixtureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mcp_output = tempfile.TemporaryDirectory(prefix="jobagent-mcp-")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          partial(QuietHandler, directory=str(FIXTURE_DIR)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.mcp_output.cleanup()
        self.assertFalse(self.thread.is_alive(), "fixture HTTP server did not stop")

    async def test_full_fixture_reaches_review_without_submission(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file(), "set JOB_AGENT_PLAYWRIGHT_MCP_CLI to pinned CLI path")
        command = MCPServerCommand(
            command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name),
        )
        aliases = {
            "first name": "personal.first_name", "last name": "personal.last_name",
            "email": "personal.email", "confirm email": "personal.email",
            "are you currently employed?": "employment.current",
            "current employer": "employment.current_employer",
        }
        session = ApplicationSession("fixture-application", "http://local/job")

        navigation = {"Continue": NavigationKind.ADVANCE, "Back": NavigationKind.BACK}
        async with PlaywrightMCPAdapter(command,
                                       normalizer=SnapshotNormalizer(aliases, navigation)) as browser:
            self.assertTrue({"browser_navigate", "browser_snapshot", "browser_type",
                             "browser_click", "browser_close"} <= set(browser.discovered_tools))
            url = f"http://127.0.0.1:{self.server.server_port}/multi_step_application.html"
            observation = await browser.navigate(url)
            session.record_observation(observation)
            self.assertEqual(observation.heading, "Basic information")
            self.assertEqual(observation.progress_text, "Step 1 of 4: Basic information")
            self.assertEqual({q.label for q in observation.questions},
                             {"First name", "Last name", "Email"})
            self.assertTrue(any(c.kind is NavigationKind.ADVANCE for c in observation.navigation_controls))

            async def fill(label, value, scope=AnswerScope.GLOBAL):
                nonlocal observation
                question = next(q for q in observation.questions if q.label == label)
                answer = Answer(question.semantic_key, value, AnswerSource.CANDIDATE_PROFILE,
                                scope, application_id=session.application_id if scope is AnswerScope.APPLICATION else None)
                result = await browser.fill_text(
                    FillText(question.target_ref, observation.observation_id, answer), session)
                observation = result.observation
                session.record_observation(observation)
                return result

            async def advance():
                nonlocal observation
                control = next(c for c in observation.navigation_controls if c.kind is NavigationKind.ADVANCE)
                result = await browser.activate_navigation(
                    Advance(control.target_ref, observation.observation_id), session)
                observation = result.observation
                session.record_observation(observation)
                return result

            await fill("First name", "Ada")
            await fill("Last name", "Lovelace")
            await fill("Email", "ada@example.test")
            self.assertEqual(next(q for q in observation.questions if q.label == "Email").current_value,
                             "ada@example.test")
            result = await advance()
            self.assertEqual(result.outcome.status, ActionStatus.STATE_CHANGED)
            self.assertEqual(observation.heading, "Employment")
            self.assertEqual(observation.progress_text, "Step 2 of 4: Employment")

            employment = next(q for q in observation.questions if q.label == "Are you currently employed?")
            answer = Answer(employment.semantic_key, "Yes", AnswerSource.CANDIDATE_PROFILE,
                            AnswerScope.APPLICATION, application_id=session.application_id)
            result = await browser.choose_option(
                ChooseOption(employment.target_ref, observation.observation_id, answer), session)
            observation = result.observation
            session.record_observation(observation)
            self.assertIn("Current employer", {q.label for q in observation.questions})
            before_validation = semantic_fingerprint(observation)

            result = await advance()
            self.assertEqual(result.outcome.status, ActionStatus.VALIDATION_BLOCKED)
            self.assertEqual(observation.heading, "Employment")
            self.assertEqual(observation.progress_text, "Step 2 of 4: Employment")
            self.assertTrue(any("Current employer" in message for message in observation.validation_messages))
            self.assertNotEqual(before_validation, semantic_fingerprint(observation))

            await fill("Current employer", "Analytical Engine", AnswerScope.APPLICATION)
            result = await advance()
            self.assertEqual(result.outcome.status, ActionStatus.STATE_CHANGED)
            self.assertEqual(observation.heading, "Contact confirmation")
            self.assertEqual(observation.progress_text, "Step 3 of 4: Contact confirmation")

            repeated = next(q for q in observation.questions if q.label == "Confirm email")
            self.assertEqual(repeated.semantic_key, "personal.email")
            self.assertIsNotNone(repeated.target_ref)
            await fill("Confirm email", "ada@example.test")
            result = await advance()
            self.assertEqual(result.outcome.status, ActionStatus.STATE_CHANGED)
            self.assertEqual(observation.heading, "Review application")
            self.assertTrue(observation.review_like)
            submit = next(c for c in observation.navigation_controls if c.kind is NavigationKind.SUBMIT)
            self.assertEqual(submit.label, "Submit application")
            self.assertEqual(session.submission_permission, SubmissionPermission.LOCKED)
            with self.assertRaises(PermissionError):
                await browser.activate_navigation(
                    Advance(submit.target_ref, observation.observation_id), session)
            final_check = await browser.observe()
            self.assertTrue(final_check.review_like)
            self.assertEqual(final_check.heading, "Review application")
            self.assertFalse(final_check.validation_messages)


if __name__ == "__main__":
    unittest.main()
