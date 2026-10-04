"""Offline V2-11 current-page, reporting, and Resume contracts."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jobagent.application_launcher import ApplicationLauncher
from jobagent.batch_domain import Blocker, HumanAction, Provenance, ReviewState, TaskStatus, Verification
from jobagent.browser import BrowserActionResult
from jobagent.controller import ApplicationController, ControllerStop, _has_answer
from jobagent.control_plane import LocalControlPlane
from jobagent.dedupe import ListingInput
from jobagent.domain import (ActionOutcome, ActionPolicy, ActionStatus, Answer, AnswerScope,
                             AnswerSource, ApplicationObservation, ApplicationSession, ControlType,
                             NavigationControl, NavigationKind, QuestionObservation)
from jobagent.persistence import BatchStore
from jobagent.resolution import (CandidateProfile, CanonicalResult, CanonicalStatus,
                                 DeterministicAnswerResolver, Resolution, ResolutionStatus)
from jobagent.runtime_worker import LaunchAndLoginWorker, LocalLoginConfiguration
from jobagent.snapshot import SnapshotNormalizer
from jobagent.scheduler import ApplicationScheduler
from tests.test_resolution import profile_data
from tests.test_scheduler import owner


class AnswerSatisfactionTests(unittest.TestCase):
    def test_empty_optional_and_placeholder_are_not_manual_answers(self):
        for value in (None, "", "   ", "Select One", "Choose", "No file chosen",
                      "Phone Extension"):
            question = QuestionObservation("Phone Extension", ControlType.TEXT,
                                           current_value=value, required=False)
            self.assertFalse(_has_answer(question), value)
        self.assertFalse(_has_answer(QuestionObservation(
            "Preferred name", ControlType.TOGGLE, current_value="unchecked", required=False)))

    def test_fresh_selected_answer_is_satisfied_independent_of_widget_capability(self):
        question = QuestionObservation("Referral source", ControlType.UNKNOWN,
                                       current_value="Employee referral", required=True)
        self.assertTrue(_has_answer(question))


class ReferralFreshStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_answered_then_empty_same_identity_becomes_required_review(self):
        class Browser:
            value = "Example source"
            observations = 0
            actions = 0

            async def observe(self):
                self.observations += 1
                question = QuestionObservation("How Did You Hear About Us?", ControlType.CHOICE,
                    required=True, current_value=self.value, target_ref=None)
                return ApplicationObservation(f"fresh-{self.observations}",
                    "https://example.test/apply", "Application",
                    questions=(question,), navigation_controls=(NavigationControl(
                        "Submit Application", NavigationKind.SUBMIT, "submit-ref"),))

        browser = Browser()
        profile = CandidateProfile.from_mapping({"personal_info": {}, "work_history": [],
            "education": [], "skills": {}, "qa_bank": {}, "documents": {},
            "application_preferences": {}})
        controller = ApplicationController(browser, DeterministicAnswerResolver(profile))
        answered = await controller.run(ApplicationSession("task", "https://example.test/apply"),
                                        current_page_only=True)
        self.assertEqual(answered.fields[0].action, "manual_complete")
        self.assertFalse(answered.fields[0].needs_review)
        browser.value = "Select One"
        empty = await controller.run(ApplicationSession("task", "https://example.test/apply"),
                                     current_page_only=True)
        self.assertEqual(answered.fields[0].question.report_identity(),
                         empty.fields[0].question.report_identity())
        self.assertFalse(empty.fields[0].question.answer_state().satisfied)
        self.assertTrue(empty.fields[0].needs_review)
        self.assertEqual(empty.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.observations, 2)
        self.assertEqual(browser.actions, 0)


class PageBrowser:
    def __init__(self, *, terminal=True, ambiguous=False, failed_verify=False,
                 unknown_required=True, extra_fields=()):
        self.values = {}
        self.counter = 0
        self.last = None
        self.calls = []
        self.terminal = terminal
        self.ambiguous = ambiguous
        self.failed_verify = failed_verify
        self.annotations = {}
        self.unknown_required = unknown_required
        self.extra_fields = extra_fields
        self.page = 1
        self.heading = None
        self.checkpoint = None

    def observation(self):
        self.counter += 1
        oid = f"obs-{self.counter}"
        def field(label, kind=ControlType.TEXT, options=(), required=True):
            return QuestionObservation(label, kind, options=options, required=required,
                                       current_value=self.values.get(label), target_ref=f"{oid}-{label}")
        questions = (field("First name"), field("Last name"), field("Email"),
                     field("Are you authorized to work in the US?", ControlType.CHOICE, ("Yes", "No")),
                     field("Fixture clearance code", required=self.unknown_required),
                     *(field(extra[0], extra[1], required=extra[2] if len(extra) > 2 else True)
                       for extra in self.extra_fields))
        is_terminal = self.terminal or self.page > 1
        nav = [NavigationControl("Submit application" if is_terminal else "Continue",
                                 NavigationKind.SUBMIT if is_terminal else NavigationKind.ADVANCE,
                                 f"{oid}-nav")]
        if self.ambiguous:
            nav.append(NavigationControl("Other", NavigationKind.UNKNOWN, f"{oid}-other"))
        self.last = ApplicationObservation(oid, "http://127.0.0.1/fixture",
                                           self.heading or ("Application details" if self.page == 1
                                                            else "Review application"),
                                           questions=questions, navigation_controls=tuple(nav),
                                           checkpoint=self.checkpoint)
        return self.last

    async def observe(self):
        self.calls.append("observe")
        return self.observation()

    async def fill_text(self, action, session):
        ActionPolicy.authorize(action, session)
        self.calls.append("fill")
        if not self.failed_verify:
            label = next(q.label for q in self.last.questions if q.target_ref == action.target_ref)
            self.values[label] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

    async def choose_option(self, action, session):
        ActionPolicy.authorize(action, session)
        self.calls.append("choose")
        self.values["Are you authorized to work in the US?"] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

    async def activate_navigation(self, action, session):
        ActionPolicy.authorize(action, session)
        self.calls.append("advance")
        self.page += 1
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

    async def annotate_field(self, ref, observation_id, state):
        assert observation_id == self.last.observation_id
        question = next(q for q in self.last.questions if q.target_ref == ref)
        self.annotations[question.label] = state


def resolver():
    return DeterministicAnswerResolver(CandidateProfile.from_mapping(profile_data()))


class ControllerPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_prior_employment_radio_is_never_opened_or_answered(self):
        label = ("Have you previously worked for or are you currently working for "
                 "Workday as an employee or contractor?")
        class Browser:
            actions = []
            async def observe(self):
                return ApplicationObservation("fresh", "https://example.test/apply", "Application",
                    questions=(QuestionObservation(label, ControlType.CHOICE, required=True,
                        target_ref="first-radio-yes"),),
                    navigation_controls=(NavigationControl("Save and Continue",
                        NavigationKind.ADVANCE, "continue"),))
            async def reveal_options(self, *_):
                self.actions.append("clicked first radio")
                raise AssertionError("unanswered radio must not be clicked")
            async def choose_option(self, *_):
                self.actions.append("selected option")
                raise AssertionError("unanswered radio must not be selected")
            async def activate_navigation(self, *_):
                raise AssertionError("required unknown answer must block advance")
        browser = Browser()
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.actions, [])
        self.assertEqual(len(result.fields), 1)
        self.assertEqual(result.fields[0].reason, "no_safe_answer")
        self.assertFalse(result.fields[0].question.answer_state().satisfied)

    async def test_optionless_radio_target_is_never_clicked_even_with_known_boolean(self):
        class Browser:
            async def observe(self):
                return ApplicationObservation("fresh", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("Are you authorized to work in the US?",
                        ControlType.CHOICE, required=True, target_ref="first-radio-yes",
                        raw_role="radio"),))
            async def reveal_options(self, *_):
                raise AssertionError("radio target is an answer, not a menu trigger")
        result = await ApplicationController(Browser(), resolver()).run(
            ApplicationSession("fixture", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(result.fields[0].reason, "live_options_unavailable")

    async def test_browser_fill_exception_has_bounded_stage_without_private_detail(self):
        class Browser:
            async def observe(self):
                return ApplicationObservation("fresh", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("First Name", ControlType.TEXT,
                        target_ref="first-name-ref", required=True),))
            async def fill_text(self, *_):
                raise RuntimeError("password=private-value ref=secret-browser-ref")
        result = await ApplicationController(Browser(), resolver()).run(
            ApplicationSession("task", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.FAILED)
        self.assertEqual(result.failure_diagnostic.stage, "fill")
        self.assertEqual(result.failure_diagnostic.category, "unexpected_exception")
        self.assertNotIn("private-value", result.failure_diagnostic.detail)
        self.assertNotIn("secret-browser-ref", result.failure_diagnostic.detail)

    async def test_unsupported_dom_choice_target_is_recoverable_review(self):
        class Browser:
            calls = 0
            async def observe(self):
                self.calls += 1
                return ApplicationObservation(f"fresh-{self.calls}",
                    "https://example.test/apply", "Application",
                    questions=(QuestionObservation("How Did You Hear About Us?", ControlType.CHOICE,
                        required=True, options=("LinkedIn",), discovery_source="dom_fallback"),),
                    navigation_controls=(NavigationControl("Save and Continue",
                        NavigationKind.ADVANCE, f"next-{self.calls}"),))
            async def act_on_dom_choice(self, *_):
                raise PermissionError("selector is ambiguous")
            async def activate_navigation(self, *_):
                raise AssertionError("unresolved required choice must block navigation")
        data = profile_data()
        data["qa_bank"]["how_did_you_hear_about_us"] = {"answer": "LinkedIn"}
        browser = Browser()
        result = await ApplicationController(browser,
            DeterministicAnswerResolver(CandidateProfile.from_mapping(data))).run(
                ApplicationSession("fixture", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(result.fields[0].reason, "missing_or_ambiguous_target")

    async def test_dom_recovered_choice_verifies_fresh_selection_without_durable_ref(self):
        data = profile_data()
        data["personal_info"]["address"]["state"] = "California"
        class DOMChoiceBrowser:
            serial = 0
            selected = False
            actions = []
            async def observe(self):
                self.serial += 1
                return ApplicationObservation(f"dom-{self.serial}",
                    "https://example.test/apply", "Application",
                    questions=(QuestionObservation("State", ControlType.CHOICE,
                        options=("California", "Colorado"), required=True,
                        current_value="California" if self.selected else None,
                        selection_confirmed=self.selected,
                        discovery_source="dom_fallback"),),
                    navigation_controls=(NavigationControl("Submit Application",
                        NavigationKind.SUBMIT, f"submit-{self.serial}"),))
            async def act_on_dom_choice(self, question, observation_id, mode, value, session):
                self.actions.append((question.target_ref, observation_id, mode, value))
                self.selected = True
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED),
                                           await self.observe())
            async def activate_navigation(self, *_):
                raise AssertionError("final submit must remain human-only")
        browser = DOMChoiceBrowser()
        result = await ApplicationController(browser, DeterministicAnswerResolver(
            CandidateProfile.from_mapping(data))).run(
                ApplicationSession("task", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(browser.actions, [(None, "dom-1", "choose", "California")])
        self.assertEqual(result.fields[0].action, "selected_option")
        self.assertTrue(result.fields[0].verified)

    async def test_selected_custom_values_clear_required_blockers_and_advance_once(self):
        class SnapshotBrowser:
            def __init__(self):
                self.serial = 0
                self.manual = False
                self.advances = 0
                self.last = None

            async def observe(self):
                self.serial += 1
                ref = self.serial * 10
                source = (f'  - group "How Did You Hear About Us?" [aria-required=true] [ref=e{ref + 8}]:\n'
                          f'    - button "Employee referral" [ref=e{ref + 4}]\n') if self.manual else (
                          f'  - group "How Did You Hear About Us?" [aria-required=true] [ref=e{ref + 8}]:\n'
                          f'    - button "Select One" [ref=e{ref + 4}]\n')
                state = "California" if self.manual else "Select One"
                snapshot = f'''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e{ref}]:
  - heading "Application" [level=2] [ref=e{ref + 1}]
  - group "Country / Territory Phone Code" [aria-required=true] [ref=e{ref + 9}]:
    - button "Country / Territory Phone Code" [ref=e{ref + 2}]
    - generic: items selected
      - generic: Exampleland (+9)
{source}  - generic: State
  - text: "*"
  - combobox "State" [ref=e{ref + 6}]: {state}
  - button "Save and Continue" [ref=e{ref + 7}]
```
'''
                self.last = SnapshotNormalizer().normalize(snapshot, f"fresh-{self.serial}").observation
                return self.last

            async def activate_navigation(self, action, session):
                ActionPolicy.authorize(action, session)
                assert action.observation_id == self.last.observation_id
                assert action.target_ref in {control.target_ref for control in self.last.navigation_controls}
                self.advances += 1
                self.serial += 1
                snapshot = f'''### Page
- Page URL: https://example.test/apply/review
### Snapshot
```yaml
- main [ref=e{self.serial * 10}]:
  - heading "Review Application" [level=2] [ref=e{self.serial * 10 + 1}]
  - button "Submit Application" [ref=e{self.serial * 10 + 2}]
```
'''
                self.last = SnapshotNormalizer().normalize(snapshot, f"fresh-{self.serial}").observation
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.last)

        browser = SnapshotBrowser()
        empty_profile = CandidateProfile.from_mapping({
            "personal_info": {}, "work_history": [], "education": [], "skills": {},
            "qa_bank": {}, "documents": {}, "application_preferences": {}})
        controller = ApplicationController(browser, DeterministicAnswerResolver(empty_profile))
        first = await controller.run(ApplicationSession("task", "https://example.test/apply"),
                                     current_page_only=True)
        self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
        by_label = {field.question.label: field for field in first.fields}
        self.assertEqual(by_label["Country / Territory Phone Code"].action, "manual_complete")
        self.assertFalse(by_label["Country / Territory Phone Code"].needs_review)
        self.assertTrue(by_label["How Did You Hear About Us?"].needs_review)
        self.assertTrue(by_label["State"].question.required)
        self.assertTrue(by_label["State"].needs_review)
        self.assertEqual(browser.advances, 0)
        first_ref = by_label["How Did You Hear About Us?"].question.target_ref

        browser.manual = True
        resumed = await controller.run(ApplicationSession("task", "https://example.test/apply"),
                                       current_page_only=True)
        self.assertEqual(resumed.stop, ControllerStop.PAGE_ADVANCED)
        settled = {field.question.label: field for field in resumed.fields}
        self.assertEqual(settled["How Did You Hear About Us?"].action, "manual_complete")
        self.assertFalse(settled["How Did You Hear About Us?"].needs_review)
        self.assertEqual(settled["State"].action, "manual_complete")
        self.assertNotEqual(settled["How Did You Hear About Us?"].question.target_ref, first_ref)
        self.assertEqual(browser.advances, 1)
        self.assertEqual(resumed.session.current_observation.heading, "Review Application")
        self.assertFalse(any(action.kind == "submit" for action in resumed.session.action_history))

    async def test_configured_resume_file_is_uploaded_once_and_freshly_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"%PDF-1.4 synthetic")
            data = profile_data()
            data["documents"]["resume_path"] = str(path)

            class UploadBrowser:
                def __init__(self):
                    self.serial = 0
                    self.value = None
                    self.calls = []

                async def observe(self):
                    self.serial += 1
                    return ApplicationObservation(f"upload-{self.serial}", "https://example.test/apply",
                        "Application", questions=(QuestionObservation("Upload Resume", ControlType.FILE,
                            required=True, current_value=self.value,
                            target_ref=f"upload-ref-{self.serial}"),),
                        navigation_controls=(NavigationControl("Submit Application", NavigationKind.SUBMIT,
                            f"submit-{self.serial}"),))

                async def upload_document(self, action, session):
                    ActionPolicy.authorize(action, session)
                    self.calls.append((action.target_ref, action.document_key))
                    self.value = "synthetic.pdf"
                    return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

                async def activate_navigation(self, *_):
                    raise AssertionError("final submit must remain human-only")

            browser = UploadBrowser()
            result = await ApplicationController(browser, DeterministicAnswerResolver(
                CandidateProfile.from_mapping(data))).run(
                ApplicationSession("application-1", "https://example.test/apply"),
                current_page_only=True)
            self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
            self.assertEqual(browser.calls, [("upload-ref-1", "documents.resume")])
            self.assertEqual(result.fields[0].action, "uploaded_document")
            self.assertFalse(result.fields[0].needs_review)

    async def test_known_negative_checkbox_state_is_confirmed_without_click(self):
        data = profile_data()
        data["personal_info"]["willing_to_relocate"] = False

        class CheckboxBrowser:
            calls = []

            async def observe(self):
                return ApplicationObservation("checkbox-1", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("Willing to relocate", ControlType.TOGGLE,
                        required=True, current_value="unchecked", target_ref="checkbox-ref"),),
                    navigation_controls=(NavigationControl("Submit Application", NavigationKind.SUBMIT,
                        "submit-ref"),))

            async def toggle(self, *_):
                self.calls.append("toggle")
                raise AssertionError("already matching negative state")

            async def activate_navigation(self, *_):
                raise AssertionError("final submit must remain human-only")

        browser = CheckboxBrowser()
        result = await ApplicationController(browser, DeterministicAnswerResolver(
            CandidateProfile.from_mapping(data))).run(
                ApplicationSession("application-1", "https://example.test/apply"),
                current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertFalse(browser.calls)
        self.assertEqual(result.fields[0].action, "confirmed_trusted")

    async def test_month_entry_requires_known_format_and_fresh_verification(self):
        class DateResolver:
            async def resolve(self, question, application_id):
                answer = Answer("fixture.start_month", "2027-03", AnswerSource.QA_BANK,
                                AnswerScope.APPLICATION, application_id)
                return Resolution(ResolutionStatus.SAFE_TO_FILL,
                                  CanonicalResult(CanonicalStatus.MATCHED, answer.semantic_key), answer)

            def record_answer(self, answer):
                pass

        class DateBrowser:
            def __init__(self):
                self.value = None
                self.serial = 0
                self.calls = []

            async def observe(self):
                self.serial += 1
                return ApplicationObservation(f"date-{self.serial}", "https://example.test/apply",
                    "Application", questions=(QuestionObservation("Start month", ControlType.DATE,
                        semantic_key="fixture.start_month", required=True, current_value=self.value,
                        target_ref=f"month-{self.serial}", date_format="month"),),
                    navigation_controls=(NavigationControl("Submit Application", NavigationKind.SUBMIT,
                        f"submit-{self.serial}"),))

            async def fill_text(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append((action.target_ref, action.answer.value))
                self.value = action.answer.value
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def activate_navigation(self, *_):
                raise AssertionError("final submit must remain human-only")

        browser = DateBrowser()
        result = await ApplicationController(browser, DateResolver()).run(
            ApplicationSession("application-1", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(browser.calls, [("month-1", "2027-03")])
        self.assertEqual(result.fields[0].action, "filled_text")

    async def test_typeahead_search_chooses_only_live_exact_option_and_verifies(self):
        data = profile_data()
        data["qa_bank"]["how_did_you_hear_about_us"] = {"answer": "Referral"}

        class SearchBrowser:
            def __init__(self):
                self.stage = 0
                self.serial = 0
                self.calls = []

            async def observe(self):
                self.serial += 1
                options = ("Referral", "Recruiter") if self.stage == 1 else ()
                value = "Referral" if self.stage else None
                return ApplicationObservation(
                    f"search-{self.serial}", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("How did you hear about us?", ControlType.TYPEAHEAD,
                        options=options, required=True, current_value=value,
                        selection_confirmed=self.stage == 2,
                        target_ref=f"search-ref-{self.serial}"),),
                    navigation_controls=(NavigationControl("Submit Application", NavigationKind.SUBMIT,
                        f"submit-{self.serial}"),))

            async def search_options(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append(("search", action.target_ref, action.answer.value))
                self.stage = 1
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def choose_option(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append(("choose", action.target_ref, action.answer.value))
                self.stage = 2
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def activate_navigation(self, *_):
                raise AssertionError("final submit must remain human-only")

        browser = SearchBrowser()
        session = ApplicationSession("application-1", "https://example.test/apply")
        result = await ApplicationController(browser, DeterministicAnswerResolver(
            CandidateProfile.from_mapping(data))).run(session, current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual([call[0] for call in browser.calls], ["search", "choose"])
        self.assertNotEqual(browser.calls[0][1], browser.calls[1][1])
        self.assertEqual(browser.calls[1][2], "Referral")
        self.assertEqual(result.fields[0].action, "selected_option")
        self.assertFalse(result.fields[0].needs_review)

    async def test_unverified_typeahead_text_is_not_retyped_or_called_selected(self):
        data = profile_data()
        data["qa_bank"]["how_did_you_hear_about_us"] = {"answer": "Referral"}

        class ExistingTextBrowser:
            async def observe(self):
                return ApplicationObservation("current", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("How did you hear about us?", ControlType.TYPEAHEAD,
                        required=True, current_value="Referral", target_ref="source-ref"),),
                    navigation_controls=(NavigationControl("Next", NavigationKind.ADVANCE, "next-ref"),))

            async def search_options(self, *_):
                raise AssertionError("human-entered search text must not be overwritten")

            async def activate_navigation(self, *_):
                raise AssertionError("unverified required selection blocks navigation")

        result = await ApplicationController(ExistingTextBrowser(), DeterministicAnswerResolver(
            CandidateProfile.from_mapping(data))).run(
            ApplicationSession("application-1", "https://example.test/apply"),
            current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(result.fields[0].reason, "selection_unverified")

    async def test_successful_choice_command_with_only_search_text_is_not_verified(self):
        data = profile_data()
        data["qa_bank"]["how_did_you_hear_about_us"] = {"answer": "Referral"}
        class SearchOnlyBrowser:
            serial = 0
            chosen = 0
            async def observe(self):
                self.serial += 1
                return ApplicationObservation(f"choice-{self.serial}",
                    "https://example.test/apply", "Application",
                    questions=(QuestionObservation("How did you hear about us?",
                        ControlType.TYPEAHEAD, options=("Referral",), required=True,
                        current_value="Referral" if self.chosen else None,
                        selection_confirmed=False, target_ref=f"ref-{self.serial}"),),
                    navigation_controls=(NavigationControl("Submit Application",
                        NavigationKind.SUBMIT, f"submit-{self.serial}"),))
            async def choose_option(self, action, session):
                ActionPolicy.authorize(action, session)
                self.chosen += 1
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED),
                                           await self.observe())
            async def activate_navigation(self, *_):
                raise AssertionError("final submit must remain human-only")
        browser = SearchOnlyBrowser()
        result = await ApplicationController(browser, DeterministicAnswerResolver(
            CandidateProfile.from_mapping(data))).run(
                ApplicationSession("task", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(browser.chosen, 1)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertFalse(result.fields[0].verified)
        self.assertEqual(result.fields[0].reason, "unable_to_freshly_verify")

    async def test_fresh_answer_evidence_and_placeholders_across_control_types(self):
        for control in (ControlType.TEXT, ControlType.CHOICE, ControlType.UNKNOWN, ControlType.TOGGLE):
            for value in (None, "", "Select One", "Choose", "Please Select"):
                self.assertFalse(_has_answer(QuestionObservation("Question", control,
                                                               current_value=value)))
        for control, value in ((ControlType.TEXT, "entered"), (ControlType.CHOICE, "No"),
                               (ControlType.UNKNOWN, "Referral"), (ControlType.TOGGLE, "checked")):
            self.assertTrue(_has_answer(QuestionObservation("Question", control,
                                                            current_value=value)))

    async def test_closed_state_choice_reveals_live_options_once_before_selecting(self):
        data = profile_data()
        data["personal_info"]["address"]["state"] = "California"
        class ChoiceBrowser:
            def __init__(self, expose=True):
                self.expose = expose
                self.opened = False
                self.value = None
                self.counter = 0
                self.calls = []

            async def observe(self):
                self.counter += 1
                options = ("California", "Colorado") if self.opened and self.expose else ()
                observed = ApplicationObservation(
                    f"state-{self.counter}", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("State", ControlType.CHOICE,
                        options=options, required=True, current_value=self.value or "Select One",
                        target_ref=f"state-ref-{self.counter}"),),
                    navigation_controls=(NavigationControl("Submit application", NavigationKind.SUBMIT,
                        f"submit-{self.counter}"),))
                return observed

            async def reveal_options(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append("reveal")
                self.opened = True
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def choose_option(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append(("choose", action.answer.value))
                self.value = action.answer.value
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def fill_text(self, *_):
                raise AssertionError("state choice is not text")

            async def activate_navigation(self, *_):
                raise AssertionError("final submit is human-only")

        for expose, stop, expected in ((True, ControllerStop.READY_FOR_REVIEW,
                                        ["reveal", ("choose", "California")]),
                                       (False, ControllerStop.NEEDS_REVIEW, ["reveal"])):
            browser = ChoiceBrowser(expose)
            result = await ApplicationController(
                browser, DeterministicAnswerResolver(CandidateProfile.from_mapping(data))).run(
                    ApplicationSession("application-1", "https://example.test/apply"),
                    current_page_only=True)
            self.assertEqual(result.stop, stop)
            self.assertEqual(browser.calls, expected)
            self.assertEqual(result.fields[0].verified, expose)

    async def test_configured_radio_no_is_selected_once_and_verified(self):
        data = profile_data()
        data["qa_bank"]["employment.previously_worked_here"] = {
            "answer": False, "scope": "application", "application_id": "application-1"}
        class RadioBrowser:
            def __init__(self):
                self.counter = 0
                self.selected = None
                self.actions = []

            async def observe(self):
                self.counter += 1
                self.current = ApplicationObservation(
                    f"radio-{self.counter}", "https://example.test/apply", "Application",
                    questions=(QuestionObservation("Have you previously worked here?", ControlType.CHOICE,
                        options=("Yes", "No"), required=True, current_value=self.selected,
                        target_ref=f"radio-ref-{self.counter}"),),
                    navigation_controls=(NavigationControl("Submit application", NavigationKind.SUBMIT,
                                                           f"submit-{self.counter}"),))
                return self.current

            async def choose_option(self, action, session):
                ActionPolicy.authorize(action, session)
                self.actions.append(("choose", action.answer.value, action.observation_id))
                self.selected = action.answer.value
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

            async def fill_text(self, *_):
                raise AssertionError("radio must not be text filled")

            async def activate_navigation(self, *_):
                raise AssertionError("final submission must remain human-only")

        browser = RadioBrowser()
        result = await ApplicationController(
            browser, DeterministicAnswerResolver(CandidateProfile.from_mapping(data))).run(
                ApplicationSession("application-1", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual([(action, answer) for action, answer, _ in browser.actions], [("choose", "No")])
        self.assertTrue(result.fields[0].verified)

    async def test_known_checkbox_is_clicked_once_and_freshly_verified(self):
        class ToggleBrowser:
            checked = False
            count = 0
            clicks = 0

            async def observe(self):
                self.count += 1
                return ApplicationObservation(f"toggle-{self.count}", "https://example.test/apply",
                    "Application", questions=(QuestionObservation(
                        "Are you authorized to work in the US?", ControlType.TOGGLE,
                        required=True, current_value="checked" if self.checked else None,
                        target_ref=f"toggle-ref-{self.count}"),),
                    navigation_controls=(NavigationControl(
                        "Submit Application", NavigationKind.SUBMIT, f"submit-{self.count}"),))

            async def toggle(self, action, session):
                ActionPolicy.authorize(action, session)
                self.clicks += 1
                self.checked = True
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

        browser = ToggleBrowser()
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("toggle", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(browser.clicks, 1)
        self.assertEqual(result.fields[0].action, "selected_toggle")
        self.assertTrue(result.fields[0].verified)

    async def test_snapshot_field_coverage_fills_identity_and_reviews_custom_controls(self):
        class Browser:
            def __init__(self):
                self.values = {}
                self.calls = []
                self.counter = 0

            def observation(self):
                self.counter += 1
                first = f': {self.values["First Name"]}' if "First Name" in self.values else ""
                last = f': {self.values["Last Name"]}' if "Last Name" in self.values else ""
                text = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - generic: How Did You Hear About Us?*
  - textbox [ref=e3]
  - group "Have you previously worked here?" [required] [ref=node-group]:
    - radio "Yes" [ref=node-yes]
    - radio "No" [ref=node-no]
  - textbox "First Name" [required] [ref=e5]FIRST
  - textbox "Last Name" [required] [ref=e6]LAST
  - checkbox "I have a preferred name" [ref=e7]
  - combobox "State" [required] [ref=e8]: Select One
  - button "Save and Continue" [ref=e11]
```
'''.replace('FIRST', first).replace('LAST', last)
                self.last = SnapshotNormalizer().normalize(text, f"obs-{self.counter}").observation
                return self.last

            async def observe(self):
                self.calls.append("observe")
                return self.observation()

            async def fill_text(self, action, session):
                ActionPolicy.authorize(action, session)
                label = next(q.label for q in self.last.questions if q.target_ref == action.target_ref)
                self.calls.append(("fill", label, action.observation_id))
                self.values[label] = action.answer.value
                return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self.observation())

            async def choose_option(self, *_):
                raise AssertionError("unanswered custom choice must not be selected")

            async def activate_navigation(self, *_):
                raise AssertionError("current page must not advance or submit")

        browser = Browser()
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "https://example.test/apply"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual([(call[0], call[1]) for call in browser.calls if isinstance(call, tuple)],
                         [("fill", "First Name"), ("fill", "Last Name")])
        self.assertEqual(len(result.fields), 6)
        self.assertTrue(all(field.verified or field.needs_review or
                            field.action in {"optional_skipped", "manual_complete"}
                            for field in result.fields))
        by_label = {field.question.label: field for field in result.fields}
        self.assertTrue(by_label["First Name"].verified)
        self.assertTrue(by_label["Last Name"].verified)
        self.assertEqual(by_label["How Did You Hear About Us?"].reason, "unsupported_control")
        self.assertEqual(by_label["I have a preferred name"].reason, "no_safe_answer")
        self.assertEqual(by_label["State"].reason, "live_options_unavailable")
        self.assertEqual([call for call in browser.calls if call == "observe"], ["observe"])

    async def test_disappearing_conditional_field_is_not_reported_as_current_question(self):
        class DisappearingBrowser(PageBrowser):
            def observation(self):
                if self.values:
                    self.extra_fields = ()
                return super().observation()

        browser = DisappearingBrowser(extra_fields=(
            ("Conditional custom question", ControlType.UNKNOWN),))
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"),
            current_page_only=True)
        self.assertNotIn("Conditional custom question", [field.question.label for field in result.fields])
        self.assertEqual(len(result.fields), 5)

    async def test_redirect_to_remote_http_stops_before_another_fill(self):
        class RedirectBrowser(PageBrowser):
            def observation(self):
                observed = super().observation()
                if self.values:
                    observed = replace(observed, location="http://example.test/apply")
                    self.last = observed
                return observed

        browser = RedirectBrowser()
        session = ApplicationSession("fixture", "http://127.0.0.1/fixture")
        result = await ApplicationController(browser, resolver()).run(
            session, current_page_only=True, require_trusted_location=True)
        self.assertEqual(result.stop, ControllerStop.FAILED)
        self.assertEqual(result.reason, "insecure_page")
        self.assertEqual(browser.calls.count("fill"), 1)

    async def test_terminal_submit_does_not_prevent_safe_fill(self):
        browser = PageBrowser()
        session = ApplicationSession("fixture", "http://127.0.0.1/fixture")
        result = await ApplicationController(browser, resolver()).run(session, current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.values["First name"], "Ada")
        self.assertEqual(browser.values["Last name"], "Lovelace")
        self.assertEqual(browser.values["Email"], "ada@example.test")
        self.assertEqual(browser.values["Are you authorized to work in the US?"], "Yes")
        self.assertEqual([f.reason for f in result.fields if f.needs_review], ["no_safe_answer"])
        self.assertEqual(browser.calls.count("fill") + browser.calls.count("choose"), 4)
        self.assertEqual(len(session.observations), 5)
        self.assertEqual(set(browser.calls), {"observe", "fill", "choose"})

    async def test_conflict_and_unverified_attempt_never_become_verified(self):
        browser = PageBrowser(failed_verify=True)
        browser.values["Last name"] = "Conflicting"
        session = ApplicationSession("fixture", "http://127.0.0.1/fixture")
        result = await ApplicationController(browser, resolver()).run(session, current_page_only=True)
        by_label = {f.question.label: f for f in result.fields}
        self.assertEqual(by_label["First name"].reason, "unable_to_freshly_verify")
        self.assertFalse(by_label["First name"].verified)
        self.assertEqual(by_label["Last name"].action, "manual_complete")
        self.assertIsNone(by_label["Last name"].reason)
        self.assertFalse(by_label["Last name"].verified)
        self.assertEqual(browser.values["Last name"], "Conflicting")

    async def test_manual_value_reassessed_without_auto_provenance(self):
        browser = PageBrowser()
        first = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
        browser.values["Fixture clearance code"] = "owner supplied"
        second = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(second.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(next(f.action for f in second.fields if f.question.label == "Fixture clearance code"),
                         "manual_complete")
        self.assertEqual(browser.calls.count("fill"), 3)

    async def test_safe_advance_is_exactly_once_and_final_submit_is_never_clicked(self):
        browser = PageBrowser(terminal=False)
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(browser.calls.count("advance"), 1)
        self.assertEqual(result.session.current_observation.heading, "Review application")
        self.assertNotIn("submit", browser.calls)

    async def test_required_review_blocks_forward_but_optional_review_does_not(self):
        required = PageBrowser(terminal=False, extra_fields=(("Required custom", ControlType.UNKNOWN, True),))
        required.values["Fixture clearance code"] = "owner supplied"
        blocked = await ApplicationController(required, resolver()).run(
            ApplicationSession("required", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(blocked.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(required.calls.count("advance"), 0)
        optional = PageBrowser(terminal=False, extra_fields=(("Optional custom", ControlType.UNKNOWN, False),))
        optional.values["Fixture clearance code"] = "owner supplied"
        advanced = await ApplicationController(optional, resolver()).run(
            ApplicationSession("optional", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(advanced.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(optional.calls.count("advance"), 1)
        self.assertEqual(next(f.reason for f in advanced.fields if f.question.label == "Optional custom"),
                         "unsupported_control")

    async def test_unknown_requiredness_unsupported_is_review_only(self):
        browser = PageBrowser(terminal=False, extra_fields=(("Unknown custom", ControlType.UNKNOWN, None),))
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("unknown", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(browser.calls.count("advance"), 1)
        field = next(field for field in result.fields if field.question.label == "Unknown custom")
        self.assertEqual(field.reason, "unsupported_control")

    async def test_answered_unsupported_control_is_satisfied_before_capability_check(self):
        browser = PageBrowser(terminal=False, extra_fields=(("Required custom", ControlType.UNKNOWN, True),))
        browser.values["Fixture clearance code"] = "owner supplied"
        browser.values["Required custom"] = "manual choice"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("answered", "http://127.0.0.1/fixture"), current_page_only=True)
        field = next(field for field in result.fields if field.question.label == "Required custom")
        self.assertEqual((field.action, field.reason), ("manual_complete", None))
        self.assertEqual(result.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(browser.calls.count("advance"), 1)
        self.assertNotIn("submit", browser.calls)

    async def test_site_validation_marks_newly_required_field_without_reclick(self):
        class ValidatingBrowser(PageBrowser):
            async def activate_navigation(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append("advance")
                observed = self.observation()
                required = QuestionObservation("Employer-required answer", ControlType.UNKNOWN,
                                               required=True, target_ref=f"{observed.observation_id}-required")
                updated = replace(observed, questions=(*observed.questions, required),
                                  validation_messages=("Complete the required answer",))
                self.last = updated
                return BrowserActionResult(ActionOutcome(ActionStatus.VALIDATION_BLOCKED), updated)

        browser = ValidatingBrowser(terminal=False)
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("validation", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.VALIDATION_BLOCKED)
        self.assertEqual(browser.calls.count("advance"), 1)
        issue = next(f for f in result.fields if f.question.label == "Employer-required answer")
        self.assertEqual(issue.reason, "site_validation_required")

    async def test_ambiguous_terminal_navigation_never_clicks(self):
        browser = PageBrowser(ambiguous=True)
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertNotIn("advance", browser.calls)

    async def test_optional_unknown_skipped_but_unsupported_and_narrative_review(self):
        browser = PageBrowser(unknown_required=False, extra_fields=(
            ("Consent to background check", ControlType.TOGGLE),
            ("Why do you want to work here?", ControlType.TEXT),
            ("Resume", ControlType.FILE)))
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        fields = {f.question.label: f for f in result.fields}
        self.assertEqual(fields["Fixture clearance code"].action, "optional_skipped")
        self.assertEqual(fields["Consent to background check"].reason, "no_safe_answer")
        self.assertEqual(fields["Why do you want to work here?"].reason, "narrative_deferred")
        self.assertEqual(fields["Resume"].reason, "known_document_unavailable")
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)

    async def test_optional_human_owned_controls_outrank_optional_skip(self):
        browser = PageBrowser(unknown_required=False, extra_fields=(
            ("Consent to background check", ControlType.TOGGLE, False),
            ("Resume", ControlType.FILE, False),
            ("Why do you want to work here?", ControlType.TEXT, False),
            ("Gender", ControlType.CHOICE, False),
            ("Work authorization", ControlType.TEXT, False)))
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        fields = {field.question.label: field for field in result.fields}
        self.assertEqual(fields["Fixture clearance code"].action, "optional_skipped")
        expected = {
            "Consent to background check": "no_safe_answer",
            "Resume": "known_document_unavailable",
            "Why do you want to work here?": "narrative_deferred",
            "Gender": "requires_human_review",
            "Work authorization": "ambiguous_semantic_mapping",
        }
        self.assertEqual({label: fields[label].reason for label in expected}, expected)
        self.assertTrue(all(fields[label].needs_review and
                            fields[label].action != "optional_skipped" for label in expected))
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)

    async def test_optional_unsupported_toggle_does_not_block_terminal_handoff(self):
        browser = PageBrowser(extra_fields=(("Consent to background check", ControlType.TOGGLE, False),))
        browser.values["Fixture clearance code"] = "owner supplied"
        result = await ApplicationController(browser, resolver()).run(
            ApplicationSession("fixture", "http://127.0.0.1/fixture"), current_page_only=True)
        toggle = next(field for field in result.fields if field.question.label == "Consent to background check")
        self.assertEqual((toggle.action, toggle.reason), ("deferred", "no_safe_answer"))
        self.assertTrue(toggle.needs_review)
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(browser.calls.count("fill") + browser.calls.count("choose"), 4)


class FakeWindows:
    def __init__(self, browser):
        self.browser = browser
        self.task_id = None

    def open_count(self): return int(self.task_id is not None)
    def window_for_task(self, task_id): return "window" if self.task_id == task_id else None
    def exists(self, window_id): return window_id == "window"
    def allocate(self, task_id): self.task_id = task_id; return "window"
    def browser_for(self, window_id): return self.browser
    def opened_url(self, window_id): return "http://127.0.0.1/fixture"
    def run_browser(self, window_id, coroutine): return asyncio.run(coroutine)
    def bring_to_front(self, window_id): pass
    def release(self, window_id): self.task_id = None


class RuntimePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BatchStore(Path(self.temp.name) / "test.sqlite3")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(profile_data()), encoding="utf-8")
        self.browser = PageBrowser()
        self.windows = FakeWindows(self.browser)
        self.worker = LaunchAndLoginWorker(self.store, ApplicationLauncher(self.windows),
                                           LocalLoginConfiguration(config))
        self.scheduler = ApplicationScheduler(self.store, self.worker, self.windows,
                                              max_open_applications=1)
        run = self.store.create_run(("fixture",), 1)
        listing, _ = self.store.register_listing(ListingInput(
            "fixture", "Fixture", "Engineer", "http://127.0.0.1/fixture",
            application_url="http://127.0.0.1/fixture"))
        self.task = self.store.queue_task(run.id, listing.id)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_explicit_resume_reconciles_fresh_fill_before_response(self):
        self.scheduler.step()
        self.browser.values["First name"] = ""
        before = self.browser.calls.count("fill")
        control = LocalControlPlane(self.store, self.scheduler)
        response = control.resume_and_reconcile(self.task.id)
        self.assertEqual(response["status"], "human_paused")
        self.assertEqual(self.browser.calls.count("fill"), before + 1)
        self.assertEqual(self.browser.values["First name"], "Ada")
        entry = next(entry for entry in self.store.current_report_entries(self.task.id)
                     if entry.visible_label == "First name")
        self.assertEqual(entry.verification, Verification.VERIFIED)
        self.assertEqual(entry.review_state, ReviewState.NOT_REQUIRED)
        card = next(entry for entry in control.get_application_report(self.task.id)["entries"]
                    if entry["visible_label"] == "First name")
        self.assertEqual(card["report_group"], "completed")
        self.assertNotIn("submit", self.browser.calls)

    def test_queued_resume_reconciles_on_idle_worker_without_second_resume(self):
        from jobagent.control_plane_stdio import progress_running_batch

        self.scheduler.step()
        self.browser.values["First name"] = ""
        control = LocalControlPlane(self.store, self.scheduler)
        response = control.resume_application(self.task.id)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(self.browser.values["First name"], "")
        progress_running_batch(self.store, self.scheduler, set())
        entry = next(entry for entry in self.store.current_report_entries(self.task.id)
                     if entry.visible_label == "First name")
        self.assertEqual(entry.verification, Verification.VERIFIED)
        self.assertEqual(entry.review_state, ReviewState.NOT_REQUIRED)
        card = next(entry for entry in control.get_application_report(self.task.id)["entries"]
                    if entry["visible_label"] == "First name")
        self.assertEqual(card["report_group"], "completed")
        self.assertEqual(len(self.store.human_actions("task", self.task.id)), 1)
        self.assertNotIn("submit", self.browser.calls)

    def test_timeout_after_fresh_actions_keeps_last_observed_field_truth(self):
        real_run = self.windows.run_browser
        calls = 0
        def timeout_after_controller(window_id, coroutine):
            nonlocal calls
            calls += 1
            value = real_run(window_id, coroutine)
            if calls == 2:
                raise RuntimeError("managed browser operation timed out")
            return value
        self.windows.run_browser = timeout_after_controller
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(step.task.blocker, Blocker.BROWSER_TIMEOUT)
        self.assertEqual(self.browser.values["First name"], "Ada")
        current = {entry.visible_label: entry for entry in
                   self.store.current_report_entries(self.task.id)}
        self.assertEqual(current["First name"].review_state, ReviewState.NOT_REQUIRED)
        self.assertEqual(current["Fixture clearance code"].review_state, ReviewState.PENDING)
        self.assertNotIn("submit", self.browser.calls)

    def test_validation_required_field_is_durable_needs_answer(self):
        class ValidatingBrowser(PageBrowser):
            async def activate_navigation(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append("advance")
                observed = self.observation()
                required = QuestionObservation("Employer-required answer", ControlType.UNKNOWN,
                                               required=True, target_ref=f"{observed.observation_id}-required")
                updated = replace(observed, questions=(*observed.questions, required),
                                  validation_messages=("Complete the required answer",))
                self.last = updated
                return BrowserActionResult(ActionOutcome(ActionStatus.VALIDATION_BLOCKED), updated)

        self.browser = ValidatingBrowser(terminal=False)
        self.windows.browser = self.browser
        self.browser.values["Fixture clearance code"] = "owner supplied"
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(step.task.blocker.value, "needs_answer")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertIn("site_validation_required",
                      [entry.reason for entry in self.store.get_report(self.task.id).entries])

    def test_unanswered_prior_employment_choice_is_attention_without_action(self):
        label = ("Have you previously worked for or are you currently working for "
                 "Workday as an employee or contractor?")
        self.browser.extra_fields = ((label, ControlType.CHOICE, True),)
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.browser.calls.count("choose"), 1)  # work authorization only
        entry = next(item for item in self.store.current_report_entries(self.task.id)
                     if item.visible_label == label)
        self.assertEqual(entry.reason, "no_safe_answer")
        view = LocalControlPlane(self.store, self.scheduler).get_application_report(self.task.id)
        card = next(item for item in view["entries"] if item["visible_label"] == label)
        self.assertEqual(card["report_group"], "needs_attention")

    def test_report_survives_failure_after_a_verified_fill(self):
        class ErrorAfterFillBrowser(PageBrowser):
            async def fill_text(self, action, session):
                if self.calls.count("fill") == 1:
                    raise RuntimeError("password=private-value ref=secret-browser-ref")
                return await super().fill_text(action, session)
        self.browser = ErrorAfterFillBrowser()
        self.windows.browser = self.browser
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.FAILED)
        self.assertEqual(step.task.current_page_or_step, "Application details")
        current = {entry.visible_label: entry for entry in self.store.current_report_entries(self.task.id)}
        self.assertEqual(current["First name"].verification, Verification.VERIFIED)
        self.assertEqual(current["Last name"].review_state, ReviewState.PENDING)
        self.assertEqual(current["Fixture clearance code"].reason, "no_safe_answer")
        view = LocalControlPlane(self.store, self.scheduler).get_application_report(self.task.id)
        self.assertGreater(view["needs_attention_count"], 0)
        self.assertEqual(len(view["entries"]), len(current))
        event = self.store.failure_events(self.task.id)[0]
        self.assertEqual((event.stage, event.category), ("fill", "unexpected_exception"))
        self.assertNotIn("private-value", str(event))

    def test_timeout_retains_observed_cards_without_claiming_fill(self):
        real_run = self.windows.run_browser
        calls = 0
        def timeout_on_controller(window_id, coroutine):
            nonlocal calls
            calls += 1
            if calls == 2:
                coroutine.close()
                raise RuntimeError("managed browser operation timed out")
            return real_run(window_id, coroutine)
        self.windows.run_browser = timeout_on_controller
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(step.task.blocker.value, "browser_timeout")
        self.assertEqual(self.windows.window_for_task(self.task.id), "window")
        current = {entry.visible_label: entry for entry in self.store.current_report_entries(self.task.id)}
        self.assertIn("First name", current)
        self.assertIn("Fixture clearance code", current)
        self.assertFalse(any(entry.verification is Verification.VERIFIED for entry in current.values()))
        event = self.store.failure_events(self.task.id)[0]
        self.assertEqual((event.stage, event.category, event.detail, event.mode),
                         ("observe_snapshot", "timeout", "Managed browser operation timed out", "run"))
        detail = LocalControlPlane(self.store, self.scheduler).get_application(self.task.id)
        self.assertTrue(detail["resume_available"])
        self.assertTrue(detail["window_available"])
        self.assertIn("Resume", detail["blocker_label"])
        self.assertEqual(self.browser.calls, ["observe"])
        before = self.browser.last.observation_id
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        resumed = self.scheduler.step()
        self.assertNotEqual(self.browser.last.observation_id, before)
        self.assertNotEqual(resumed.task.status, TaskStatus.FAILED)

    def test_locate_control_timeout_stops_actions_and_retains_managed_page(self):
        class TimedOutChoiceBrowser(PageBrowser):
            async def reveal_options(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append("reveal")
                raise TimeoutError("private-value secret-browser-ref")

        self.browser = TimedOutChoiceBrowser(extra_fields=(("State", ControlType.CHOICE, True),))
        self.windows.browser = self.browser
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(step.task.blocker.value, "browser_timeout")
        self.assertEqual(self.windows.window_for_task(self.task.id), "window")
        self.assertEqual(self.browser.calls[-1], "reveal")
        self.assertNotIn("advance", self.browser.calls)
        self.assertNotIn("submit", self.browser.calls)
        before_calls = list(self.browser.calls)
        self.assertIsNone(self.scheduler.step())
        self.assertEqual(self.browser.calls, before_calls)
        event = self.store.failure_events(self.task.id)[0]
        self.assertEqual((event.stage, event.category, event.detail),
                         ("locate_control", "timeout", "Managed browser operation timed out"))
        self.assertNotIn("private-value", str(event))
        self.assertNotIn("secret-browser-ref", str(event))
        self.assertIn("First name", {entry.visible_label for entry in
                                     self.store.current_report_entries(self.task.id)})
        state = next(entry for entry in self.store.current_report_entries(self.task.id)
                     if entry.visible_label == "State")
        before_resolution = list(self.browser.calls)
        resolved = self.store.resolve_field_by_human(
            state.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual(resolved.action, "human_resolved")
        self.assertEqual(self.browser.calls, before_resolution)

    def test_report_annotations_and_resume(self):
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        report = self.store.get_report(self.task.id)
        self.assertEqual(len(report.entries), 5)
        self.assertEqual(len([e for e in report.entries if e.verification is Verification.VERIFIED]), 4)
        self.assertEqual(len([e for e in report.entries if e.review_state is ReviewState.PENDING]), 1)
        self.assertEqual(self.browser.annotations["First name"], "verified")
        self.assertEqual(self.browser.annotations["Fixture clearance code"], "needs-review")
        self.assertEqual(report.entries[-1].reason, "no_safe_answer")
        self.assertNotIn("obs-", repr(report))
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
        self.browser.values["Fixture clearance code"] = "owner supplied"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        third = self.scheduler.step()
        self.assertEqual(third.task.status, TaskStatus.READY_FOR_REVIEW)
        self.assertEqual(self.browser.annotations["Fixture clearance code"], "clear")
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
        self.assertEqual(self.store.get_report(self.task.id).entries[-1].action, "manual_complete")
        self.assertEqual(self.browser.calls.count("fill"), 3)
        self.assertFalse(any(e.provenance is Provenance.HUMAN_PROVIDED for e in report.entries))

    def test_successful_fill_command_without_fresh_value_is_not_reported_verified(self):
        self.browser = PageBrowser(failed_verify=True)
        self.windows.browser = self.browser
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        first = next(entry for entry in self.store.current_report_entries(self.task.id)
                     if entry.visible_label == "First name")
        self.assertEqual(first.review_state, ReviewState.PENDING)
        self.assertEqual(first.reason, "unable_to_freshly_verify")
        self.assertEqual(first.verification, Verification.NOT_ATTEMPTED)
        self.assertEqual(self.browser.calls.count("fill"), 3)
        self.assertNotIn("advance", self.browser.calls)
        self.assertNotIn("submit", self.browser.calls)

    def test_fresh_human_value_differing_from_profile_clears_required_blocker(self):
        self.browser.terminal = False
        self.browser.values["First name"] = "Human entered name"
        self.browser.values["Fixture clearance code"] = "Human entered answer"
        step = self.scheduler.step()
        self.assertEqual(step.outcome.page_or_step, "page_advanced")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertNotIn("submit", self.browser.calls)
        first = next(entry for entry in self.store.current_report_entries(self.task.id)
                     if entry.visible_label == "First name")
        self.assertEqual((first.action, first.verification, first.review_state),
                         ("manual_complete", Verification.NOT_ATTEMPTED,
                          ReviewState.NOT_REQUIRED))
        self.assertEqual(self.browser.values["First name"], "Human entered name")
        report = LocalControlPlane(self.store, self.scheduler).get_application_report(self.task.id)
        card = next(entry for entry in report["entries"] if entry["visible_label"] == "First name")
        self.assertEqual(card["report_group"], "completed")
        self.assertFalse(card["is_blocking"])

    def test_fresh_satisfied_field_replaces_pending_card_even_if_next_action_times_out(self):
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        pending = next(entry for entry in self.store.current_report_entries(self.task.id)
                       if entry.visible_label == "Fixture clearance code")
        self.assertEqual(pending.review_state, ReviewState.PENDING)
        self.browser.values["Fixture clearance code"] = "Human entered answer"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        real_run = self.windows.run_browser
        calls = 0
        def timeout_after_fresh_observation(window_id, coroutine):
            nonlocal calls
            calls += 1
            if calls == 2:
                coroutine.close()
                raise RuntimeError("managed browser operation timed out")
            return real_run(window_id, coroutine)
        self.windows.run_browser = timeout_after_fresh_observation
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(step.task.blocker.value, "browser_timeout")
        current = next(entry for entry in self.store.current_report_entries(self.task.id)
                       if entry.visible_label == "Fixture clearance code")
        self.assertEqual(current.action, "manual_complete")
        self.assertEqual(current.review_state, ReviewState.NOT_REQUIRED)
        self.assertEqual(current.verification, Verification.NOT_ATTEMPTED)
        report = LocalControlPlane(self.store, self.scheduler).get_application_report(self.task.id)
        card = next(entry for entry in report["entries"]
                    if entry["visible_label"] == "Fixture clearance code")
        self.assertEqual(card["report_group"], "completed")
        self.assertFalse(card["is_blocking"])
        self.assertEqual(len([entry for entry in report["entries"]
                              if entry["visible_label"] == "Fixture clearance code"]), 1)
        self.assertEqual(self.browser.calls.count("advance"), 0)

    def test_human_changed_answer_reconciles_without_erasing_verified_history(self):
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        old = next(entry for entry in self.store.get_report(self.task.id).entries
                   if entry.visible_label == "First name")
        self.assertEqual(old.verification, Verification.VERIFIED)
        self.browser.values["First name"] = "Human changed name"
        self.browser.values["Fixture clearance code"] = "Human entered answer"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.READY_FOR_REVIEW)
        current = next(entry for entry in self.store.current_report_entries(self.task.id)
                       if entry.visible_label == "First name")
        self.assertEqual(current.action, "manual_complete")
        self.assertEqual(current.verification, Verification.NOT_ATTEMPTED)
        history = [entry for entry in self.store.get_report(self.task.id).entries
                   if entry.visible_label == "First name"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].id, old.id)
        self.assertEqual(history[0].verification, Verification.VERIFIED)

    def test_page_advance_timeout_resumes_on_actual_new_page_without_reclick(self):
        class TimedOutAfterNavigation(PageBrowser):
            async def activate_navigation(self, action, session):
                ActionPolicy.authorize(action, session)
                self.calls.append("advance")
                self.page += 1
                raise TimeoutError("private browser detail")

        self.browser = TimedOutAfterNavigation(terminal=False)
        self.windows.browser = self.browser
        self.browser.values["Fixture clearance code"] = "Human entered answer"
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(first.task.blocker.value, "browser_timeout")
        self.assertEqual(self.store.failure_events(self.task.id)[0].stage, "page_advance")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        old_observation = self.browser.last.observation_id
        before_resume = list(self.browser.calls)
        self.assertIsNone(self.scheduler.step())
        self.assertEqual(self.browser.calls, before_resume)
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.READY_FOR_REVIEW)
        self.assertEqual(second.task.current_page_or_step, "Review application")
        self.assertNotEqual(self.browser.last.observation_id, old_observation)
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertNotIn("submit", self.browser.calls)

    def test_human_resolution_survives_fresh_resume_and_expires_after_advance(self):
        self.browser.terminal = False
        self.browser.extra_fields = (("Prior employment question", ControlType.UNKNOWN, True),)
        self.browser.values["Fixture clearance code"] = "owner supplied"
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(first.task.blocker.value, "unsupported_control")
        pending = next(entry for entry in self.store.current_report_entries(self.task.id)
                       if entry.visible_label == "Prior employment question")
        before_calls = list(self.browser.calls)
        resolved = self.store.resolve_field_by_human(
            pending.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.assertEqual(self.browser.calls, before_calls)
        self.assertEqual(resolved.verification, Verification.NOT_ATTEMPTED)
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.outcome.page_or_step, "page_advanced")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertGreater(self.browser.counter, len(before_calls))
        self.assertEqual(self.store.get_report_entry(resolved.id).action,
                         "human_resolved_prior_page")
        third = self.scheduler.step()
        self.assertEqual(third.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(third.task.blocker.value, "unsupported_control")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertNotIn("submit", self.browser.calls)

    def test_attestation_on_one_checkpoint_never_satisfies_the_next_step(self):
        # Workday-like: every step shares the job-title heading; only the step
        # heading changes. The owner advances outside automation.
        self.browser.terminal = False
        self.browser.heading = "Software Engineer"
        self.browser.checkpoint = "My Information"
        self.browser.extra_fields = (("Prior employment question", ControlType.UNKNOWN, True),)
        self.browser.values["Fixture clearance code"] = "owner supplied"
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(first.task.current_page_or_step, "Software Engineer › My Information")
        pending = next(entry for entry in self.store.current_report_entries(self.task.id)
                       if entry.visible_label == "Prior employment question")
        resolved = self.store.resolve_field_by_human(
            pending.id, authorization=owner(HumanAction.RESOLVE_FIELD))
        self.browser.checkpoint = "Application Questions"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(second.task.current_page_or_step, "Software Engineer › Application Questions")
        self.assertEqual(self.browser.calls.count("advance"), 0)
        self.assertNotIn("submit", self.browser.calls)
        current = [entry for entry in self.store.current_report_entries(self.task.id)
                   if entry.visible_label == "Prior employment question"]
        on_b = next(entry for entry in current
                    if entry.page_or_step == "Software Engineer › Application Questions")
        self.assertEqual(on_b.review_state, ReviewState.PENDING)
        self.assertFalse(self.store.active_manual_resolutions(
            self.task.id, "Software Engineer › Application Questions"))
        # Page A's attestation and its audit record remain readable history.
        self.assertEqual(self.store.get_report_entry(resolved.id).page_or_step,
                         "Software Engineer › My Information")
        self.assertEqual(self.store.human_actions("report_entry", resolved.id)[0].action,
                         HumanAction.RESOLVE_FIELD)

    def test_required_unsupported_manual_resume_clears_old_blocker_and_advances_once(self):
        self.browser.terminal = False
        self.browser.extra_fields = (("How Did You Hear About Us?", ControlType.UNKNOWN, True),)
        self.browser.values["Fixture clearance code"] = "owner supplied"
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(first.task.blocker.value, "unsupported_control")
        self.assertEqual(self.browser.calls.count("advance"), 0)
        initial = next(entry for entry in self.store.get_report(self.task.id).entries
                       if entry.visible_label == "How Did You Hear About Us?")
        self.assertEqual(initial.reason, "unsupported_control")
        first_observation_id = self.browser.last.observation_id
        self.browser.values["How Did You Hear About Us?"] = "manual choice"
        self.scheduler.resume_application(self.task.id, owner(HumanAction.RESUME))
        second = self.scheduler.step()
        self.assertEqual(second.outcome.page_or_step, "page_advanced")
        self.assertIsNone(second.task.blocker)
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertGreaterEqual(self.browser.calls.count("observe"), 2)
        self.assertNotEqual(self.browser.last.observation_id, first_observation_id)
        self.assertNotIn("submit", self.browser.calls)
        settled = [entry for entry in self.store.get_report(self.task.id).entries
                   if entry.visible_label == "How Did You Hear About Us?"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0].id, initial.id)
        self.assertEqual(settled[0].action, "manual_complete")
        self.assertEqual(settled[0].reason, "unsupported_control")
        self.assertEqual(settled[0].review_state, ReviewState.NOT_REQUIRED)

    def test_safe_advance_keeps_current_page_report_and_fresh_next_page(self):
        self.browser.terminal = False
        self.browser.values["Fixture clearance code"] = "owner supplied"
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.FILLING)
        self.assertEqual(step.outcome.page_or_step, "page_advanced")
        self.assertEqual(self.browser.calls.count("advance"), 1)
        self.assertEqual(self.browser.last.heading, "Review application")
        self.assertNotIn("submit", self.browser.calls)

    def test_every_discovered_field_has_a_durable_report_outcome(self):
        self.browser.unknown_required = False
        self.browser.extra_fields = (
            ("How Did You Hear About Us?", ControlType.UNKNOWN, True),
            ("Unsupported input", ControlType.TOGGLE, False))
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        observed = self.browser.last.questions
        report = self.store.get_report(self.task.id).entries
        self.assertEqual(len(observed), 7)
        self.assertEqual(len(report), len(observed))
        self.assertEqual({entry.visible_label for entry in report},
                         {question.label for question in observed})
        decisions = {entry.visible_label: entry for entry in report}
        self.assertEqual(decisions["First name"].verification, Verification.VERIFIED)
        self.assertEqual(decisions["Fixture clearance code"].action, "optional_skipped")
        self.assertEqual(decisions["Fixture clearance code"].reason,
                         "optional_without_trusted_answer")
        self.assertEqual(decisions["How Did You Hear About Us?"].reason,
                         "unsupported_control")
        self.assertEqual(decisions["Unsupported input"].reason, "no_safe_answer")
        self.assertNotIn("advance", self.browser.calls)

    def test_requiredness_survives_report_storage_and_control_plane(self):
        self.browser.extra_fields = (
            ("Required custom", ControlType.UNKNOWN, True),
            ("Optional custom", ControlType.UNKNOWN, False),
            ("Unknown custom", ControlType.UNKNOWN, None))
        self.scheduler.step()
        entries = {entry.visible_label: entry for entry in self.store.get_report(self.task.id).entries}
        self.assertEqual(entries["Required custom"].requiredness, "required")
        self.assertEqual(entries["Optional custom"].requiredness, "optional")
        self.assertEqual(entries["Unknown custom"].requiredness, "unknown")
        from jobagent.control_plane import LocalControlPlane
        payload = LocalControlPlane(self.store, self.scheduler).get_application_report(self.task.id)
        wire = {entry["visible_label"]: entry for entry in payload["entries"]}
        self.assertEqual(wire["Required custom"]["requiredness"], "required")
        self.assertEqual(wire["Optional custom"]["requiredness"], "optional")
        self.assertEqual(wire["Unknown custom"]["requiredness"], "unknown")
        self.store.close()
        self.store = BatchStore(Path(self.temp.name) / "test.sqlite3")
        reloaded = {entry.visible_label: entry for entry in self.store.get_report(self.task.id).entries}
        self.assertEqual(reloaded["Required custom"].requiredness, "required")
        self.assertEqual(reloaded["Optional custom"].requiredness, "optional")
        self.assertEqual(reloaded["Unknown custom"].requiredness, "unknown")

    def test_ambiguous_navigation_has_report_reason(self):
        self.browser.ambiguous = True
        self.browser.values["Fixture clearance code"] = "owner supplied"
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertIn("ambiguous_navigation",
                      [e.reason for e in self.store.get_report(self.task.id).entries])

    def test_annotation_failure_cannot_cancel_safe_fill_or_report(self):
        async def fail_annotation(*_):
            self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)
            raise PermissionError("missing current target")
        self.browser.annotate_field = fail_annotation
        step = self.scheduler.step()
        self.assertEqual(step.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.browser.values["First name"], "Ada")
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 5)

    def test_optional_human_owned_fields_report_and_annotate_once(self):
        self.browser.values["Fixture clearance code"] = "owner supplied"
        self.browser.extra_fields = (
            ("Consent to background check", ControlType.TOGGLE, False),
            ("Resume", ControlType.FILE, False),
            ("Why do you want to work here?", ControlType.TEXT, False))
        first = self.scheduler.step()
        self.assertEqual(first.task.status, TaskStatus.READY_FOR_REVIEW)
        report = self.store.get_report(self.task.id)
        pending = {entry.visible_label: entry for entry in report.entries
                   if entry.review_state is ReviewState.PENDING}
        self.assertEqual({label: entry.reason for label, entry in pending.items()}, {
            "Consent to background check": "no_safe_answer",
            "Resume": "known_document_unavailable",
            "Why do you want to work here?": "narrative_deferred",
        })
        self.assertTrue(all(entry.provenance is Provenance.UNRESOLVED and
                            entry.verification is Verification.NOT_ATTEMPTED for entry in pending.values()))
        self.assertEqual(len(report.entries), 8)
        self.assertEqual(next(entry.action for entry in report.entries
                              if entry.visible_label == "Fixture clearance code"), "manual_complete")
        self.assertNotIn("obs-", repr(report))
        self.assertNotIn("owner supplied", repr(report))
        self.assertTrue(all(self.browser.annotations[label] == "needs-review" for label in pending))
        self.assertEqual(len(self.store.get_report(self.task.id).entries), 8)
        self.assertEqual(self.browser.calls.count("fill") + self.browser.calls.count("choose"), 4)
        self.assertTrue(all(self.browser.annotations[label] == "needs-review" for label in pending))


if __name__ == "__main__":
    unittest.main()
