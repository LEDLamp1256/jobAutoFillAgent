"""Opt-in real MCP gate for the local one-page fixture."""

import os
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jobagent.controller import ApplicationController, ControllerStop
from jobagent.domain import ApplicationSession
from jobagent.mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from tests.test_resolution import profile_data


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get("JOB_AGENT_RUN_MCP_BROWSER_TESTS") == "1",
                     "set JOB_AGENT_RUN_MCP_BROWSER_TESTS=1 for real MCP browser test")
class OnePageMCPTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jobagent-one-page-")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
            QuietHandler, directory=str(Path(__file__).parent / "fixtures")))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    async def test_fields_markers_and_zero_submit_interactions(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(shutil.which("node") or "node",
            (cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome",
             "--codegen", "none"), Path(self.temp.name))
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/one_page_application.html"
            initial = await browser.navigate(url)
            session = ApplicationSession("fixture", url)
            result = await ApplicationController(browser, DeterministicAnswerResolver(
                CandidateProfile.from_mapping(profile_data()))).run(
                    session, initial_observation=initial, current_page_only=True)
            self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
            final = session.current_observation
            active_before = await browser._page_marker("() => document.activeElement?.id || ''")
            for field in result.fields:
                if field.question.target_ref:
                    await browser.annotate_field(field.question.target_ref, final.observation_id,
                                                 "verified" if field.verified else
                                                 "needs-review" if field.needs_review else "clear")
            choice = next(f for f in result.fields if f.question.control_type.value == "choice")
            await browser.annotate_field(choice.question.target_ref, final.observation_id, "verified")
            unknown = next(f for f in result.fields if f.question.label == "Fixture clearance code")
            await browser.annotate_field(unknown.question.target_ref, final.observation_id, "clear")
            cleared = await browser._page_marker(
                "() => document.querySelector('#unknown').hasAttribute('data-jobagent-review-state')")
            self.assertIs(cleared, False)
            await browser.annotate_field(unknown.question.target_ref, final.observation_id, "needs-review")
            evidence = await browser._page_marker("""() => ({
              first: document.querySelector('#first-name').value,
              last: document.querySelector('#last-name').value,
              email: document.querySelector('#email').value,
              authorization: document.querySelector('input[name=authorization]:checked')?.value || '',
              unknown: document.querySelector('#unknown').value,
              markers: ['#first-name','#last-name','#email','#unknown'].map(s =>
                document.querySelector(s).getAttribute('data-jobagent-review-state')),
              choiceMarker: document.querySelector('fieldset').getAttribute('data-jobagent-review-state'),
              styleCount: document.querySelectorAll('#jobagent-review-style').length,
              firstOutline: getComputedStyle(document.querySelector('#first-name')).outlineStyle,
              unknownOutline: getComputedStyle(document.querySelector('#unknown')).outlineStyle,
              originalStyle: document.querySelector('#first-name').getAttribute('style'),
              active: document.activeElement?.id || '',
              count: document.querySelector('#submit-count').value
            })""")
            self.assertEqual((evidence["first"], evidence["last"], evidence["email"]),
                             ("Ada", "Lovelace", "ada@example.test"))
            self.assertEqual(evidence["authorization"], "Yes")
            self.assertEqual(evidence["unknown"], "")
            self.assertEqual(evidence["markers"],
                             ["verified", "verified", "verified", "needs-review"])
            self.assertEqual(evidence["choiceMarker"], "verified")
            self.assertEqual(evidence["styleCount"], 1)
            self.assertEqual(evidence["firstOutline"], "solid")
            self.assertEqual(evidence["unknownOutline"], "solid")
            self.assertEqual(evidence["originalStyle"], "border: 1px dotted navy")
            self.assertEqual(evidence["active"], active_before)
            self.assertEqual(evidence["count"], "0")


if __name__ == "__main__":
    unittest.main()
