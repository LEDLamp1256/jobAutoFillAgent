"""Offline V2-10 window, launcher, control-plane, and login integration tests."""

import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from jobagent.application_launcher import ApplicationLauncher, application_url
from jobagent.batch_domain import Blocker, HumanAction, HumanActor, HumanAuthorization, TaskStatus
from jobagent.control_plane import LocalControlPlane
from jobagent.dedupe import ListingInput
from jobagent.domain import ApplicationObservation, NavigationControl
from jobagent.managed_runtime import MCPManagedWindows
from jobagent.mcp_browser import MCPServerCommand
from jobagent.persistence import BatchStore
from jobagent.runtime_worker import LaunchAndLoginWorker, LocalLoginConfiguration
from jobagent.scheduler import ApplicationScheduler
from tests.test_authentication import FakeAuthBrowser, SECRET, application_observation


class FakeManagedBrowser(FakeAuthBrowser):
    def __init__(self, *, login=False, after_login="application"):
        super().__init__(after_login)
        self.login = login
        self.closed = False
        self.foreground_count = 0
        self.navigations = []
        self.tab_count = 1
        self.page_marker = None
        self.close_fails = False
        self.tab_reads = 0

    async def __aenter__(self):
        return self

    async def managed_tabs(self):
        self.tab_reads += 1
        return ("" if self.closed else "### Open tabs\n" +
                "\n".join(f"- {index}: test" for index in range(self.tab_count)))

    async def set_managed_page_token(self, token):
        self.page_marker = token

    async def managed_page_token_matches(self, token):
        return self.page_marker == token

    async def navigate(self, url):
        self.navigations.append(url)
        if not self.login:
            self.current = application_observation(1)
        return self.current

    async def observe(self):
        if self.current.heading == "Sign In" and self.login:
            location = self.current.location
            self.current = replace(self._fresh(), location=location)
            return self.current
        self.index += 1
        self.current = replace(self.current, observation_id=f"e{self.index}")
        return self.current

    async def bring_managed_tab_to_front(self):
        if self.closed:
            raise RuntimeError("closed")
        self.foreground_count += 1

    async def close(self):
        self.closed = True
        if self.close_fails:
            raise RuntimeError("synthetic cleanup failure")


