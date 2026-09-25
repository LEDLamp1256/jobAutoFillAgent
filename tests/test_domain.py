import unittest
from dataclasses import replace

from jobagent.domain import (
    ActionOutcome, ActionPolicy, ActionStatus, Advance, Answer, AnswerScope,
    AnswerSource, ApplicationObservation, ApplicationOutcome, ApplicationSession, ChooseOption,
    ControlType, FillText, GoBack, HumanApproval, NavigationControl, NavigationKind,
    QuestionObservation, SubmissionPermission, Submit, semantic_fingerprint,
)


def review_observation(observation_id="review-a", *, submit_ref="submit-a"):
    return ApplicationObservation(
        observation_id=observation_id,
        location="https://example.test/apply",
        heading="Review application",
        progress_text="Step 4 of 4",
        questions=(QuestionObservation("Email", ControlType.TEXT,
                                       semantic_key="personal.email", current_value="a@example.test",
                                       target_ref="email-a"),),
        navigation_controls=(NavigationControl("Submit application", NavigationKind.SUBMIT, submit_ref),
                             NavigationControl("Back", NavigationKind.BACK, "back-a")),
        review_like=True,
    )


class QuestionTests(unittest.TestCase):
    def test_reference_and_label_variation_do_not_define_known_identity(self):
        first = QuestionObservation("First name", ControlType.TEXT,
                                    semantic_key="personal.first_name", target_ref="e1")
        second = replace(first, label="Legal first name", target_ref="e99")
        self.assertEqual(first.identity(), second.identity())

    def test_unknown_question_is_valid(self):
        question = QuestionObservation("Other information", target_ref="e1")
        self.assertEqual(question.control_type, ControlType.UNKNOWN)
        self.assertTrue(question.identity())

    def test_repeated_records_have_distinct_identity(self):
        first = QuestionObservation("Start date", ControlType.TEXT,
                                    semantic_key="employment.start_date", record_context="Acme")
        second = replace(first, record_context="Other Company")
        self.assertNotEqual(first.identity(), second.identity())


class AnswerTests(unittest.TestCase):
    def test_provenance_and_scope_are_explicit(self):
        profile = Answer("personal.email", "a@example.test", AnswerSource.CANDIDATE_PROFILE,
                         AnswerScope.GLOBAL, confidence=1)
        model = Answer("why_this_company", "Because...", AnswerSource.LOCAL_LLM,
                       AnswerScope.APPLICATION, application_id="app-a", confidence=.7)
        self.assertNotEqual(profile.source, model.source)
        self.assertTrue(profile.reusable_in("app-b"))
        self.assertTrue(model.reusable_in("app-a"))
        self.assertFalse(model.reusable_in("app-b"))

    def test_application_answer_cannot_lack_application_id(self):
        with self.assertRaises(ValueError):
            Answer("desired_salary", "100", AnswerSource.HUMAN, AnswerScope.APPLICATION)

    def test_human_review_flag_blocks_unapproved_reuse(self):
        answer = Answer("desired_salary", "100", AnswerSource.LOCAL_LLM,
                        AnswerScope.APPLICATION, application_id="app-a",
                        requires_human_approval=True)
        self.assertFalse(answer.reusable_in("app-a"))
        self.assertTrue(replace(answer, human_approved=True).reusable_in("app-a"))

    def test_low_confidence_model_answer_is_not_automatically_fillable(self):
        answer = Answer("personal.email", "guess@example.test", AnswerSource.LOCAL_LLM,
                        AnswerScope.APPLICATION, application_id="app-a", confidence=.55)
        self.assertFalse(answer.safe_for_automatic_fill("app-a"))
        self.assertTrue(replace(answer, human_approved=True).safe_for_automatic_fill("app-a"))


