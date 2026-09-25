import unittest
from dataclasses import replace

from jobagent.browser import BrowserActionResult
from jobagent.controller import (
    AdvanceClassification, ApplicationController, ControllerLimits, ControllerStop,
    classify_advance, step_signature,
)
from jobagent.domain import (
    ActionOutcome, ActionStatus, ApplicationObservation, ApplicationOutcome,
    ApplicationSession, ControlType, NavigationControl, NavigationKind,
    QuestionObservation, semantic_fingerprint,
)
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from tests.test_resolution import profile_data


class ScriptedBrowser:
    def __init__(self, *, advance_behavior="normal", extra_question=False,
                 optional_question=False, duplicate_advance=False):
        self.step = 1
        self.counter = 0
        self.current = None
        self.values = {}
        self.validation = ()
        self.advance_behavior = advance_behavior
        self.extra_question = extra_question
        self.optional_question = optional_question
        self.duplicate_advance = duplicate_advance
        self.revealed_field = False
        self.calls = []

    def _observation(self):
        self.counter += 1
        ref = f"e{self.counter}"

        def text(label, key, required=True):
            return QuestionObservation(label, ControlType.TEXT, semantic_key=key, required=required,
                                       current_value=self.values.get(key), target_ref=f"{ref}-{key}")

        if self.step == 1:
            heading, progress = "Basic information", "Step 1 of 4: Basic information"
            questions = [text("First name", "personal.first_name"),
                         text("Last name", "personal.last_name"),
                         text("Email", "personal.email")]
            if self.revealed_field:
                questions.append(text("Phone" if self.advance_behavior == "reveal_field" else
                                      "Unknown follow-up", "personal.phone" if
                                      self.advance_behavior == "reveal_field" else "unknown.followup"))
            if self.extra_question:
                questions.append(text("Unknown employer code", "unknown.code",
                                      required=False if self.optional_question else None))
        elif self.step == 2:
            heading, progress = "Employment", "Step 2 of 4: Employment"
            questions = [QuestionObservation(
                "Are you currently employed?", ControlType.CHOICE,
                semantic_key="employment.current", options=("Yes", "No"), required=None,
                current_value=self.values.get("employment.current"), target_ref=f"{ref}-employment.current")]
            if self.values.get("employment.current") == "Yes":
                questions.append(text("Current employer", "employment.current_employer"))
        elif self.step == 3:
            heading, progress = "Contact confirmation", "Step 3 of 4: Contact confirmation"
            questions = [replace(text("Confirm email", "personal.email"),
                                 current_value=self.values.get("confirmed_email"))]
        else:
            heading, progress = "Review application", "Step 4 of 4: Review application"
            questions = []
        controls = ([NavigationControl("Submit application", NavigationKind.SUBMIT, f"{ref}-submit")]
                    if self.step == 4 else
                    [NavigationControl("Continue", NavigationKind.ADVANCE, f"{ref}-advance")])
        if self.step != 4 and self.duplicate_advance:
            controls.append(NavigationControl("Continue", NavigationKind.UNKNOWN, f"{ref}-duplicate"))
        self.current = ApplicationObservation(ref, "http://local/application", heading, progress,
                                              tuple(questions), self.validation, tuple(controls),
                                              self.step == 4)
        return self.current

    async def navigate(self, url):
        self.calls.append(("navigate", url))
        return self._observation()

    async def observe(self):
        self.calls.append(("observe", None))
        return self._observation()

    async def fill_text(self, action, session):
        self.calls.append(("fill", action.target_ref))
        assert action.observation_id == self.current.observation_id
        key = "confirmed_email" if self.step == 3 else action.answer.semantic_key
        self.values[key] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self._observation())

    async def choose_option(self, action, session):
        self.calls.append(("choose", action.target_ref))
        assert action.observation_id == self.current.observation_id
        self.values[action.answer.semantic_key] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self._observation())

    async def activate_navigation(self, action, session):
        self.calls.append(("advance", action.target_ref))
        assert action.observation_id == self.current.observation_id
        assert self.step < 4, "Submit must never be activated"
        if self.advance_behavior == "validation":
            self.validation = ("A required value is missing",)
            return BrowserActionResult(ActionOutcome(ActionStatus.VALIDATION_BLOCKED), self._observation())
        if self.advance_behavior == "no_progress":
            return BrowserActionResult(ActionOutcome(ActionStatus.NO_PROGRESS), self._observation())
        if self.advance_behavior in {"reveal_field", "reveal_unknown"} and not self.revealed_field:
            self.revealed_field = True
            return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self._observation())
        self.step += 1
        self.validation = ()
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), self._observation())

    async def close(self):
        pass


def resolver():
    data = profile_data()
    data["work_history"] = [{"company": "Analytical Engine", "is_current": True}]
    return DeterministicAnswerResolver(CandidateProfile.from_mapping(data))


