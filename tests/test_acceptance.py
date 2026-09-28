"""Acceptance runner contracts without an external site or headed browser."""

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jobagent.acceptance import (
    AcceptanceStage, observation_summary, run_acceptance,
    server_command, validate_inputs,
)
from jobagent.authentication import LoginIdentity, PageKind, classify_page
from jobagent.domain import (ApplicationObservation, ControlType, NavigationControl,
                             NavigationKind, QuestionObservation)
from jobagent.resolution import CandidateProfile, ProfileError
from jobagent.snapshot import SnapshotAccessChallenge, SnapshotEmpty, snapshot_diagnostic
from tests.test_resolution import profile_data
from tests.test_authentication import FakeAuthBrowser, FakeCredentials


class AcceptanceConfigTests(unittest.TestCase):
    def test_gate_and_profile_validation_precede_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "candidate.json"
            cli = Path(directory) / "cli.js"
            config.write_text(json.dumps(profile_data()), encoding="utf-8")
            cli.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {"JOB_AGENT_RUN_REAL_ATS_ACCEPTANCE": "0"}):
                with self.assertRaisesRegex(ValueError, "gate|JOB_AGENT_RUN_REAL_ATS_ACCEPTANCE"):
                    validate_inputs("https://example.test/apply", str(config), str(cli),
                                    AcceptanceStage.DETERMINISTIC)
            with patch.dict(os.environ, {"JOB_AGENT_RUN_REAL_ATS_ACCEPTANCE": "1"}):
                url, profile, path = validate_inputs("https://example.test/apply", str(config), str(cli),
                                                      AcceptanceStage.DETERMINISTIC)
                self.assertEqual(url, "https://example.test/apply")
                self.assertIsInstance(profile, CandidateProfile)
                self.assertEqual(path, cli)
                _, empty_profile, _ = validate_inputs(url, None, str(cli), AcceptanceStage.OBSERVE)
                self.assertIsNone(empty_profile)
                with self.assertRaisesRegex(ValueError, "--config"):
                    validate_inputs(url, None, str(cli), AcceptanceStage.TRAVERSE)
                config.write_text("{}", encoding="utf-8")
                with self.assertRaises(ProfileError):
                    validate_inputs(url, str(config), str(cli), AcceptanceStage.DETERMINISTIC)
                with self.assertRaisesRegex(ValueError, "HTTPS"):
                    validate_inputs("http://example.test/apply", str(config), str(cli),
                                    AcceptanceStage.DETERMINISTIC)

    def test_live_command_is_headed_isolated_and_has_no_saved_profile(self):
        command = server_command(Path("/local/mcp.js"), Path("/private/tmp/isolated"))
        self.assertEqual(command.args[0], "/local/mcp.js")
        self.assertIn("--isolated", command.args)
        self.assertIn("--no-webmcp", command.args)
        self.assertNotIn("--headless", command.args)
        self.assertNotIn("--user-data-dir", command.args)
        self.assertNotIn("--storage-state", command.args)

    def test_authentication_detection_and_private_query_omission(self):
        auth = ApplicationObservation("a", "https://example.test/login?token=secret", "Sign In",
                                      questions=(QuestionObservation("Password", ControlType.TEXT),))
        self.assertEqual(classify_page(auth).kind, PageKind.HUMAN_INTERVENTION_REQUIRED)
        self.assertNotIn("secret", str(observation_summary(auth)))
        application = ApplicationObservation("b", "https://example.test/apply", "Personal Information")
        self.assertEqual(classify_page(application).kind, PageKind.UNKNOWN)


class AcceptanceObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_post_resume_prints_bounded_control_rejection_evidence(self):
        before = ApplicationObservation(
            "before", "https://example.test/apply", "Careers",
            navigation_controls=(NavigationControl("Sign In", NavigationKind.UNKNOWN, "e1"),))
        fresh = ApplicationObservation("fresh", "https://example.test/apply", "Application")
        diagnostic = snapshot_diagnostic('''### Page
- Page URL: https://example.test/apply?token=private-token
### Snapshot
```yaml
  - textbox "Email" [ref=node-email] [disabled]: someone@example.com
  - button "Next" [ref=node-next] unexpected
```
''')

        class Browser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def navigate(self, _url):
                return before

            async def observe(self):
                return fresh

            def diagnostic_for(self, observation_id):
                return diagnostic if observation_id == fresh.observation_id else None

        output = io.StringIO()
        with patch("jobagent.acceptance.PlaywrightMCPAdapter", Browser), \
                patch("builtins.input", return_value=""), redirect_stdout(output):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE,
                                          hold=True)
        self.assertEqual(result, "OBSERVED")
        self.assertIn("post_resume_control_diagnostic=", output.getvalue())
        self.assertIn("'line_syntax_or_attribute_order'", output.getvalue())
        for private in ("node-email", "node-next", "someone@example.com", "private-token"):
            self.assertNotIn(private, output.getvalue())

    async def test_same_intervention_after_resume_reports_fresh_normalized_evidence(self):
        calls = []
        before = ApplicationObservation(
            "before", "https://example.test/apply", "Careers",
            navigation_controls=(NavigationControl("Sign In", NavigationKind.UNKNOWN, "e1"),))
        fresh = ApplicationObservation(
            "fresh", "https://example.test/apply", "Careers",
            navigation_controls=(NavigationControl("Sign In", NavigationKind.UNKNOWN, "e2"),))

        class Browser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, _url):
                calls.append("navigate")
                return before

            async def observe(self):
                calls.append("observe")
                return fresh

        output = io.StringIO()
        with patch("jobagent.acceptance.PlaywrightMCPAdapter", Browser), \
                patch("builtins.input", side_effect=lambda _prompt: calls.append("owner_resume")), \
                redirect_stdout(output):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE,
                                          hold=True)
        self.assertEqual(result, "HUMAN_INTERVENTION_REQUIRED")
        self.assertEqual(calls, ["navigate", "owner_resume", "observe", "close"])
        self.assertIn("'fresh_observation_id_differs': True", output.getvalue())
        self.assertIn("'same_state_stop': True", output.getvalue())
        self.assertIn("'sign_in_controls': 1", output.getvalue())

    async def test_application_after_resume_replaces_intervention_state(self):
        calls = []
        before = ApplicationObservation(
            "before", "https://example.test/apply", "Careers",
            navigation_controls=(NavigationControl("Sign In", NavigationKind.UNKNOWN, "e1"),))
        fresh = ApplicationObservation(
            "fresh", "https://example.test/apply", "Application",
            questions=(QuestionObservation("First Name", ControlType.TEXT,
                                           current_value="private candidate value", target_ref="e3"),),
            navigation_controls=(NavigationControl("user@example.com", NavigationKind.UNKNOWN, "e4"),
                                 NavigationControl("100 Main Street", NavigationKind.UNKNOWN, "e5")))

        class Browser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, _url):
                calls.append("navigate")
                return before

            async def observe(self):
                calls.append("observe")
                return fresh

        output = io.StringIO()
        with patch("jobagent.acceptance.PlaywrightMCPAdapter", Browser), \
                patch("builtins.input", side_effect=lambda _prompt: calls.append("owner_input")), \
                redirect_stdout(output):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE,
                                          hold=True)
        self.assertEqual(result, "OBSERVED")
        self.assertEqual(calls, ["navigate", "owner_input", "observe", "owner_input", "close"])
        self.assertIn("'fresh_classification': ('application', None)", output.getvalue())
        self.assertIn("'same_state_stop': False", output.getvalue())
        self.assertNotIn("private candidate value", output.getvalue())
        self.assertNotIn("user@example.com", output.getvalue())
        self.assertNotIn("100 Main Street", output.getvalue())

    async def test_runner_can_use_injected_trusted_login_without_candidate_config(self):
        class Browser(FakeAuthBrowser):
            def __init__(self, *args, **kwargs):
                super().__init__()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def navigate(self, url):
                return self.current

        browser = Browser()
        provider = FakeCredentials()
        with patch("jobagent.acceptance.PlaywrightMCPAdapter", return_value=browser):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE,
                                          login_identity=LoginIdentity("account-1", "user@example.test"),
                                          credentials=provider)
        self.assertEqual(result, "OBSERVED")
        self.assertEqual(provider.lookups, ["account-1"])
        self.assertEqual([name for name, _ in browser.calls], ["identity", "password", "sign_in"])

    async def test_observation_stage_does_not_fill_or_advance(self):
        calls = []
        observation = ApplicationObservation("a", "https://example.test/apply", "Application")

        class FakeBrowser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, url):
                calls.append("navigate")
                return observation

        with patch("jobagent.acceptance.PlaywrightMCPAdapter", FakeBrowser):
            result = await run_acceptance("https://example.test/apply",
                                          CandidateProfile.from_mapping(profile_data()),
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE)
        self.assertEqual(result, "OBSERVED")
        self.assertEqual(calls, ["navigate", "close"])

    async def test_empty_loading_snapshot_gets_one_read_only_reobservation(self):
        calls = []
        observation = ApplicationObservation("a", "https://example.test/apply", "Application")

        class FakeBrowser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, url):
                calls.append("navigate")
                raise SnapshotEmpty("loading")

            async def observe(self):
                calls.append("observe")
                return observation

        with patch("jobagent.acceptance.PlaywrightMCPAdapter", FakeBrowser), \
                patch("jobagent.acceptance.asyncio.sleep"):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE)
        self.assertEqual(result, "OBSERVED")
        self.assertEqual(calls, ["navigate", "observe", "close"])

    async def test_access_challenge_stops_without_interaction(self):
        calls = []

        class FakeBrowser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, url):
                calls.append("navigate")
                raise SnapshotAccessChallenge("site returned HTTP 429 access challenge")

        with patch("jobagent.acceptance.PlaywrightMCPAdapter", FakeBrowser):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE)
        self.assertEqual(result, "HUMAN_INTERVENTION_REQUIRED")
        self.assertEqual(calls, ["navigate", "close"])

    async def test_challenge_resumes_only_after_owner_input_and_fresh_observation(self):
        calls = []
        observation = ApplicationObservation("fresh", "https://example.test/apply",
                                              "Application", questions=(
                                                  QuestionObservation("First Name", ControlType.TEXT),))

        class FakeBrowser:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                calls.append("close")

            async def navigate(self, url):
                calls.append("navigate")
                raise SnapshotAccessChallenge("site returned HTTP 429 access challenge")

            async def observe(self):
                calls.append("observe")
                return observation

        def owner_input(message):
            calls.append("owner_input")

        with patch("jobagent.acceptance.PlaywrightMCPAdapter", FakeBrowser), \
                patch("builtins.input", side_effect=owner_input):
            result = await run_acceptance("https://example.test/apply", None,
                                          Path("/local/mcp.js"), AcceptanceStage.OBSERVE,
                                          hold=True)
        self.assertEqual(result, "OBSERVED")
        self.assertEqual(calls, ["navigate", "owner_input", "observe", "owner_input", "close"])



if __name__ == "__main__":
    unittest.main()
