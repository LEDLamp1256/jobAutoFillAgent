"""Deterministic four-page direct-application rehearsal; no employer endpoint."""

import unittest

from jobagent.browser import BrowserActionResult
from jobagent.controller import ApplicationController, ControllerStop
from jobagent.domain import (
    ActionOutcome, ActionPolicy, ActionStatus, ApplicationObservation, ApplicationSession,
    ControlType, NavigationControl, NavigationKind, QuestionObservation, SectionAction,
)
from jobagent.resolution import CandidateProfile, DeterministicAnswerResolver
from tests.test_resolution import profile_data


class LocalPages:
    def __init__(self):
        self.page = 1
        self.serial = 0
        self.values = {}
        self.last = None
        self.actions = []

    async def observe(self):
        self.serial += 1
        oid = f"fresh-{self.serial}"

        def field(label, kind=ControlType.TEXT, *, required=True, options=(), selected=()):
            return QuestionObservation(label, kind, required=required,
                options=options, selected_values=selected,
                current_value=self.values.get(label), target_ref=f"{oid}-{label}")

        if self.page == 1:
            heading = "Applicant information"
            questions = (field("First Name"), field("Last Name"),
                         field("Middle Name", required=False),
                         field("Referral Source", ControlType.UNKNOWN))
            controls = (NavigationControl("Next", NavigationKind.ADVANCE, f"{oid}-next"),)
            sections = ()
        elif self.page == 2:
            heading = "Contact and documents"
            questions = (field("Phone"),
                         field("State/Province", ControlType.CHOICE,
                               options=("Select One", "California", "Colorado")),
                         field("Optional note", required=False),
                         field("Required page-two choice", ControlType.UNKNOWN))
            controls = (NavigationControl("Save and Continue", NavigationKind.ADVANCE,
                                          f"{oid}-save"),)
            sections = ()
        elif self.page == 3:
            heading = "Experience and education"
            questions = (field("Skills", ControlType.MULTI_CHOICE, required=False,
                               selected=("Example skill",)),)
            controls = (NavigationControl("Continue to Review", NavigationKind.ADVANCE,
                                          f"{oid}-review"),)
            sections = (SectionAction("Add", "Work Experience"),
                        SectionAction("Add", "Education"),
                        SectionAction("Add Another", "Certifications"))
        else:
            heading = "Review Application"
            questions = ()
            controls = (NavigationControl("Submit Application", NavigationKind.SUBMIT,
                                          f"{oid}-submit"),)
            sections = ()
        self.last = ApplicationObservation(oid, f"http://127.0.0.1/fixture/{self.page}",
            heading, questions=questions, navigation_controls=controls,
            section_actions=sections, review_like=self.page == 4)
        return self.last

    async def fill_text(self, action, session):
        ActionPolicy.authorize(action, session)
        question = next(q for q in self.last.questions if q.target_ref == action.target_ref)
        self.actions.append(("fill", question.label, action.observation_id))
        self.values[question.label] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

    async def choose_option(self, action, session):
        ActionPolicy.authorize(action, session)
        question = next(q for q in self.last.questions if q.target_ref == action.target_ref)
        assert action.answer.value in question.options
        self.actions.append(("choose", question.label, action.observation_id))
        self.values[question.label] = action.answer.value
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())

    async def activate_navigation(self, action, session):
        ActionPolicy.authorize(action, session)
        assert action.target_ref in {control.target_ref for control in self.last.navigation_controls
                                     if control.kind is NavigationKind.ADVANCE}
        self.actions.append(("advance", self.page, action.observation_id))
        self.page += 1
        return BrowserActionResult(ActionOutcome(ActionStatus.STATE_CHANGED), await self.observe())


class LocalMultiPageAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_pause_manual_resume_and_reach_final_human_boundary(self):
        browser = LocalPages()
        data = profile_data()
        data["personal_info"]["address"]["state"] = "California"
        controller = ApplicationController(browser,
            DeterministicAnswerResolver(CandidateProfile.from_mapping(data)))

        async def one_pass():
            return await controller.run(ApplicationSession("fixture", "http://127.0.0.1/fixture"),
                                        current_page_only=True)

        first = await one_pass()
        self.assertEqual(first.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.page, 1)
        self.assertEqual(browser.values["First Name"], data["personal_info"]["first_name"])
        self.assertEqual(browser.values["Last Name"], data["personal_info"]["last_name"])
        self.assertEqual(next(f for f in first.fields if f.question.label == "Middle Name").action,
                         "optional_skipped")
        first_ref = next(f.question.target_ref for f in first.fields
                         if f.question.label == "Referral Source")
        browser.values["Referral Source"] = "Example source"  # Simulated owner edit.
        second = await one_pass()
        self.assertEqual(second.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(next(f for f in second.fields if f.question.label == "Referral Source").action,
                         "manual_complete")
        self.assertNotEqual(first_ref, next(f.question.target_ref for f in second.fields
                                            if f.question.label == "Referral Source"))
        self.assertEqual(browser.page, 2)

        third = await one_pass()
        self.assertEqual(third.stop, ControllerStop.NEEDS_REVIEW)
        self.assertEqual(browser.values["State/Province"], "California")
        self.assertEqual(browser.page, 2)
        browser.values["Required page-two choice"] = "Example answer"  # Simulated owner edit.
        fourth = await one_pass()
        self.assertEqual(fourth.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(browser.page, 3)
        fifth = await one_pass()
        self.assertEqual(fifth.stop, ControllerStop.PAGE_ADVANCED)
        self.assertEqual(browser.page, 4)
        final = await one_pass()
        self.assertEqual(final.stop, ControllerStop.READY_FOR_REVIEW)
        self.assertEqual([action[1] for action in browser.actions if action[0] == "advance"],
                         [1, 2, 3])
        self.assertFalse(any(action[0] == "submit" for action in browser.actions))


if __name__ == "__main__":
    unittest.main()
