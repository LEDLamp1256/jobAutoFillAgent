"""Deterministic v2 resolution contracts, without browser or model calls."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jobagent.domain import Answer, AnswerScope, AnswerSource, ControlType, QuestionObservation
from jobagent.resolution import (
    AnswerLedger, CandidateProfile, CanonicalStatus, DeterministicAnswerResolver,
    ProfileError, ResolutionStatus, SemanticCanonicalizer,
)


def profile_data():
    return {
        "personal_info": {
            "full_name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
            "email": "ada@example.test", "phone": "555-0100",
            "address": {"street": "1 Main St", "city": "London", "state": "CA",
                        "zip_code": "90001", "country": "United States"},
            "authorized_to_work_us": True, "requires_visa_sponsorship": False,
            "willing_to_relocate": True, "linkedin_url": "https://example.test/ada",
        },
        "work_history": [],
        "education": [{"institution": "Example University", "degree": "B.S.", "gpa": "3.9"}],
        "skills": {"languages": ["Python"]},
        "qa_bank": {"desired_salary": {"answer": "$100,000"},
                    "eeo_gender": {"answer": "Decline to answer"}},
        "documents": {"resume_path": "/test/resume.pdf"},
        "application_preferences": {},
    }


def question(label, *, control=ControlType.TEXT, options=(), section=None, record=None, ref=None):
    return QuestionObservation(label, control, section=section, record_context=record,
                               options=options, target_ref=ref)


class ProfileTests(unittest.TestCase):
    def test_existing_schema_sections_and_json_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(profile_data()), encoding="utf-8")
            profile = CandidateProfile.from_json(path)
            self.assertEqual(profile.personal_info["email"], "ada@example.test")
            self.assertEqual(profile.education[0]["institution"], "Example University")
            self.assertEqual(profile.skills["languages"], ["Python"])
            self.assertEqual(profile.documents["resume_path"], "/test/resume.pdf")

    def test_malformed_or_missing_profile_fails_explicitly_without_pii(self):
        for data in ([], {"personal_info": {}}, {**profile_data(), "education": "bad"},
                     {**profile_data(), "qa_bank": {"key": "answer"}}):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ProfileError):
                CandidateProfile.from_mapping(data)
        with self.assertRaises(ProfileError) as error:
            CandidateProfile.from_json("/no/such/private-profile.json")
        self.assertNotIn("ada@example.test", str(error.exception))

    def test_null_qa_answer_loads_as_missing_fact(self):
        data = profile_data()
        data["qa_bank"]["desired_salary"]["answer"] = None
        profile = CandidateProfile.from_mapping(data)
        result = DeterministicAnswerResolver(profile).resolve(question("Desired Salary"), "A")
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.answer)
        with self.assertRaises(ProfileError):
            CandidateProfile.from_mapping({**data, "qa_bank": {"desired_salary": {"notes": "missing"}}})


class CanonicalizerTests(unittest.TestCase):
    def setUp(self):
        self.canonicalizer = SemanticCanonicalizer()

    def assert_key(self, labels, key, **kwargs):
        for label in labels:
            with self.subTest(label=label):
                result = self.canonicalizer.canonicalize(question(label, **kwargs))
                self.assertEqual(result.status, CanonicalStatus.MATCHED)
                self.assertEqual(result.semantic_key, key)
                self.assertEqual(result.rule, "explicit_alias")

    def test_basic_aliases(self):
        self.assert_key(("First Name", "Legal First Name", "Given Name"), "personal.first_name")
        self.assert_key(("Email", "Email Address", "E-mail Address", "Confirm Email"), "personal.email")
        self.assert_key(("Phone", "Phone Number", "Mobile Phone"), "personal.phone")
        self.assert_key(("ZIP Code", "Postal Code"), "personal.address.postal")
        self.assert_key(("Street Address", "Address Line 1"), "personal.address.street")
        self.assert_key(("City",), "personal.address.city")
        self.assert_key(("LinkedIn URL",), "personal.linkedin")

    def test_work_education_and_upload_aliases(self):
        self.assert_key(("Are you authorized to work in the US?",), "employment.us_authorized")
        self.assert_key(("Do you require visa sponsorship?",), "employment.sponsorship")
        self.assert_key(("Willing to relocate?",), "employment.relocation")
        self.assert_key(("School", "Institution Name"), "education.institution", section="Education")
        self.assert_key(("Degree",), "education.degree", section="Education")
        self.assert_key(("GPA",), "education.gpa", section="Education")
        self.assert_key(("Resume/CV",), "documents.resume", control=ControlType.FILE)

    def test_ambiguous_context_and_conflicting_hint(self):
        for item in (question("Start Date"), question("Work Authorization"),
                     question("School"), question("City", section="Work History"),
                     question("Email", section="Reference contact"),
                     question("Resume", control=ControlType.TEXT),
                     QuestionObservation("Email", semantic_key="personal.phone")):
            with self.subTest(label=item.label):
                self.assertEqual(self.canonicalizer.canonicalize(item).status, CanonicalStatus.AMBIGUOUS)
        self.assertEqual(self.canonicalizer.canonicalize(question("Describe a project")).status,
                         CanonicalStatus.UNRESOLVED)

    def test_browser_ref_does_not_change_semantics(self):
        first = question("Email Address", ref="e1")
        self.assertEqual(self.canonicalizer.canonicalize(first),
                         self.canonicalizer.canonicalize(replace(first, target_ref="e99")))


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.profile = CandidateProfile.from_mapping(profile_data())
        self.ledger = AnswerLedger()
        self.resolver = DeterministicAnswerResolver(self.profile, self.ledger)

    def test_profile_facts_and_missing_values(self):
        for label, expected in (("Given Name", "Ada"), ("Last Name", "Lovelace"),
                                ("Email", "ada@example.test"), ("Phone", "555-0100"),
                                ("City", "London"), ("ZIP Code", "90001")):
            with self.subTest(label=label):
                result = self.resolver.resolve(question(label), "A")
                self.assertEqual(result.status, ResolutionStatus.SAFE_TO_FILL)
                self.assertEqual(result.answer.value, expected)
                self.assertEqual(result.answer.source, AnswerSource.CANDIDATE_PROFILE)
        missing = profile_data()
        del missing["personal_info"]["phone"]
        self.assertEqual(DeterministicAnswerResolver(CandidateProfile.from_mapping(missing))
                         .resolve(question("Phone"), "A").status, ResolutionStatus.UNRESOLVED)

    def test_first_last_are_not_guessed_from_full_name(self):
        data = profile_data()
        del data["personal_info"]["first_name"]
        del data["personal_info"]["last_name"]
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
        self.assertEqual(resolver.resolve(question("First Name"), "A").status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolver.resolve(question("Last Name"), "A").status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolver.resolve(question("Full Name"), "A").answer.value, "Ada Lovelace")

    def test_profile_authority_and_human_app_correction(self):
        self.ledger.record(Answer("personal.email", "stale@example.test", AnswerSource.HUMAN,
                                  AnswerScope.GLOBAL, human_approved=True))
        self.assertEqual(self.resolver.resolve(question("Email"), "A").answer.value, "ada@example.test")
        correction = Answer("personal.email", "ada+job@example.test", AnswerSource.HUMAN,
                            AnswerScope.APPLICATION, "A", human_approved=True)
        self.ledger.record(correction)
        resolved = self.resolver.resolve(question("Email"), "A")
        self.assertEqual(resolved.answer, correction)
        self.assertEqual(self.resolver.resolve(question("Email"), "B").answer.value, "ada@example.test")
        self.assertEqual(self.profile.personal_info["email"], "ada@example.test")

    def test_global_and_application_ledger_scope_and_provenance(self):
        data = profile_data()
        del data["personal_info"]["phone"]
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data), self.ledger)
        global_answer = Answer("personal.phone", "555-2222", AnswerSource.HUMAN,
                               AnswerScope.GLOBAL, human_approved=True)
        self.ledger.record(global_answer)
        self.assertEqual(resolver.resolve(question("Phone"), "B").answer, global_answer)
        app_answer = Answer("personal.phone", "555-3333", AnswerSource.HUMAN,
                            AnswerScope.APPLICATION, "A", human_approved=True)
        self.ledger.record(app_answer)
        self.assertEqual(resolver.resolve(question("Phone"), "A").answer, app_answer)
        self.assertEqual(resolver.resolve(question("Phone"), "B").answer, global_answer)
        with self.assertRaises(ValueError):
            self.ledger.record(Answer("desired_salary", "100", AnswerSource.HUMAN,
                                      AnswerScope.GLOBAL, human_approved=True))

    def test_unapproved_ledger_cannot_be_autofilled(self):
        with self.assertRaises(ValueError):
            self.ledger.record(Answer("personal.email", "guess@example.test", AnswerSource.LOCAL_LLM,
                                      AnswerScope.GLOBAL, confidence=.5))
        with self.assertRaises(ValueError):
            self.ledger.record(Answer("personal.email", "plausible@example.test", AnswerSource.LOCAL_LLM,
                                      AnswerScope.GLOBAL, confidence=.99))

    def test_qa_bank_exact_scope_and_narrative(self):
        salary = self.resolver.resolve(question("Desired Salary"), "A")
        self.assertEqual(salary.status, ResolutionStatus.SAFE_TO_FILL)
        self.assertEqual(salary.answer.source, AnswerSource.QA_BANK)
        self.assertEqual(salary.answer.scope, AnswerScope.APPLICATION)
        self.assertEqual(self.resolver.resolve(question("Why do you want to work here?"), "A").status,
                         ResolutionStatus.UNRESOLVED)
        data = profile_data()
        data["qa_bank"]["why_this_company"] = {
            "answer": "I know this team and its work.", "scope": "application", "application_id": "A"}
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
        self.assertEqual(resolver.resolve(question("Why do you want to work here?"), "A").status,
                         ResolutionStatus.SAFE_TO_FILL)
        self.assertEqual(resolver.resolve(question("Why do you want to work here?"), "B").status,
                         ResolutionStatus.REQUIRES_REVIEW)
        self.assertEqual(resolver.resolve(question("Describe a challenging project"), "A").status,
                         ResolutionStatus.UNRESOLVED)
        unrelated = question("How did you hear about us?")
        self.assertEqual(self.resolver.resolve(unrelated, "A").status, ResolutionStatus.UNRESOLVED)

    def test_sensitive_field_requires_explicit_configured_preference(self):
        options = ("Decline to answer", "Female", "Male")
        explicit = self.resolver.resolve(question("Gender", control=ControlType.CHOICE, options=options), "A")
        self.assertEqual(explicit.answer.value, "Decline to answer")
        data = profile_data()
        del data["qa_bank"]["eeo_gender"]
        self.assertEqual(DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
                         .resolve(question("Gender", options=options), "A").status,
                         ResolutionStatus.UNRESOLVED)

    def test_option_mapping_exact_only(self):
        yes_no = ("Yes", "No")
        authorized = self.resolver.resolve(question("Are you authorized to work in the US?",
                                                    control=ControlType.CHOICE, options=yes_no), "A")
        sponsorship = self.resolver.resolve(question("Do you require visa sponsorship?",
                                                     control=ControlType.CHOICE, options=yes_no), "A")
        self.assertEqual(authorized.answer.value, "Yes")
        self.assertEqual(sponsorship.answer.value, "No")
        ambiguous = self.resolver.resolve(question("Are you authorized to work in the US?",
                                                   options=("Yes", "No", "Maybe")), "A")
        self.assertEqual(ambiguous.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(ambiguous.answer)

    def test_education_multiple_records_need_identity(self):
        self.assertEqual(self.resolver.resolve(question("School", section="Education"), "A").answer.value,
                         "Example University")
        data = profile_data()
        data["education"].append({"institution": "Other University", "degree": "M.S."})
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
        self.assertEqual(resolver.resolve(question("Degree", section="Education"), "A").status,
                         ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolver.resolve(question("Degree", section="Education",
                                                   record="Other University"), "A").answer.value, "M.S.")

    def test_repeated_fixture_email_and_ref_independence(self):
        step_one = question("Email Address", ref="e7")
        step_three = question("Confirm Email", ref="e83")
        first = self.resolver.resolve(step_one, "A")
        repeated = self.resolver.resolve(step_three, "A")
        self.assertEqual(first.canonical.semantic_key, repeated.canonical.semantic_key)
        self.assertEqual(first.answer, repeated.answer)
        self.assertEqual(repeated, self.resolver.resolve(replace(step_three, target_ref="e99"), "A"))

    def test_current_employer_requires_one_explicit_current_record(self):
        field = question("Current employer", section="Employment")
        choice = question("Are you currently employed?", control=ControlType.CHOICE,
                          options=("Yes", "No"), section="Employment")
        for records in ([], [{"company": "One", "is_current": False}],
                        [{"company": "One", "is_current": True},
                         {"company": "Two", "is_current": True}]):
            with self.subTest(records=records):
                data = profile_data()
                data["work_history"] = records
                resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
                self.assertEqual(resolver.resolve(field, "A").status, ResolutionStatus.UNRESOLVED)
                self.assertEqual(resolver.resolve(choice, "A").status, ResolutionStatus.UNRESOLVED)
        data = profile_data()
        data["work_history"] = [{"company": "One", "is_current": True},
                                {"company": "Old", "is_current": False}]
        resolver = DeterministicAnswerResolver(CandidateProfile.from_mapping(data))
        self.assertEqual(resolver.resolve(field, "A").answer.value, "One")
        self.assertEqual(resolver.resolve(choice, "A").answer.value, "Yes")

    def test_partial_nonempty_answer_is_not_safe(self):
        data = profile_data()
        data["personal_info"]["email"] = "probably ada@example.test"
        result = DeterministicAnswerResolver(CandidateProfile.from_mapping(data)).resolve(
            question("Unclear Email or Username"), "A")
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.answer)


if __name__ == "__main__":
    unittest.main()
