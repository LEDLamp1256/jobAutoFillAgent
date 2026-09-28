"""Deterministic authentication and intervention tests; no real credentials."""

import unittest

from jobagent.authentication import (
    InterventionReason, LoginIdentity, LoginOrchestrator, LoginStatus, PageKind,
    classify_page, resume_after_human,
)
from jobagent.domain import (
    ApplicationObservation, ControlType, NavigationControl, QuestionObservation,
)
from jobagent.snapshot import SnapshotAccessChallenge


SECRET = "synthetic-test-secret"


def login_observation(index, validation=()):
    ref = f"e{index}"
    return ApplicationObservation(
        ref, "https://example.test/login", "Sign In",
        questions=(QuestionObservation("Email Address", ControlType.TEXT,
                                       target_ref=f"{ref}-identity"),
                   QuestionObservation("Password", ControlType.SECRET,
                                       target_ref=f"{ref}-password")),
        navigation_controls=(NavigationControl("Sign In", target_ref=f"{ref}-sign-in"),),
        validation_messages=validation)


def application_observation(index):
    return ApplicationObservation(f"e{index}", "https://example.test/apply", "Personal Information",
                                  questions=(QuestionObservation("First Name", ControlType.TEXT),))


def mfa_observation(index):
    return ApplicationObservation(f"e{index}", "https://example.test/verify", "Verification Code",
                                  questions=(QuestionObservation("One-time code", ControlType.TEXT),))


class FakeCredentials:
    def __init__(self, password=SECRET):
        self.password = password
        self.lookups = []

    async def get_password(self, account_id):
        self.lookups.append(account_id)
        return self.password


class FakeAuthBrowser:
    def __init__(self, after_login="application"):
        self.current = login_observation(1)
        self.after_login = after_login
        self.calls = []
        self.index = 1

    def _fresh(self):
        self.index += 1
        self.current = login_observation(self.index)
        return self.current

    async def fill_login_identity(self, target_ref, observation_id, username):
        assert observation_id == self.current.observation_id
        assert target_ref.endswith("-identity") and username == "user@example.test"
        self.calls.append(("identity", observation_id))
        return self._fresh()

    async def fill_login_password(self, target_ref, observation_id, password):
        assert observation_id == self.current.observation_id
        assert target_ref.endswith("-password") and password == SECRET
        self.calls.append(("password", observation_id))
        return self._fresh()

    async def activate_login(self, target_ref, observation_id):
        assert observation_id == self.current.observation_id
        assert target_ref.endswith("-sign-in")
        self.calls.append(("sign_in", observation_id))
        if self.after_login == "challenge":
            raise SnapshotAccessChallenge("site returned HTTP 429 access challenge")
        self.index += 1
        self.current = {"application": application_observation,
                        "mfa": mfa_observation,
                        "failure": lambda index: login_observation(index, ("Incorrect password",))}[
                            self.after_login](self.index)
        return self.current

    async def observe(self):
        self.calls.append(("observe", self.current.observation_id))
        self.index += 1
        self.current = application_observation(self.index)
        return self.current


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sso_email_verification_and_ambiguous_login_are_human_states(self):
        sso = ApplicationObservation("sso", "https://example.test/login", "Sign In",
                                     navigation_controls=(NavigationControl("Continue with Google"),))
        email = ApplicationObservation("email", "https://example.test/verify", "Verify your email")
        ambiguous = login_observation(1)
        ambiguous = ApplicationObservation(
            ambiguous.observation_id, ambiguous.location, ambiguous.heading,
            questions=ambiguous.questions + (QuestionObservation("Username", ControlType.TEXT,
                                                                  target_ref="extra"),),
            navigation_controls=ambiguous.navigation_controls)
        self.assertEqual(classify_page(sso).reason, InterventionReason.SSO_REQUIRED)
        self.assertEqual(classify_page(email).reason,
                         InterventionReason.EMAIL_VERIFICATION_REQUIRED)
        self.assertEqual(classify_page(ambiguous).reason,
                         InterventionReason.HUMAN_JUDGMENT_REQUIRED)

    async def test_normal_login_uses_fresh_refs_and_keeps_secret_out_of_result(self):
        browser = FakeAuthBrowser()
        provider = FakeCredentials()
        result = await LoginOrchestrator(browser, provider).attempt(
            browser.current, LoginIdentity("account-1", "user@example.test"))
        self.assertEqual(result.status, LoginStatus.AUTHENTICATED)
        self.assertEqual([name for name, _ in browser.calls], ["identity", "password", "sign_in"])
        self.assertEqual([ref for _, ref in browser.calls], ["e1", "e2", "e3"])
        self.assertEqual(provider.lookups, ["account-1"])
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, repr(browser.calls))

    async def test_login_to_mfa_requires_human(self):
        browser = FakeAuthBrowser("mfa")
        result = await LoginOrchestrator(browser, FakeCredentials()).attempt(
            browser.current, LoginIdentity("account-1", "user@example.test"))
        self.assertEqual(result.status, LoginStatus.HUMAN_INTERVENTION_REQUIRED)
        self.assertEqual(result.reason, InterventionReason.MFA_REQUIRED)

    async def test_login_failure_has_no_retry(self):
        browser = FakeAuthBrowser("failure")
        result = await LoginOrchestrator(browser, FakeCredentials()).attempt(
            browser.current, LoginIdentity("account-1", "user@example.test"))
        self.assertEqual(result.status, LoginStatus.LOGIN_FAILED)
        self.assertEqual([name for name, _ in browser.calls].count("sign_in"), 1)

    async def test_challenge_before_login_never_looks_up_credentials(self):
        browser = FakeAuthBrowser()
        provider = FakeCredentials()
        challenge = ApplicationObservation("challenge", "https://example.test/challenge",
                                           "Verify you are human")
        result = await LoginOrchestrator(browser, provider).attempt(
            challenge, LoginIdentity("account-1", "user@example.test"))
        self.assertEqual(result.status, LoginStatus.HUMAN_INTERVENTION_REQUIRED)
        self.assertEqual(result.reason, InterventionReason.ACCESS_CHALLENGE)
        self.assertEqual(provider.lookups, [])
        self.assertEqual(browser.calls, [])

    async def test_challenge_after_login_requires_human(self):
        browser = FakeAuthBrowser("challenge")
        result = await LoginOrchestrator(browser, FakeCredentials()).attempt(
            browser.current, LoginIdentity("account-1", "user@example.test"))
        self.assertEqual(result.status, LoginStatus.HUMAN_INTERVENTION_REQUIRED)
        self.assertEqual(result.reason, InterventionReason.ACCESS_CHALLENGE)

    async def test_explicit_resume_observes_fresh_state(self):
        browser = FakeAuthBrowser()
        old = browser.current
        prompts = []
        fresh = await resume_after_human(browser, lambda message: prompts.append(message))
        self.assertEqual(len(prompts), 1)
        self.assertNotEqual(fresh.observation_id, old.observation_id)
        self.assertEqual(browser.calls, [("observe", old.observation_id)])
        self.assertEqual(classify_page(fresh).kind, PageKind.APPLICATION)


if __name__ == "__main__":
    unittest.main()