class ProgressTests(unittest.TestCase):
    def test_signature_ignores_refs_values_validation_and_ids(self):
        question = QuestionObservation("Email", ControlType.TEXT, current_value=None, target_ref="old")
        old = ApplicationObservation("old", "/apply", "Contact", "Step 1", (question,))
        new = replace(old, observation_id="new", validation_messages=("Required",),
                      questions=(replace(question, current_value="ada@example.test", target_ref="new"),))
        self.assertEqual(step_signature(old), step_signature(new))
        self.assertNotEqual(semantic_fingerprint(old), semantic_fingerprint(new))
        self.assertEqual(classify_advance(old, new, ActionStatus.STATE_CHANGED),
                         AdvanceClassification.VALIDATION_BLOCKED)

    def test_url_alone_is_not_progress_and_question_revelation_is_not_new_step(self):
        old = ApplicationObservation("a", "/one", "Employment", "Step 2",
                                     (QuestionObservation("Employed?"),))
        changed_url = replace(old, observation_id="b", location="/two")
        self.assertEqual(classify_advance(old, changed_url, ActionStatus.STATE_CHANGED),
                         AdvanceClassification.NO_PROGRESS)
        reveal = replace(old, observation_id="c", questions=old.questions +
                         (QuestionObservation("Current employer"),))
        self.assertEqual(classify_advance(old, reveal, ActionStatus.STATE_CHANGED),
                         AdvanceClassification.NEW_QUESTIONS)
        next_step = replace(old, observation_id="d", heading="Review", progress_text="Step 3")
        self.assertNotEqual(step_signature(old), step_signature(next_step))
        self.assertEqual(classify_advance(old, next_step, ActionStatus.STATE_CHANGED),
                         AdvanceClassification.PROGRESSED)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, browser=None, limits=None):
        browser = browser or ScriptedBrowser()
        session = ApplicationSession("fixture", "http://local/application")
        result = await ApplicationController(browser, resolver(), limits).run(session)
        return result, browser

    async def test_normal_conditional_repeated_and_final_review(self):
        result, browser = await self.run_case()
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(result.session.outcome, ApplicationOutcome.READY_FOR_REVIEW)
        self.assertEqual([step.to_heading for step in result.session.step_history],
                         ["Employment", "Contact confirmation", "Review application"])
        self.assertEqual(browser.values["employment.current_employer"], "Analytical Engine")
        self.assertEqual(browser.values["personal.email"], "ada@example.test")
        self.assertEqual(browser.values["confirmed_email"], "ada@example.test")
        self.assertEqual(len([call for call in browser.calls if call[0] == "advance"]), 3)
        self.assertFalse(any("submit" in str(target) for _, target in browser.calls))
        self.assertTrue(result.session.action_history)
        self.assertFalse(any("submit" in str(entry) for entry in result.session.action_history))
        employment_index = next(i for i, obs in enumerate(result.session.observations)
                                if obs.heading == "Employment" and
                                any(q.label == "Current employer" for q in obs.questions))
        self.assertLess(employment_index, next(i for i, obs in enumerate(result.session.observations)
                                               if obs.heading == "Contact confirmation"))

    async def test_validation_blocked_stops_without_retry(self):
        result, browser = await self.run_case(ScriptedBrowser(advance_behavior="validation"))
        self.assertEqual(result.stop, ControllerStop.VALIDATION_BLOCKED)
        self.assertEqual(len([call for call in browser.calls if call[0] == "advance"]), 1)
        self.assertTrue(result.session.validation_history)
        self.assertEqual(result.session.current_observation.heading, "Basic information")

    async def test_no_progress_stops_without_retry(self):
        result, browser = await self.run_case(ScriptedBrowser(advance_behavior="no_progress"))
        self.assertEqual(result.stop, ControllerStop.NO_PROGRESS)
        self.assertEqual(len([call for call in browser.calls if call[0] == "advance"]), 1)

    async def test_unresolved_unknown_requiredness_stops(self):
        result, browser = await self.run_case(ScriptedBrowser(extra_question=True))
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertTrue(result.session.unresolved_questions)
        self.assertFalse(any(call[0] == "advance" for call in browser.calls))

    async def test_explicitly_optional_unknown_question_can_be_skipped(self):
        result, _ = await self.run_case(ScriptedBrowser(extra_question=True, optional_question=True))
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertTrue(result.session.unresolved_questions)

    async def test_duplicate_advance_intent_stops(self):
        result, browser = await self.run_case(ScriptedBrowser(duplicate_advance=True))
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertFalse(any(call[0] == "advance" for call in browser.calls))

    async def test_advance_reveals_field_then_repairs_before_retry(self):
        result, browser = await self.run_case(ScriptedBrowser(advance_behavior="reveal_field"))
        self.assertEqual(result.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual(browser.values["personal.phone"], "555-0100")
        self.assertEqual(len([call for call in browser.calls if call[0] == "advance"]), 4)

    async def test_advance_reveals_unknown_field_without_blind_retry(self):
        result, browser = await self.run_case(ScriptedBrowser(advance_behavior="reveal_unknown"))
        self.assertEqual(result.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(len([call for call in browser.calls if call[0] == "advance"]), 1)

    async def test_action_and_cycle_bounds(self):
        for limits in (ControllerLimits(max_actions=1), ControllerLimits(max_cycles=1),
                       ControllerLimits(max_same_step_actions=1)):
            with self.subTest(limits=limits):
                result, _ = await self.run_case(limits=limits)
                self.assertEqual(result.stop, ControllerStop.ACTION_LIMIT_REACHED)

    async def test_step_transition_bound(self):
        result, _ = await self.run_case(limits=ControllerLimits(max_step_transitions=1))
        self.assertEqual(result.stop, ControllerStop.ACTION_LIMIT_REACHED)
        self.assertEqual(len(result.session.step_history), 2)


if __name__ == "__main__":
    unittest.main()
