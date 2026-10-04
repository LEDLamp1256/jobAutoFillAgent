"""Offline scheduler contract tests with temporary SQLite and managed-window fakes."""

import tempfile
import unittest
from pathlib import Path

from jobagent.application_worker import ReviewIssue, WorkerOutcome, WorkerYield
from jobagent.batch_domain import (
    Blocker, FailureReason, HumanAction, HumanActor, HumanAuthorization, Ownership, TaskStatus,
)
from jobagent.dedupe import ListingInput
from jobagent.persistence import BatchStore
from jobagent.scheduler import ApplicationScheduler


class FakeWindows:
    serial = 0

    def __init__(self):
        self.by_task = {}
        self.foregrounded = []

    def open_count(self):
        return len(self.by_task)

    def window_for_task(self, task_id):
        return self.by_task.get(task_id)

    def exists(self, window_id):
        return window_id in self.by_task.values()

    def allocate(self, task_id):
        if task_id in self.by_task:
            raise AssertionError("task already has a window")
        FakeWindows.serial += 1
        window_id = f"window-{FakeWindows.serial}"
        self.by_task[task_id] = window_id
        return window_id

    def release(self, window_id):
        task_id = next(task for task, value in self.by_task.items() if value == window_id)
        del self.by_task[task_id]

    def bring_to_front(self, window_id):
        self.foregrounded.append(window_id)


class FakeWorker:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.requests = []
        self.running = False
        self.max_active = 0

    def work_until_yield(self, request):
        if self.running:
            raise AssertionError("parallel invocation")
        self.running = True
        self.max_active = max(self.max_active, int(self.running))
        try:
            self.requests.append(request)
            return self.outcomes.pop(0)
        finally:
            self.running = False


