"""Gated controller walk through real Playwright MCP; never clicks Submit."""

import os
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from jobagent.controller import ApplicationController, ControllerStop
from jobagent.domain import ApplicationOutcome, ApplicationSession, NavigationKind, SubmissionPermission
from jobagent.mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from jobagent.snapshot import SnapshotNormalizer
from tests.test_mcp_integration import QuietHandler, FIXTURE_DIR
from tests.test_resolution import profile_data


@unittest.skipUnless(os.environ.get("JOB_AGENT_RUN_MCP_BROWSER_TESTS") == "1",
                     "set JOB_AGENT_RUN_MCP_BROWSER_TESTS=1 for real MCP controller test")
class RealControllerFixtureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mcp_output = tempfile.TemporaryDirectory(prefix="jobagent-controller-mcp-")
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

    async def test_controller_reaches_final_review_without_submit(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file(), "set JOB_AGENT_PLAYWRIGHT_MCP_CLI to pinned CLI path")
        command = MCPServerCommand(
            command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name),
        )
        data = profile_data()
        data["work_history"] = [{"company": "Analytical Engine", "is_current": True}]
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
        url = f"http://127.0.0.1:{self.server.server_port}/multi_step_application.html"
        session = ApplicationSession("fixture-controller", url)
        normalizer = SnapshotNormalizer(navigation_kinds={"Continue": NavigationKind.ADVANCE,
                                                         "Back": NavigationKind.BACK})
        async with PlaywrightMCPAdapter(command, normalizer=normalizer) as browser:
            result = await ApplicationController(browser, resolver).run(session)
            self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW, result.reason)
            self.assertEqual(result.session.outcome, ApplicationOutcome.READY_FOR_REVIEW)
            self.assertEqual(result.session.submission_permission, SubmissionPermission.LOCKED)
            self.assertEqual([step.to_heading for step in result.session.step_history],
                             ["Employment", "Contact confirmation", "Review application"])
            self.assertTrue(any(q.label == "Current employer" for obs in result.session.observations
                                for q in obs.questions))
            email_answers = [answer for answer in result.session.resolved_questions.values()
                             if answer.semantic_key == "personal.email"]
            self.assertEqual(len(email_answers), 2)
            self.assertEqual({answer.value for answer in email_answers}, {"ada@example.test"})
            self.assertEqual(len([record for record in result.session.action_history
                                  if record.kind == "advance"]), 3)
            self.assertFalse(any(record.kind == "submit" for record in result.session.action_history))
            self.assertTrue(any(c.kind is NavigationKind.SUBMIT
                                for c in result.session.current_observation.navigation_controls))
            final = await browser.observe()
            self.assertTrue(final.review_like)
            self.assertEqual(final.heading, "Review application")


if __name__ == "__main__":
    unittest.main()
