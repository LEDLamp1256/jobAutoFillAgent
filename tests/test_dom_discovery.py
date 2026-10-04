"""Synthetic current-state evidence; no employer website or personal values."""

import unittest

from jobagent.dom_discovery import merge_dom_observation
from jobagent.controller import ApplicationController, ControllerStop
from jobagent.domain import (
    AnswerEvidence, ApplicationObservation, ApplicationSession, ControlType,
    NavigationControl, NavigationKind, QuestionObservation, current_field_diagnostic,
)
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from tests.test_resolution import profile_data


def observed(*questions):
    return ApplicationObservation("fresh", "https://example.test/apply", "Application",
                                  questions=questions)


def candidate(label, *, kind="choice", value=None, required=True, evidence="dom_associated",
              group=1, index=1, button=None, role="input"):
    return {"label": label, "kind": kind, "value": value, "required": required,
            "requiredEvidence": "aria_required" if required else None,
            "evidence": evidence, "groupIndex": group, "elementIndex": index,
            "buttonLabel": button, "role": role, "section": None,
            "selectedValues": [], "confirmed": False}


def payload(*items):
    return {"candidates": list(items), "rawActionableCount": len(items),
            "ignoredReasons": {}, "truncated": False}


class DOMDiscoveryTests(unittest.TestCase):
    def test_dom_only_required_state_empty_and_selected(self):
        for value, satisfied in (("Select One", False), ("California", True)):
            with self.subTest(value=value):
                page = merge_dom_observation(observed(), payload(candidate(
                    "State / Province", value=value, button=value)))
                self.assertEqual(len(page.questions), 1)
                state = page.questions[0]
                self.assertEqual(state.label, "State / Province")
                self.assertTrue(state.required)
                self.assertEqual(state.discovery_source, "dom_fallback")
                self.assertIsNone(state.target_ref)
                self.assertEqual(state.answer_state().satisfied, satisfied)
                self.assertEqual(current_field_diagnostic(
                    state, automation_supported=False, blocking=not satisfied)[
                        "current_value_present"], satisfied)
                self.assertEqual(page.discovery_summary.dom_recovered_field_count, 1)

    def test_empty_primary_input_merges_associated_phone_selection(self):
        primary = QuestionObservation("Country / Territory Phone Code", ControlType.UNKNOWN,
            required=True, target_ref="e1", current_value=None, raw_role="input")
        page = merge_dom_observation(observed(primary), payload(candidate(
            primary.label, value="Exampleland (+9)")))
        self.assertEqual(len(page.questions), 1)
        phone = page.questions[0]
        self.assertEqual(phone.current_value, "Exampleland (+9)")
        self.assertEqual(phone.answer_evidence, AnswerEvidence.DOM_ASSOCIATED)
        self.assertTrue(phone.answer_state().satisfied)
        self.assertTrue(current_field_diagnostic(
            phone, automation_supported=False, blocking=False)["current_value_present"])
        self.assertEqual(phone.discovery_source, "merged")

    def test_one_composite_input_and_button_becomes_one_unactionable_question(self):
        primary = QuestionObservation("Country / Territory Phone Code", ControlType.UNKNOWN,
            required=True, target_ref="input-ref", raw_role="input", occurrence=0)
        trigger = QuestionObservation("Country / Territory Phone Code", ControlType.CHOICE,
            required=True, target_ref="button-ref", raw_role="button", occurrence=1)
        page = merge_dom_observation(observed(primary, trigger), payload(candidate(
            primary.label, value="Exampleland (+9)")))
        self.assertEqual(len(page.questions), 1)
        self.assertTrue(page.questions[0].answer_state().satisfied)
        self.assertIsNone(page.questions[0].target_ref)
        self.assertEqual(page.questions[0].occurrence, 0)

    def test_referral_manual_answer_preserves_identity(self):
        before = QuestionObservation("How Did You Hear About Us?", ControlType.UNKNOWN,
            required=True, target_ref="old", current_value=None)
        after = merge_dom_observation(observed(before), payload(candidate(
            before.label, value="Example source", button="Example source"))).questions[0]
        self.assertEqual(before.identity(), after.identity())
        self.assertTrue(after.answer_state().satisfied)

    def test_checkbox_default_value_is_not_checked(self):
        prior = QuestionObservation("I have a preferred name", ControlType.UNKNOWN,
            current_value="on", required=False, target_ref="e1")
        empty = merge_dom_observation(observed(prior), payload(candidate(
            prior.label, kind="toggle", value="unchecked", required=False,
            evidence="checked_state", role="input"))).questions[0]
        self.assertEqual(empty.control_type, ControlType.TOGGLE)
        self.assertFalse(empty.answer_state().satisfied)
        self.assertFalse(current_field_diagnostic(
            empty, automation_supported=False, blocking=False)["current_value_present"])
        checked = merge_dom_observation(observed(), payload(candidate(
            prior.label, kind="toggle", value="checked", required=False,
            evidence="checked_state", role="input"))).questions[0]
        self.assertTrue(checked.answer_state().satisfied)

    def test_unique_accessibility_and_dom_field_merge_once(self):
        prior = QuestionObservation("First Name", ControlType.TEXT, target_ref="e1")
        page = merge_dom_observation(observed(prior), payload(candidate(
            "First Name", kind="text", value="Example", required=True,
            evidence="dom_value", group=-1)))
        self.assertEqual(len(page.questions), 1)
        self.assertEqual(page.questions[0].discovery_source, "merged")
        self.assertEqual(page.discovery_summary.dom_recovered_field_count, 0)

    def test_structural_disclosure_is_removed_from_accessibility_questions(self):
        heading = QuestionObservation("Contact", ControlType.CHOICE, raw_role="button",
            current_value="Contact")
        field = QuestionObservation("Phone Device Type", ControlType.CHOICE,
            current_value="Mobile", required=True)
        data = payload(candidate("Phone Device Type", value="Mobile", button="Mobile"))
        data["structuralLabels"] = ["Contact"]
        data["ignoredReasons"] = {"section_or_container": 1}
        page = merge_dom_observation(observed(heading, field), data)
        self.assertEqual([q.label for q in page.questions], ["Phone Device Type"])
        self.assertEqual(dict(page.discovery_summary.ignored_reasons)["section_or_container"], 1)

    def test_real_answer_is_not_removed_as_structural_heading(self):
        question = QuestionObservation("Contact", ControlType.CHOICE, raw_role="button",
            current_value="Mobile", required=True)
        data = payload()
        data["structuralLabels"] = ["Contact"]
        page = merge_dom_observation(observed(question), data)
        self.assertEqual(page.questions, (question,))

    def test_conflicting_real_values_are_not_silently_accepted(self):
        prior = QuestionObservation("State", ControlType.CHOICE, current_value="Pacific",
            answer_evidence=AnswerEvidence.SELECTED_OPTION, target_ref="e1")
        page = merge_dom_observation(observed(prior), payload(candidate(
            "State", value="Atlantic", evidence="dom_value")))
        self.assertTrue(page.questions[0].state_conflict)
        self.assertFalse(page.questions[0].answer_state().satisfied)

    def test_repeated_dom_only_labels_keep_distinct_occurrences(self):
        page = merge_dom_observation(observed(), payload(
            candidate("Job Title", kind="text", value="Example A", group=1),
            candidate("Job Title", kind="text", value="Example B", group=2)))
        self.assertEqual(len(page.questions), 2)
        self.assertNotEqual(page.questions[0].identity(), page.questions[1].identity())

    def test_repeated_dom_values_do_not_overwrite_one_accessibility_field(self):
        prior = QuestionObservation("Job Title", ControlType.TEXT, target_ref="e1")
        page = merge_dom_observation(observed(prior), payload(
            candidate("Job Title", kind="text", value="Example A", group=1),
            candidate("Job Title", kind="text", value="Example B", group=2)))
        self.assertEqual(len(page.questions), 1)
        self.assertIsNone(page.questions[0].current_value)
        self.assertEqual(dict(page.discovery_summary.ignored_reasons)["ambiguous_repeated_label"], 2)

    def test_navigation_repeater_and_submit_never_become_questions(self):
        page = merge_dom_observation(observed(), payload(
            candidate("Add", kind="button", button="Add", group=-1, index=1),
            candidate("Save and Continue", kind="button", button="Save and Continue", group=-1, index=2),
            candidate("Submit Application", kind="button", button="Submit Application", group=-1, index=3),
            candidate("State", value="Select One", group=4)))
        self.assertEqual([q.label for q in page.questions], ["State"])
        self.assertEqual([item.label for item in page.section_actions], ["Add"])
        self.assertEqual([item.kind for item in page.navigation_controls],
                         [NavigationKind.ADVANCE, NavigationKind.SUBMIT])
        self.assertEqual(page.discovery_summary.raw_actionable_count, 4)


class DOMOnlyControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_dom_only_state_blocks_until_fresh_selected_answer(self):
        class Browser:
            value = "Select One"
            actions = []

            async def observe(self):
                page = observed()
                page = merge_dom_observation(page, payload(candidate(
                    "State / Province", value=self.value, button=self.value)))
                return ApplicationObservation(page.observation_id, page.location, page.heading,
                    questions=page.questions,
                    navigation_controls=(NavigationControl(
                        "Submit Application", NavigationKind.SUBMIT, "submit-ref"),))

        browser = Browser()
        controller = ApplicationController(browser,
            DeterministicAnswerResolver(CandidateProfile.from_mapping(profile_data())))
        first = await controller.run(ApplicationSession("fixture", "https://example.test/apply"),
                                     current_page_only=True)
        self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(len(first.fields), 1)
        self.assertTrue(first.fields[0].question.required)
        browser.value = "California"  # Simulated owner selection, never a browser action.
        second = await controller.run(ApplicationSession("fixture", "https://example.test/apply"),
                                      current_page_only=True)
        self.assertEqual(second.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(second.fields[0].action, "manual_complete")
        self.assertEqual(browser.actions, [])


if __name__ == "__main__":
    unittest.main()