def owner(action):
    return HumanAuthorization(HumanActor.LOCAL_OWNER, action)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "batch.sqlite3"
        self.store = BatchStore(self.path)
        self.run = self.store.create_run(("board",), 10)
        self.windows = FakeWindows()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def queue(self, name):
        listing, _ = self.store.register_listing(ListingInput(
            "board", name, "Engineer", f"https://jobs.test/{name}", name))
        return self.store.queue_task(self.run.id, listing.id)

    def scheduler(self, outcomes, cap=3):
        self.worker = FakeWorker(outcomes)
        return ApplicationScheduler(self.store, self.worker, self.windows,
                                    max_open_applications=cap)

    def test_yield_next_attention_and_explicit_foreground(self):
        a, b, c = (self.queue(name) for name in ("a", "b", "c"))
        issues = (ReviewIssue("Question 1", "needs_answer", "page 1"),
                  ReviewIssue("Question 2", "needs_answer", "page 1"))
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, issues, Blocker.NEEDS_ANSWER, page_or_step="page 1"),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW, issues),
            WorkerOutcome(WorkerYield.PROGRESS),
        ])
        first = scheduler.step()
        self.assertEqual((first.task.id, first.task.status, first.task.ownership),
                         (a.id, TaskStatus.HUMAN_PAUSED, Ownership.HUMAN_OWNED))
        self.assertEqual(scheduler.step().task.id, b.id)
        self.assertEqual(self.store.get_task(b.id).status, TaskStatus.READY_FOR_REVIEW)
        self.assertEqual(scheduler.step().task.id, c.id)
        self.assertEqual(self.worker.max_active, 1)
        self.assertEqual(len(scheduler.attention()), 2)
        self.assertEqual(len(scheduler.attention()[0].report.entries), 2)
        self.assertEqual(self.windows.foregrounded, [])
        scheduler.tasks()
        scheduler.bring_window_to_front(a.id)
        self.assertEqual(self.windows.foregrounded, [self.windows.by_task[a.id]])
        self.assertEqual(self.store.get_task(a.id).browser_session_id, self.windows.by_task[a.id])
        self.assertEqual(self.store.get_task(b.id).browser_session_id, self.windows.by_task[b.id])

    def test_resume_requires_authorization_and_fresh_observation(self):
        a, b = self.queue("a"), self.queue("b")
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.MFA_REQUIRED),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
            WorkerOutcome(WorkerYield.PROGRESS),
        ], cap=2)
        scheduler.step()
        self.assertEqual(scheduler.step().task.id, b.id)
        self.assertIsNone(scheduler.step())
        with self.assertRaises(PermissionError):
            scheduler.resume_application(a.id, None)
        with self.assertRaises(PermissionError):
            scheduler.resume_application(a.id, owner(HumanAction.FINAL_REVIEW))
        previous_window = self.windows.by_task[a.id]
        resumed = scheduler.resume_application(a.id, owner(HumanAction.RESUME))
        self.assertEqual((resumed.status, resumed.ownership, resumed.browser_session_id),
                         (TaskStatus.QUEUED, Ownership.AUTOMATION_OWNED, None))
        self.assertEqual(scheduler.step().task.id, a.id)
        self.assertEqual(self.worker.requests[-1].window_id, previous_window)
        self.assertTrue(self.worker.requests[-1].fresh_observation_required)
        self.assertTrue(self.worker.requests[-1].resumed_by_human)
        self.assertEqual(self.store.get_task(a.id).browser_session_id, previous_window)

    def test_window_cap_release_and_restart(self):
        a, b, c = (self.queue(name) for name in ("a", "b", "c"))
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.CAPTCHA_REQUIRED),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
            WorkerOutcome(WorkerYield.PROGRESS),
        ], cap=2)
        scheduler.step()
        scheduler.step()
        self.assertIsNone(scheduler.step())
        self.assertEqual(self.store.get_task(c.id).status, TaskStatus.QUEUED)
        self.assertEqual(self.windows.open_count(), 2)
        self.store.close()
        self.store = BatchStore(self.path)
        restarted = ApplicationScheduler(self.store, self.worker, self.windows, max_open_applications=2)
        self.assertEqual([item.task.status for item in restarted.attention()],
                         [TaskStatus.HUMAN_PAUSED, TaskStatus.READY_FOR_REVIEW])
        self.assertIsNone(restarted.step())
        restarted.release_window(a.id)
        self.assertIsNone(self.store.get_task(a.id).browser_session_id)
        self.assertEqual(restarted.step().task.id, c.id)
        self.assertEqual(self.windows.open_count(), 2)
        self.assertNotEqual(self.store.get_task(c.id).browser_session_id, self.windows.by_task[b.id])

    def test_ready_for_review_never_runs_or_submits(self):
        a = self.queue("a")
        scheduler = self.scheduler([WorkerOutcome(WorkerYield.READY_FOR_REVIEW)])
        scheduler.step()
        self.assertIsNone(scheduler.step())
        self.assertEqual(len(self.worker.requests), 1)
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.READY_FOR_REVIEW)
        self.assertIsNone(self.store.get_task(a.id).submitted_at)

    def test_active_recovery_requires_fresh_observation_and_single_worker(self):
        a, b = self.queue("a"), self.queue("b")
        scheduler = self.scheduler([WorkerOutcome(WorkerYield.PROGRESS),
                                    WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.OTHER)])
        scheduler.step()
        self.store.close()
        self.store = BatchStore(self.path)
        restarted = ApplicationScheduler(self.store, self.worker, self.windows, max_open_applications=3)
        self.assertEqual(restarted.step().task.id, a.id)
        self.assertTrue(self.worker.requests[-1].fresh_observation_required)
        self.assertEqual(self.store.get_task(b.id).status, TaskStatus.QUEUED)

    def test_resumed_task_joins_back_of_queue(self):
        a = self.queue("a")
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.OTHER),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
        ])
        scheduler.step()
        b = self.queue("b")
        scheduler.resume_application(a.id, owner(HumanAction.RESUME))
        self.assertEqual(scheduler.step().task.id, b.id)
        self.assertEqual(scheduler.step().task.id, a.id)

    def test_last_blocker_clears_only_after_successful_fresh_progress(self):
        task = self.queue("retain-until-observed")
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.UNSUPPORTED_CONTROL,
                          page_or_step="Application"),
            WorkerOutcome(WorkerYield.PROGRESS, page_or_step="page_advanced"),
        ])
        self.assertEqual(scheduler.step().task.blocker, Blocker.UNSUPPORTED_CONTROL)
        resumed = scheduler.resume_application(task.id, owner(HumanAction.RESUME))
        self.assertEqual(resumed.blocker, Blocker.UNSUPPORTED_CONTROL)
        self.assertEqual(scheduler.step().task.blocker, None)
        self.assertTrue(self.worker.requests[-1].fresh_observation_required)

    def test_failed_task_releases_window_and_capacity(self):
        a, b = self.queue("a"), self.queue("b")
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.FAILED, failure=FailureReason.SITE_ERROR),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
        ], cap=1)
        self.assertEqual(scheduler.step().task.status, TaskStatus.FAILED)
        self.assertEqual(self.windows.open_count(), 0)
        self.assertIsNone(self.store.get_task(a.id).browser_session_id)
        self.assertEqual(scheduler.step().task.id, b.id)

    def test_stale_active_window_is_reassigned_after_restart(self):
        a = self.queue("a")
        scheduler = self.scheduler([WorkerOutcome(WorkerYield.PROGRESS)])
        old = scheduler.step().task.browser_session_id
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.LAUNCHING)
        self.store.close()
        self.store = BatchStore(self.path)
        replacement_windows = FakeWindows()
        recovered_worker = FakeWorker([WorkerOutcome(WorkerYield.PROGRESS)])
        restarted = ApplicationScheduler(self.store, recovered_worker,
                                         replacement_windows, max_open_applications=1)
        result = restarted.step()
        self.assertEqual(result.task.status, TaskStatus.LAUNCHING)
        self.assertNotEqual(result.task.browser_session_id, old)
        self.assertEqual(result.task.browser_session_id, replacement_windows.by_task[a.id])
        self.assertEqual(len(recovered_worker.requests), 1)
        self.assertTrue(recovered_worker.requests[0].fresh_observation_required)
        self.assertIsNone(result.task.submitted_at)

    def test_explicit_resume_reopens_a_known_closed_managed_window(self):
        class ClosedWindows(FakeWindows):
            closed = None

            def exists(self, window_id):
                return window_id != self.closed and super().exists(window_id)

            def recoverable_closed(self, window_id):
                return window_id == self.closed

        self.windows = ClosedWindows()
        task = self.queue("closed")
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.LOGIN_REQUIRED),
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.LOGIN_REQUIRED),
        ])
        first = scheduler.step()
        old = first.task.browser_session_id
        self.windows.closed = old
        scheduler.resume_application(task.id, owner(HumanAction.RESUME))
        reopened = scheduler.step()
        self.assertNotEqual(reopened.task.browser_session_id, old)
        self.assertEqual(reopened.task.status, TaskStatus.HUMAN_PAUSED)
        self.assertTrue(self.worker.requests[-1].fresh_observation_required)

    def test_missing_paused_and_review_windows_do_not_consume_capacity_after_restart(self):
        a, b, c = (self.queue(name) for name in ("a", "b", "c"))
        scheduler = self.scheduler([
            WorkerOutcome(WorkerYield.HUMAN_BLOCKED, blocker=Blocker.MFA_REQUIRED),
            WorkerOutcome(WorkerYield.READY_FOR_REVIEW),
        ], cap=2)
        scheduler.step()
        scheduler.step()
        self.assertIsNone(scheduler.step())
        stale_a = self.store.get_task(a.id).browser_session_id
        stale_b = self.store.get_task(b.id).browser_session_id
        self.store.close()
        self.store = BatchStore(self.path)
        replacement_windows = FakeWindows()  # Runtime says neither old window exists.
        recovered_worker = FakeWorker([WorkerOutcome(WorkerYield.PROGRESS)])
        restarted = ApplicationScheduler(self.store, recovered_worker,
                                         replacement_windows, max_open_applications=2)
        self.assertEqual(replacement_windows.open_count(), 0)
        self.assertEqual(restarted.step().task.id, c.id)
        self.assertEqual(replacement_windows.open_count(), 1)
        self.assertNotEqual(self.store.get_task(c.id).browser_session_id, stale_a)
        self.assertNotEqual(self.store.get_task(c.id).browser_session_id, stale_b)
        self.assertEqual(self.store.get_task(a.id).status, TaskStatus.HUMAN_PAUSED)
        self.assertEqual(self.store.get_task(b.id).status, TaskStatus.READY_FOR_REVIEW)
        restarted.release_window(a.id)  # Clear a stale persisted association safely.
        self.assertIsNone(self.store.get_task(a.id).browser_session_id)

    def test_active_task_blocks_other_run_and_reentrant_worker(self):
        a = self.queue("a")
        other_run = self.store.create_run(("board",), 2)
        listing, _ = self.store.register_listing(ListingInput(
            "board", "other", "Engineer", "https://jobs.test/other", "other"))
        b = self.store.queue_task(other_run.id, listing.id)

        class ReentrantWorker:
            def work_until_yield(inner, request):
                with self.assertRaises(RuntimeError):
                    scheduler.step()
                return WorkerOutcome(WorkerYield.PROGRESS)

        scheduler = ApplicationScheduler(self.store, ReentrantWorker(), self.windows,
                                         max_open_applications=2)
        self.assertEqual(scheduler.step(self.run.id).task.id, a.id)
        self.assertIsNone(scheduler.step(other_run.id))
        self.assertEqual(self.store.get_task(b.id).status, TaskStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
