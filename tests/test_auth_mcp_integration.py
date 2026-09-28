"""Gated local MCP login contract with synthetic credentials only."""

import os
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from jobagent.authentication import LoginIdentity, LoginOrchestrator, LoginStatus
from jobagent.mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
from tests.test_authentication import FakeCredentials, SECRET
from tests.test_mcp_integration import FIXTURE_DIR, QuietHandler


@unittest.skipUnless(os.environ.get("JOB_AGENT_RUN_MCP_BROWSER_TESTS") == "1",
                     "set JOB_AGENT_RUN_MCP_BROWSER_TESTS=1 for real MCP login fixture")
class RealLoginFixtureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory(prefix="jobagent-auth-mcp-")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          partial(QuietHandler, directory=str(FIXTURE_DIR)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.output.cleanup()
        self.assertFalse(self.thread.is_alive())

    async def test_synthetic_login_uses_fresh_mcp_observations(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(shutil.which("node") or "node",
                                   (cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
                                   Path(self.output.name))
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/login_handoff.html"
            initial = await browser.navigate(url)
            result = await LoginOrchestrator(browser, FakeCredentials()).attempt(
                initial, LoginIdentity("synthetic-account", "user@example.test"))
            self.assertEqual(result.status, LoginStatus.AUTHENTICATED)
            self.assertEqual(result.observation.heading, "Personal Information")
            self.assertNotIn(SECRET, repr(initial))
            self.assertNotIn(SECRET, repr(result))


if __name__ == "__main__":
    unittest.main()