class RecordingLoginConfiguration(LocalLoginConfiguration):
    def __init__(self, path):
        super().__init__(path)
        self.password_lookups = []

    async def get_password(self, account_id):
        self.password_lookups.append(account_id)
        return await super().get_password(account_id)


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = BatchStore(self.path / "batch.sqlite3")
        self.run = self.store.create_run(("board",), 3)
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser()
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)

    def tearDown(self):
        self.windows.close()
        self.store.close()
        self.temp.cleanup()

    def queue(self, name="one", url=None):
        listing, _ = self.store.register_listing(ListingInput(
            "board", name, "Engineer", f"https://example.test/jobs/{name}", name,
            application_url=url or f"https://example.test/apply/{name}"))
        return self.store.queue_task(self.run.id, listing.id)

    def scheduler(self, config=None):
        worker = LaunchAndLoginWorker(self.store, self.launcher,
                                      LocalLoginConfiguration(config or self.path / "missing.json"))
        return ApplicationScheduler(self.store, worker, self.windows, max_open_applications=3)

    def test_distinct_associations_passive_reads_and_explicit_foreground(self):
        a, b = self.queue("a"), self.queue("b")
        scheduler = self.scheduler()
        self.assertEqual(scheduler.step().task.id, a.id)
        first = self.store.get_task(a.id).browser_session_id
        self.assertEqual(self.windows.allocate(a.id), first)
        # V2-11 current-page work yields HUMAN_PAUSED for the unknown field.
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(scheduler.step().task.id, b.id)
        second = self.store.get_task(b.id).browser_session_id
        self.assertNotEqual(first, second)
        control = LocalControlPlane(self.store, scheduler)
        tab_reads = [browser.tab_reads for browser in self.browsers]
        control.get_application(a.id)
        control.list_applications()
        control.list_attention_required()
        self.assertEqual([browser.tab_reads for browser in self.browsers], tab_reads)
        self.assertEqual([browser.foreground_count for browser in self.browsers], [0, 0])
        control.bring_window_to_front(a.id)
        self.assertEqual([browser.foreground_count for browser in self.browsers], [1, 0])
        self.assertEqual(self.browsers[0].navigations, ["https://example.test/apply/a"])
        self.assertEqual(self.browsers[1].navigations, ["https://example.test/apply/b"])
        self.assertNotIn("target_ref", (self.path / "batch.sqlite3").read_bytes().decode("latin1"))
        scheduler.release_window(a.id)
        self.assertFalse(self.windows.exists(first))
        self.assertTrue(self.windows.exists(second))
        control.bring_window_to_front(b.id)
        self.assertEqual([browser.foreground_count for browser in self.browsers], [1, 1])

    def test_stale_and_restart_never_trust_persisted_identity(self):
        task = self.queue()
        scheduler = self.scheduler()
        scheduler.step()
        old = self.store.get_task(task.id).browser_session_id
        self.browsers[0].closed = True
        self.assertFalse(self.windows.exists(old))
        with self.assertRaises(LookupError):
            scheduler.bring_window_to_front(task.id)
        # A backend restart has no live in-memory association, even with SQLite intact.
        other_browsers = []
        def factory(_):
            browser = FakeManagedBrowser()
            other_browsers.append(browser)
            return browser
        restarted_windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        try:
            self.assertFalse(restarted_windows.exists(old))
            self.store.resume_by_human(task.id, authorization=HumanAuthorization(
                HumanActor.LOCAL_OWNER, HumanAction.RESUME))
            restarted = ApplicationScheduler(
                self.store,
                LaunchAndLoginWorker(self.store, ApplicationLauncher(restarted_windows),
                                     LocalLoginConfiguration(self.path / "missing.json")),
                restarted_windows, max_open_applications=3)
            result = restarted.step()
            self.assertNotEqual(result.task.browser_session_id, old)
            self.assertTrue(result.task.browser_session_id)
            self.assertEqual(other_browsers[0].navigations, ["https://example.test/apply/one"])
        finally:
            restarted_windows.close()

    def test_invalid_url_fails_before_browser_navigation(self):
        listing, _ = self.store.register_listing(ListingInput(
            "board", "valid application URL", "Engineer", "https://example.test/jobs/one",
            "valid-one", application_url="https://example.test/apply/one"))
        self.assertEqual(application_url(listing), "https://example.test/apply/one")
        for value in (None, "ftp://example.test/apply", "http://example.test/apply"):
            from dataclasses import replace
            with self.assertRaises(ValueError):
                application_url(replace(listing, application_url=value))
        self.assertEqual(self.browsers, [])

        no_application, _ = self.store.register_listing(ListingInput(
            "board", "no application URL", "Engineer", "https://example.test/jobs/empty",
            "empty"))
        queued = self.store.queue_task(self.run.id, no_application.id)
        result = self.scheduler().step()
        self.assertEqual((result.task.id, result.task.status), (queued.id, TaskStatus.FAILED))
        self.assertEqual(self.browsers[0].navigations, [])
        self.assertEqual(self.windows.open_count(), 0)

    def test_login_config_and_pause_keep_secret_out_of_persistence(self):
        self.windows.close()
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser(login=True, after_login="mfa")
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        config = self.path / "config.json"
        config.write_text(json.dumps({"login_accounts": {
            "example.test": {"username": "user@example.test", "password": SECRET}}}))
        task = self.queue()
        scheduler = self.scheduler(config)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = scheduler.step()
        self.assertEqual((result.task.status, result.task.blocker),
                         (TaskStatus.HUMAN_PAUSED, Blocker.MFA_REQUIRED))
        self.assertEqual([name for name, _ in self.browsers[0].calls],
                         ["identity", "password", "sign_in"])
        self.assertNotIn(SECRET, (self.path / "batch.sqlite3").read_bytes().decode("latin1"))
        self.assertNotIn(SECRET, repr(self.store.get_report(task.id)))
        self.assertNotIn(SECRET, repr(LocalControlPlane(self.store, scheduler).get_application(task.id)))
        self.assertNotIn(SECRET, output.getvalue() + errors.getvalue())
        with self.assertRaises(PermissionError):
            scheduler.resume_application(task.id, None)
        resumed = scheduler.resume_application(
            task.id, HumanAuthorization(HumanActor.LOCAL_OWNER, HumanAction.RESUME))
        self.assertIsNone(resumed.browser_session_id)

    def test_login_success_uses_config_and_fresh_observations(self):
        self.windows.close()
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser(login=True)
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        config = self.path / "config.json"
        config.write_text(json.dumps({"login_accounts": {
            "example.test": {"username": "user@example.test", "password": SECRET}}}))
        task = self.queue()
        result = self.scheduler(config).step()
        self.assertEqual((result.task.status, result.outcome.kind.value),
                         (TaskStatus.HUMAN_PAUSED, "human_blocked"))
        self.assertEqual(self.browsers[0].calls,
                         [("identity", "e2"), ("password", "e3"), ("sign_in", "e4")])
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, (self.path / "batch.sqlite3").read_bytes().decode("latin1"))
        self.assertEqual(self.browsers[0].foreground_count, 0)

    def test_missing_credentials_captcha_sso_and_unusual_verification_pause(self):
        self.windows.close()
        observations = [
            None,
            ApplicationObservation("captcha", "https://example.test/login", "Verify you are human"),
            ApplicationObservation("sso", "https://example.test/login", "Sign In",
                                   navigation_controls=(NavigationControl("Continue with Google"),)),
            ApplicationObservation("qr", "https://example.test/login", "Scan this QR code"),
        ]
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser(login=True)
            state = observations[len(self.browsers)]
            if state is not None:
                browser.current = state
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        scheduler = ApplicationScheduler(
            self.store,
            LaunchAndLoginWorker(self.store, self.launcher,
                                 LocalLoginConfiguration(self.path / "missing.json")),
            self.windows, max_open_applications=4)
        for index, blocker in enumerate((Blocker.LOGIN_REQUIRED, Blocker.CAPTCHA_REQUIRED,
                                          Blocker.LOGIN_REQUIRED, Blocker.MFA_REQUIRED)):
            task = self.queue(f"challenge-{index}")
            result = scheduler.step()
            self.assertEqual((result.task.id, result.task.status, result.task.blocker),
                             (task.id, TaskStatus.HUMAN_PAUSED, blocker))
            self.assertEqual(self.browsers[index].calls, [])

    def test_restart_during_login_requires_human_before_any_retry(self):
        config = self.path / "config.json"
        config.write_text(json.dumps({"login_accounts": {
            "example.test": {"username": "user@example.test", "password": SECRET}}}))
        task = self.queue()
        previous = self.windows.allocate(task.id)
        self.store.start(task.id, browser_session_id=previous)
        self.store.advance_state(task.id, TaskStatus.AUTHENTICATING)
        self.store.close()
        self.store = BatchStore(self.path / "batch.sqlite3")
        self.windows.close()
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser(login=True)
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        result = self.scheduler(config).step()
        self.assertEqual((result.task.status, result.task.blocker),
                         (TaskStatus.HUMAN_PAUSED, Blocker.LOGIN_REQUIRED))
        self.assertNotEqual(result.task.browser_session_id, previous)
        self.assertEqual(self.browsers[0].calls, [])

    def test_insecure_login_redirect_never_receives_configured_credentials(self):
        config = self.path / "config.json"
        config.write_text(json.dumps({"login_accounts": {
            "example.test": {"username": "user@example.test", "password": SECRET}}}))
        self.windows.close()
        self.browsers = []
        def factory(_):
            browser = FakeManagedBrowser(login=True)
            original = browser.current
            browser.current = ApplicationObservation(
                original.observation_id, "http://example.test/login", original.heading,
                questions=original.questions, navigation_controls=original.navigation_controls)
            self.browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        task = self.queue()
        result = self.scheduler(config).step()
        self.assertEqual((result.task.status, result.task.blocker),
                         (TaskStatus.HUMAN_PAUSED, Blocker.LOGIN_REQUIRED))
        self.assertEqual(self.browsers[0].calls, [])

    def test_password_redirect_requires_https_exact_host_and_same_account(self):
        class RedirectingBrowser(FakeManagedBrowser):
            def __init__(self, destination):
                super().__init__(login=True)
                self.destination = destination

            async def fill_login_identity(self, target_ref, observation_id, username):
                observation = await super().fill_login_identity(target_ref, observation_id, username)
                self.current = replace(observation, location=self.destination)
                return self.current

        self.windows.close()
        destinations = ("http://example.test/login", "https://unknown.test/login",
                        "https://other.test/login")
        browsers = []
        def factory(_):
            browser = RedirectingBrowser(destinations[len(browsers)])
            browsers.append(browser)
            return browser
        self.windows = MCPManagedWindows(lambda: MCPServerCommand("node", ("mcp.js",)), factory)
        self.launcher = ApplicationLauncher(self.windows)
        config = self.path / "config.json"
        config.write_text(json.dumps({"login_accounts": {
            "example.test": {"username": "user@example.test", "password": SECRET},
            "other.test": {"username": "other@example.test", "password": "other-synthetic-secret"}}}))
        credentials = RecordingLoginConfiguration(config)
        scheduler = ApplicationScheduler(self.store, LaunchAndLoginWorker(
            self.store, self.launcher, credentials), self.windows, max_open_applications=3)
        for index in range(3):
            task = self.queue(f"redirect-{index}")
            result = scheduler.step()
            self.assertEqual((result.task.id, result.task.status, result.task.blocker),
                             (task.id, TaskStatus.HUMAN_PAUSED, Blocker.LOGIN_REQUIRED))
            self.assertEqual([name for name, _ in browsers[index].calls], ["identity"])
        self.assertEqual(credentials.password_lookups, [])
        self.assertNotIn(SECRET, (self.path / "batch.sqlite3").read_bytes().decode("latin1"))

    def test_extra_or_replacement_tab_is_unavailable_and_never_foregrounded(self):
        a, b = self.queue("identity-a"), self.queue("identity-b")
        scheduler = self.scheduler()
        scheduler.step()
        first = self.store.get_task(a.id).browser_session_id
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.HUMAN_PAUSED)
        scheduler.step()
        second = self.store.get_task(b.id).browser_session_id
        self.browsers[0].tab_count = 2
        self.assertFalse(self.windows.exists(first))
        with self.assertRaises(LookupError):
            self.windows.bring_to_front(first)
        self.assertEqual(self.browsers[0].foreground_count, 0)
        self.browsers[0].tab_count = 0
        self.assertFalse(self.windows.exists(first))
        self.browsers[0].tab_count = 1
        self.browsers[0].page_marker = None  # Replacement now occupies index zero.
        self.assertFalse(self.windows.exists(first))
        with self.assertRaises(LookupError):
            self.windows.bring_to_front(first)
        self.assertTrue(self.windows.exists(second))
        self.windows.bring_to_front(second)
        self.assertEqual([browser.foreground_count for browser in self.browsers], [0, 1])

    def test_cleanup_attempts_every_session_after_first_close_failure(self):
        self.windows.allocate("first")
        self.windows.allocate("second")
        directories = [Path(window.artifacts.name) for window in self.windows._windows.values()]
        for directory in directories:
            (directory / "synthetic-page.yml").write_text("synthetic fixture only")
        self.browsers[0].close_fails = True
        with self.assertRaisesRegex(RuntimeError, "1 session"):
            self.windows.close()
        self.assertEqual([browser.closed for browser in self.browsers], [True, True])
        self.assertEqual(self.windows.open_count(), 0)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_mcp_subprocess_artifacts_stay_in_owned_temporary_directory(self):
        script = self.path / "synthetic_mcp.py"
        script.write_text("""import sys
from pathlib import Path
output = Path(sys.argv[sys.argv.index('--output-dir') + 1])
(Path.cwd() / 'synthetic-managed-page.yml').write_text('fixture snapshot')
(output / 'synthetic-managed-trace.zip').write_text('fixture trace')
""")
        caller_directory = self.path / "caller-owned"
        caller_directory.mkdir()
        caller_file = caller_directory / "keep.txt"
        caller_file.write_text("user-owned")
        observed = []
        def factory(command):
            observed.append(command)
            subprocess.run([command.command, *command.args], cwd=command.cwd,
                           check=True, capture_output=True, text=True)
            return FakeManagedBrowser()
        windows = MCPManagedWindows(
            lambda: MCPServerCommand(sys.executable, (str(script),), cwd=caller_directory), factory)
        try:
            window_id = windows.allocate("fixture-task")
            directory = observed[0].cwd
            self.assertEqual(observed[0].args[-2:], ("--output-dir", str(directory)))
            self.assertNotEqual(directory, caller_directory)
            self.assertFalse(directory.is_relative_to(Path(__file__).resolve().parents[1]))
            self.assertTrue((directory / "synthetic-managed-page.yml").is_file())
            self.assertTrue((directory / "synthetic-managed-trace.zip").is_file())
            self.assertFalse((Path.cwd() / "synthetic-managed-page.yml").exists())
            self.assertFalse((caller_directory / "synthetic-managed-page.yml").exists())
            windows.release(window_id)
            self.assertFalse(directory.exists())
            self.assertEqual(caller_file.read_text(), "user-owned")
        finally:
            windows.close()

    def test_scheduler_pauses_ambiguous_session_without_closing_extra_tab(self):
        task = self.queue("ambiguous")
        scheduler = self.scheduler()
        scheduler.step()
        window_id = self.store.get_task(task.id).browser_session_id
        self.browsers[0].tab_count = 2
        self.store.resume_by_human(task.id, authorization=HumanAuthorization(
            HumanActor.LOCAL_OWNER, HumanAction.RESUME))
        result = scheduler.step()
        self.assertEqual((result.task.status, result.task.blocker),
                         (TaskStatus.HUMAN_PAUSED, Blocker.OTHER))
        self.assertEqual(self.windows.window_for_task(task.id), window_id)
        self.assertEqual(len(self.browsers), 1)
        self.assertFalse(self.browsers[0].closed)
        self.assertEqual(self.windows.open_count(), 1)

    def test_replacement_at_index_zero_fails_without_prior_extra_tab(self):
        task = self.queue("replacement")
        scheduler = self.scheduler()
        scheduler.step()
        window_id = self.store.get_task(task.id).browser_session_id
        self.browsers[0].page_marker = None
        self.assertFalse(self.windows.exists(window_id))
        with self.assertRaises(LookupError):
            scheduler.bring_window_to_front(task.id)
        self.assertEqual(self.browsers[0].foreground_count, 0)

    def test_managed_page_disappears_without_replacement(self):
        task = self.queue("disappeared")
        self.scheduler().step()
        window_id = self.store.get_task(task.id).browser_session_id
        self.browsers[0].tab_count = 0
        self.assertFalse(self.windows.exists(window_id))
        self.assertFalse(self.windows.passive_available(window_id))


if __name__ == "__main__":
    unittest.main()
