"""Acceptance runner contracts without an external site or headed browser."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobagent.acceptance import (
    AcceptanceStage, authentication_required, observation_summary, run_acceptance,
    server_command, validate_inputs,
)
from jobagent.domain import ApplicationObservation, ControlType, QuestionObservation
from jobagent.resolution import CandidateProfile, ProfileError
from jobagent.snapshot import SnapshotAccessChallenge, SnapshotEmpty
from tests.test_resolution import profile_data


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
        self.assertTrue(authentication_required(auth))
        self.assertNotIn("secret", str(observation_summary(auth)))
        application = ApplicationObservation("b", "https://example.test/apply", "Personal Information")
        self.assertFalse(authentication_required(application))


class AcceptanceObservationTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(result, "ACCESS_CHALLENGE")
        self.assertEqual(calls, ["navigate", "close"])


if __name__ == "__main__":
    unittest.main()