class ObservationTests(unittest.TestCase):
    def test_session_history_unresolved_validation_and_uploads(self):
        session = ApplicationSession("app-a", "https://example.test/job", company="Example")
        question = QuestionObservation("Email", ControlType.TEXT, semantic_key="personal.email")
        a = ApplicationObservation("a", "/apply", questions=(question,))
        b = ApplicationObservation("b", "/apply", questions=(question,),
                                   validation_messages=("Email is required",))
        session.record_observation(a)
        session.mark_unresolved(question)
        session.record_observation(b)
        self.assertIs(session.previous_observation, a)
        self.assertIs(session.current_observation, b)
        self.assertEqual(len(session.question_history[question.identity()]), 2)
        self.assertIn(question.identity(), session.unresolved_questions)
        self.assertEqual(session.validation_problems, ("Email is required",))
        session.record_answer(question, Answer("personal.email", "a@example.test",
                                               AnswerSource.CANDIDATE_PROFILE, AnswerScope.GLOBAL))
        session.record_upload("resume")
        self.assertNotIn(question.identity(), session.unresolved_questions)
        self.assertIn("resume", session.uploaded_documents)

    def test_fingerprint_ignores_temporary_references_and_observation_id(self):
        a = review_observation()
        b = replace(a, observation_id="review-b",
                    questions=(replace(a.questions[0], target_ref="email-b"),),
                    navigation_controls=(replace(a.navigation_controls[0], target_ref="submit-b"),
                                         replace(a.navigation_controls[1], target_ref="back-b")))
        self.assertEqual(semantic_fingerprint(a), semantic_fingerprint(b))

    def test_fingerprint_changes_for_step_question_value_and_validation(self):
        a = review_observation()
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(replace(a, heading="Employment")))
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(replace(a, questions=())))
        changed_value = replace(a, questions=(replace(a.questions[0], current_value="b@example.test"),))
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(changed_value))
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(
            replace(a, validation_messages=("Fix email",))))

    def test_equal_fingerprints_are_only_apparent_state_equality(self):
        a = review_observation()
        same = replace(a, observation_id="again")
        outcome = ActionOutcome(ActionStatus.NO_PROGRESS, before=semantic_fingerprint(a),
                                after=semantic_fingerprint(same))
        self.assertEqual(outcome.before, outcome.after)
        self.assertNotEqual(outcome.status, ActionStatus.FAILED)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.session = ApplicationSession("app-a", "https://example.test/job")
        self.review = review_observation()
        self.session.record_observation(self.review)

    def approve(self):
        approval = HumanApproval("app-a", self.review.observation_id,
                                 semantic_fingerprint(self.review))
        self.session.approve_submission(approval)

    def test_locked_submit_denied(self):
        self.assertEqual(self.session.submission_permission, SubmissionPermission.LOCKED)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Submit("submit-a", "review-a"), self.session)

    def test_matching_human_approval_grants_submit_permit(self):
        self.approve()
        permit = ActionPolicy.authorize(Submit("submit-a", "review-a"), self.session)
        self.assertEqual(permit.application_id, "app-a")
        self.session.start_submission(permit)
        self.assertEqual(self.session.outcome, ApplicationOutcome.SUBMITTING)
        self.assertEqual(self.session.submission_permission, SubmissionPermission.LOCKED)

    def test_state_change_revokes_approval_even_if_semantics_look_same(self):
        self.approve()
        self.session.record_observation(replace(self.review, observation_id="review-b"))
        self.assertEqual(self.session.submission_permission, SubmissionPermission.LOCKED)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Submit("submit-a", "review-b"), self.session)

    def test_wrong_application_or_review_state_cannot_be_approved(self):
        with self.assertRaises(PermissionError):
            self.session.approve_submission(HumanApproval("app-b", "review-a",
                                                          semantic_fingerprint(self.review)))
        with self.assertRaises(PermissionError):
            self.session.approve_submission(HumanApproval("app-a", "review-a",
                                                          semantic_fingerprint(replace(self.review, heading="Other"))))

    def test_navigation_does_not_need_submit_approval(self):
        step = ApplicationObservation("step-a", "/apply", questions=(
            QuestionObservation("Email", ControlType.TEXT, semantic_key="personal.email",
                                target_ref="email-a"),),
            navigation_controls=(NavigationControl("Continue", NavigationKind.ADVANCE, "next-a"),))
        self.session.record_observation(step)
        self.assertIsNone(ActionPolicy.authorize(Advance("next-a", "step-a"), self.session))
        answer = Answer("personal.email", "a@example.test", AnswerSource.CANDIDATE_PROFILE,
                        AnswerScope.GLOBAL)
        self.assertIsNone(ActionPolicy.authorize(FillText("email-a", "step-a", answer), self.session))

    def test_uncertain_answer_cannot_be_filled_even_when_nonempty(self):
        answer = Answer("personal.email", "guess@example.test", AnswerSource.LOCAL_LLM,
                        AnswerScope.APPLICATION, application_id="app-a", confidence=.55)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(FillText("email-a", "review-a", answer), self.session)
        with self.assertRaises(ValueError):
            self.session.record_answer(self.review.questions[0], answer)

    def test_unknown_or_ambiguous_navigation_fails_closed(self):
        unknown = ApplicationObservation("unknown", "/apply", navigation_controls=(
            NavigationControl("Apply", NavigationKind.UNKNOWN, "mystery"),))
        self.session.record_observation(unknown)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Advance("mystery", "unknown"), self.session)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Submit("mystery", "unknown"), self.session)
        ambiguous = replace(unknown, observation_id="ambiguous", navigation_controls=(
            NavigationControl("Continue", NavigationKind.ADVANCE, "same"),
            NavigationControl("Submit", NavigationKind.SUBMIT, "same")))
        self.session.record_observation(ambiguous)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Advance("same", "ambiguous"), self.session)

    def test_submit_control_cannot_be_advanced_as_routine_action(self):
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(Advance("submit-a", "review-a"), self.session)

    def test_action_from_previous_observation_is_rejected(self):
        self.session.record_observation(replace(self.review, observation_id="review-b"))
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(GoBack("back-a", "review-a"), self.session)

    def test_answer_action_must_match_control_and_visible_options(self):
        choice = QuestionObservation("Currently employed?", ControlType.CHOICE,
                                     semantic_key="employment.current", options=("Yes", "No"),
                                     target_ref="employed")
        self.session.record_observation(ApplicationObservation("employment", "/apply",
                                                               questions=(choice,)))
        answer = Answer("employment.current", "Maybe", AnswerSource.CANDIDATE_PROFILE,
                        AnswerScope.GLOBAL)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(ChooseOption("employed", "employment", answer), self.session)
        with self.assertRaises(PermissionError):
            ActionPolicy.authorize(FillText("employed", "employment", replace(answer, value="Yes")),
                                   self.session)

    def test_submission_confirmation_can_be_observed_without_resetting_attempt(self):
        self.approve()
        self.session.start_submission(ActionPolicy.authorize(Submit("submit-a", "review-a"), self.session))
        self.session.record_observation(ApplicationObservation("confirmation", "/thanks",
                                                            heading="Thank you"))
        self.assertEqual(self.session.outcome, ApplicationOutcome.SUBMITTING)
        self.session.finish_submission(verified=True)
        self.assertEqual(self.session.outcome, ApplicationOutcome.SUBMITTED_VERIFIED)

    def test_outcomes_distinguish_decline_attempt_and_verification(self):
        self.session.abandon()
        self.assertEqual(self.session.outcome, ApplicationOutcome.ABANDONED)
        attempted = ApplicationSession("app-b", "/job")
        observation = review_observation("review-b", submit_ref="submit-b")
        attempted.record_observation(observation)
        attempted.approve_submission(HumanApproval("app-b", "review-b", semantic_fingerprint(observation)))
        permit = ActionPolicy.authorize(Submit("submit-b", "review-b"), attempted)
        attempted.start_submission(permit)
        self.assertEqual(attempted.outcome, ApplicationOutcome.SUBMITTING)
        attempted.finish_submission(verified=False)
        self.assertEqual(attempted.outcome, ApplicationOutcome.SUBMITTED_UNVERIFIED)
        verified = ApplicationSession("app-c", "/job")
        verified.record_observation(observation)
        verified.approve_submission(HumanApproval("app-c", "review-b", semantic_fingerprint(observation)))
        verified.start_submission(ActionPolicy.authorize(Submit("submit-b", "review-b"), verified))
        verified.finish_submission(verified=True)
        self.assertEqual(verified.outcome, ApplicationOutcome.SUBMITTED_VERIFIED)
        self.assertEqual(len({self.session.outcome, ApplicationOutcome.SUBMITTING,
                              attempted.outcome, verified.outcome}), 4)


if __name__ == "__main__":
    unittest.main()
