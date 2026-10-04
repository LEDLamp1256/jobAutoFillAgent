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
    ChooseOption, FillText, NavigationKind, SearchOptions, SubmissionPermission, Toggle,
    UploadDocument,
    semantic_fingerprint,
)
from jobagent.controller import ApplicationController, ControllerStop
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from tests.test_resolution import profile_data
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

    async def test_workday_equivalent_custom_controls_and_resume(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name))
        data = profile_data()
        data["personal_info"]["address"]["state"] = "California"
        data["qa_bank"]["how_did_you_hear_about_us"] = {"answer": "LinkedIn"}
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/workday_custom_controls.html"
            initial = await browser.navigate(url)
            fields = {q.label: q for q in initial.questions}
            phone = fields["Country / Territory Phone Code"]
            self.assertEqual(phone.control_type.value, "choice")
            self.assertTrue(phone.answer_state().satisfied)
            self.assertEqual(len([q for q in initial.questions if q.label == phone.label]), 1)
            self.assertFalse(fields["How Did You Hear About Us?"].answer_state().satisfied,
                             fields["How Did You Hear About Us?"])
            self.assertIn("State", fields, (list(fields), initial.discovery_summary))
            self.assertFalse(fields["State"].answer_state().satisfied)
            controller = ApplicationController(browser,
                DeterministicAnswerResolver(CandidateProfile.from_mapping(data)))
            first = await controller.run(ApplicationSession("fixture", url), current_page_only=True,
                                         initial_observation=initial)
            self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
            settled = {field.question.label: field for field in first.fields}
            self.assertTrue(settled["State"].question.answer_state().satisfied)
            self.assertTrue(settled["How Did You Hear About Us?"].question.answer_state().satisfied)
            self.assertTrue(settled["Have you previously worked for or are you currently working for Workday as an employee or contractor?"].needs_review)
            self.assertEqual(await browser._page_marker(
                "() => document.querySelector('input[name=prior]:checked')?.value || ''"), "")
            await browser._call("browser_evaluate", {"function":
                "() => { document.querySelector('input[name=prior][value=No]').click(); return true; }"})
            second = await controller.run(ApplicationSession("fixture", url), current_page_only=True)
            self.assertEqual(second.stop, ControllerStop.PAGE_ADVANCED)
            self.assertEqual(second.session.current_observation.heading, "Next Page")
            count = await browser._page_marker("() => window.advanceCount")
            self.assertEqual(count, 1)

    async def test_open_token_removal_and_uncommitted_search(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        command = MCPServerCommand(command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name))
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/workday_custom_controls.html"
            await browser.navigate(url)
            await browser._call("browser_evaluate", {"function":
                "() => { document.querySelector('#code-options').hidden=false; "
                "document.querySelector('#source-search').value='LinkedIn'; return true; }"})
            open_page = await browser.observe()
            fields = {q.label: q for q in open_page.questions}
            self.assertTrue(fields["Country / Territory Phone Code"].answer_state().satisfied)
            referral = fields["How Did You Hear About Us?"]
            self.assertFalse(referral.answer_state().satisfied)
            self.assertTrue(referral.current_value)
            await browser._call("browser_evaluate", {"function":
                "() => { document.querySelector('#phone-token button').click(); return true; }"})
            removed = await browser.observe()
            phone = next(q for q in removed.questions if q.label == "Country / Territory Phone Code")
            self.assertTrue(phone.required)
            self.assertFalse(phone.answer_state().satisfied)

    async def test_sectioned_custom_choices_and_selected_token(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(
            command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name),
        )
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/sectioned_custom_choices.html"
            observation = await browser.navigate(url)
            fields = {question.label: question for question in observation.questions}
            self.assertNotIn("Phone", fields)
            self.assertNotIn("Address", fields)
            for label in ("Phone Device Type", "State", "Country / Territory Phone Code",
                          "How Did You Hear About Us?"):
                self.assertIn(label, fields, (observation.discovery_summary,
                    [(q.label, q.discovery_source) for q in observation.questions]))
                self.assertTrue(fields[label].required, label)
                self.assertTrue(fields[label].answer_state().satisfied, label)
            self.assertEqual(fields["Phone Device Type"].control_type.value, "choice")
            self.assertEqual(fields["State"].control_type.value, "choice")
            self.assertEqual(fields["Country / Territory Phone Code"].control_type.value, "choice")
            self.assertEqual(len([q for q in observation.questions if q.label ==
                                  "Country / Territory Phone Code"]), 1)
            self.assertFalse(fields["I have a preferred name"].answer_state().satisfied)
            self.assertEqual(fields["Previously worked here?"].current_value, "No")
            self.assertIn("Save and Continue", [c.label for c in observation.navigation_controls])
            self.assertIn("Submit Application", [c.label for c in observation.navigation_controls])
            self.assertEqual(next(c.kind for c in observation.navigation_controls
                if c.label == "Submit Application"), NavigationKind.SUBMIT)
            self.assertGreaterEqual(dict(observation.discovery_summary.ignored_reasons).get(
                "section_or_container", 0), 2)
            self.assertEqual(observation.discovery_summary.dom_recovered_field_count, 2)
            await browser._call("browser_evaluate", {"function":
                "() => { document.getElementById('state-choice').textContent = 'Select One'; return true; }"})
            empty = await browser.observe()
            state = next(q for q in empty.questions if q.label == "State")
            self.assertTrue(state.required)
            self.assertFalse(state.answer_state().satisfied)

    async def test_dom_fallback_recovers_hidden_accessibility_field_and_associated_choices(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(
            command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name),
        )
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/dom_fallback_controls.html"
            observed = await browser.navigate(url)
            fields = {question.label: question for question in observed.questions}
            self.assertNotIn("items selected", fields)
            self.assertIn("State / Province", fields)
            self.assertEqual(fields["State / Province"].discovery_source, "dom_fallback")
            self.assertTrue(fields["State / Province"].required)
            self.assertFalse(fields["State / Province"].answer_state().satisfied)
            self.assertTrue(fields["Marker Required"].required)
            self.assertFalse(fields["Marker Required"].answer_state().satisfied)
            self.assertEqual(fields["Country / Territory Phone Code"].current_value,
                             "Exampleland (+9)", (observed.discovery_summary,
                               [(q.label, q.current_value, q.discovery_source)
                                for q in observed.questions]))
            self.assertTrue(fields["Country / Territory Phone Code"].answer_state().satisfied)
            self.assertFalse(fields["How Did You Hear About Us?"].answer_state().satisfied)
            self.assertFalse(fields["I have a preferred name"].answer_state().satisfied)
            self.assertTrue(fields["Previously contacted"].answer_state().satisfied)
            self.assertEqual(len([q for q in observed.questions if q.label ==
                                  "Country / Territory Phone Code"]), 1)
            self.assertGreaterEqual(observed.discovery_summary.dom_recovered_field_count, 1)
            self.assertIn("Save and Continue", [c.label for c in observed.navigation_controls])
            self.assertIn("Submit Application", [c.label for c in observed.navigation_controls])
            self.assertEqual([a.label for a in observed.section_actions], ["Add"])
            await browser._call("browser_evaluate", {"function":
                "() => { document.getElementById('referral-button').click(); "
                "document.getElementById('state-button').click(); return true; }"})
            fresh = await browser.observe()
            by_label = {question.label: question for question in fresh.questions}
            for label in ("State / Province", "How Did You Hear About Us?"):
                self.assertEqual(fields[label].identity(), by_label[label].identity())
                self.assertTrue(by_label[label].answer_state().satisfied)
            self.assertEqual(next(c.kind for c in fresh.navigation_controls
                                  if c.label == "Submit Application"), NavigationKind.SUBMIT)

    async def test_common_controls_reach_final_human_boundary_without_submit(self):
        cli = os.environ.get("JOB_AGENT_PLAYWRIGHT_MCP_CLI")
        self.assertTrue(cli and Path(cli).is_file())
        command = MCPServerCommand(
            command=shutil.which("node") or "node",
            args=(cli, "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
            cwd=Path(self.mcp_output.name),
        )
        async with PlaywrightMCPAdapter(command) as browser:
            url = f"http://127.0.0.1:{self.server.server_port}/common_controls_application.html"
            observation = await browser.navigate(url)
            session = ApplicationSession("common-controls-fixture", observation.location)
            session.record_observation(observation)
            labels = {question.label for question in observation.questions}
            self.assertNotIn("Suggestions", labels)
            self.assertIn("First Name", labels)
            self.assertIn("Last Name", labels)
            self.assertIn("Are you authorized to work in the US?", labels)
            self.assertIn("Willing to relocate", labels)
            self.assertIn("State", labels)
            self.assertIn("School", labels)
            fields = {question.label: question for question in observation.questions}
            self.assertTrue(fields["Legal First Name"].answer_state().satisfied)
            self.assertIs(fields["Middle Name"].required, False)
            self.assertFalse(fields["Middle Name"].answer_state().satisfied)
            self.assertEqual(fields["Have you previously worked here?"].current_value, "No")
            self.assertTrue(fields["Previously contacted"].answer_state().satisfied)
            self.assertFalse(fields["Willing to relocate"].answer_state().satisfied)
            self.assertTrue(fields["State"].required)
            self.assertFalse(fields["State"].answer_state().satisfied)
            self.assertEqual(fields["Current region"].current_value, "Pacific")
            self.assertIn("Country / Territory Phone Code", fields,
                          browser.diagnostic_for(observation.observation_id))
            self.assertEqual(fields["Country / Territory Phone Code"].current_value,
                             "Exampleland (+9)")
            self.assertTrue(fields["Country / Territory Phone Code"].answer_state().satisfied)
            self.assertFalse(fields["Referral Source"].answer_state().satisfied)
            self.assertTrue(fields["Referral Source"].required)
            self.assertTrue(fields["State / Province"].required)
            self.assertFalse(fields["State / Province"].answer_state().satisfied)
            self.assertNotIn("items selected", labels)
            # Synthetic owner actions change button text. Each subsequent read
            # is a new observation; the agent does not choose these answers.
            for label, expected in (("Referral Source", "Example source"),
                                    ("State / Province", "California")):
                before = next(question for question in observation.questions if question.label == label)
                await browser._call("browser_click", {"element": "synthetic owner choice",
                                                       "target": before.target_ref})
                fresh = await browser.observe()
                after = next(question for question in fresh.questions if question.label == label)
                self.assertEqual(after.identity(), before.identity())
                self.assertNotEqual(fresh.observation_id, observation.observation_id)
                self.assertEqual(after.current_value, expected)
                self.assertTrue(after.answer_state().satisfied)
                observation = fresh
                session.record_observation(observation)
            self.assertEqual(len([question for question in observation.questions
                                  if question.label == "Are you authorized to work in the US?"]), 1)
            self.assertEqual([item.label for item in observation.navigation_controls
                              if item.kind is NavigationKind.ADVANCE], ["Next"])
            state = next(question for question in observation.questions if question.label == "State")
            chosen = Answer("personal.address.state", "California", AnswerSource.CANDIDATE_PROFILE,
                            AnswerScope.GLOBAL)
            observation = (await browser.choose_option(
                ChooseOption(state.target_ref, observation.observation_id, chosen), session)).observation
            session.record_observation(observation)
            self.assertEqual(next(question for question in observation.questions
                                  if question.label == "State").current_value, "California")
            relocate = next(question for question in observation.questions
                            if question.label == "Willing to relocate")
            affirmative = Answer("employment.relocation", "Yes", AnswerSource.CANDIDATE_PROFILE,
                                 AnswerScope.GLOBAL)
            observation = (await browser.toggle(
                Toggle(relocate.target_ref, observation.observation_id, affirmative), session)).observation
            session.record_observation(observation)
            self.assertEqual(next(question for question in observation.questions
                                  if question.label == "Willing to relocate").current_value, "checked")
            school = next(question for question in observation.questions if question.label == "School")
            college = Answer("education.institution", "Pacific College", AnswerSource.CANDIDATE_PROFILE,
                             AnswerScope.APPLICATION, session.application_id)
            observation = (await browser.search_options(
                SearchOptions(school.target_ref, observation.observation_id, college), session)).observation
            session.record_observation(observation)
            self.assertIn("Pacific College", next(question for question in observation.questions
                                                   if question.label == "School").options)
            school = next(question for question in observation.questions if question.label == "School")
            observation = (await browser.choose_option(
                ChooseOption(school.target_ref, observation.observation_id, college), session)).observation
            session.record_observation(observation)
            self.assertEqual(next(question for question in observation.questions
                                  if question.label == "School").current_value, "Pacific College")
            next_button = next(item for item in observation.navigation_controls
                               if item.kind is NavigationKind.ADVANCE)
            observation = (await browser.activate_navigation(
                Advance(next_button.target_ref, observation.observation_id), session)).observation
            session.record_observation(observation)
            self.assertEqual(observation.heading, "Contact and documents")
            self.assertIn("Available Start Month", {question.label for question in observation.questions})
            self.assertFalse(observation.section_actions)
            page_two_fields = {question.label: question for question in observation.questions}
            self.assertTrue(page_two_fields["Phone Number"].answer_state().satisfied)
            self.assertFalse(page_two_fields["Phone Extension"].answer_state().satisfied)
            self.assertIs(page_two_fields["Phone Extension"].required, False)
            self.assertTrue(page_two_fields["State / Province"].answer_state().satisfied)
            self.assertTrue(page_two_fields["Required page-two choice"].required)
            self.assertFalse(page_two_fields["Required page-two choice"].answer_state().satisfied)
            self.assertTrue(page_two_fields["Committed school"].answer_state().satisfied)
            self.assertTrue(page_two_fields["Available Start Month"].answer_state().satisfied)
            self.assertEqual(page_two_fields["Upload Resume"].current_value, "synthetic.pdf")
            month = next(question for question in observation.questions
                         if question.label == "Available Start Month")
            self.assertEqual(month.control_type.value, "date")
            month_answer = Answer("fixture.month", "2027-03", AnswerSource.QA_BANK,
                                  AnswerScope.APPLICATION, session.application_id)
            observation = (await browser.fill_text(
                FillText(month.target_ref, observation.observation_id, month_answer), session)).observation
            session.record_observation(observation)
            self.assertEqual(next(question for question in observation.questions
                                  if question.label == "Available Start Month").current_value,
                             "2027-03")
            synthetic = Path(self.mcp_output.name) / "synthetic.pdf"
            synthetic.write_bytes(b"%PDF-1.4 synthetic test file")
            resume = next(question for question in observation.questions
                          if question.label == "Upload Resume")
            resume_answer = Answer("documents.resume", str(synthetic), AnswerSource.CANDIDATE_PROFILE,
                                   AnswerScope.GLOBAL)
            observation = (await browser.upload_document(
                UploadDocument(resume.target_ref, observation.observation_id,
                               "documents.resume", resume_answer), session)).observation
            session.record_observation(observation)
            self.assertEqual(next(question for question in observation.questions
                                  if question.label == "Upload Resume").current_value,
                             "synthetic.pdf")
            required_choice = next(question for question in observation.questions
                                   if question.label == "Required page-two choice")
            await browser._call("browser_click", {"element": "synthetic owner choice",
                                                   "target": required_choice.target_ref})
            observation = await browser.observe()
            session.record_observation(observation)
            self.assertIn("Required page-two choice", {q.label for q in observation.questions})
            self.assertTrue(next(question for question in observation.questions
                                 if question.label == "Required page-two choice").answer_state().satisfied)
            save_button = next(item for item in observation.navigation_controls
                               if item.label == "Save and Continue")
            observation = (await browser.activate_navigation(
                Advance(save_button.target_ref, observation.observation_id), session)).observation
            session.record_observation(observation)
            self.assertEqual(observation.heading, "Experience and education")
            self.assertEqual([item.label for item in observation.section_actions],
                             ["Add", "Add", "Add Another"])
            skills = next(question for question in observation.questions if question.label == "Skills")
            self.assertEqual(skills.selected_values, ("Example skill",))
            self.assertTrue(skills.answer_state().satisfied)
            review_button = next(item for item in observation.navigation_controls
                                 if item.label == "Continue to Review")
            observation = (await browser.activate_navigation(
                Advance(review_button.target_ref, observation.observation_id), session)).observation
            session.record_observation(observation)
            self.assertEqual(observation.heading, "Review Application")
            self.assertTrue(observation.review_like)
            submit = next(item for item in observation.navigation_controls
                          if item.kind is NavigationKind.SUBMIT)
            with self.assertRaises(PermissionError):
                await browser.activate_navigation(
                    Advance(submit.target_ref, observation.observation_id), session)
            fresh = await browser.observe()
            self.assertEqual(fresh.heading, "Review Application")
            self.assertTrue(fresh.review_like)

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
