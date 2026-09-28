# V2-8 application scheduler

`ApplicationScheduler` performs one bounded `ApplicationWorkerPort.work_until_yield`
call per `step()`. It selects persisted active automation first, then queued tasks
with automation ownership. Queue order is `(updated_at, id)`: new tasks enter in
creation order and a human Resume moves its task to the back of the waiting
queue. A progress yield leaves the task active for its next bounded call. A
blocker transfers it to `HUMAN_PAUSED`; final page or application review moves
it to `READY_FOR_REVIEW`. Later calls can select other eligible tasks. The
scheduler contains no form, discovery, or ATS logic.

The worker receives a task and an opaque managed window ID. Every invocation
requires a fresh observation before browser action, including after Resume.
`resumed_by_human` explicitly tells an eventual worker adapter that the old
execution context was cleared. A yield can carry multiple review issues for a
page; the scheduler persists those as pending report entries. It never asks the
worker to submit an application. Submission remains the separate human-gated
`BatchStore.mark_submitted_by_human` operation.

`ApplicationWindowPort` associates at most one live managed window with a task.
Several windows may remain open while only one synchronous worker call runs.
The scheduler does not allocate past `max_open_applications` and keeps paused
and review-ready windows open. Releasing such a window is an explicit operation.
Failure releases its window. A resumed task can reuse a still-live window, but
the stored ID is cleared by Resume and the worker must observe again. If an
active task's stored ID is stale after restart, the scheduler obtains a new
window and updates the ID. Window objects and browser element references are
never persisted.

V2-8 assumes one backend scheduler owner for each database. Its synchronous
guard prevents reentrant worker calls and its persisted-state check rejects
multiple active tasks, but it provides no cross-process coordination. The
initial SwiftUI control plane should use one backend scheduler owner.

Reading `tasks()` or `attention()` has no foreground side effect. Only
`bring_window_to_front(task_id)` calls the window port. `attention()` returns
paused and review-ready tasks with their blocker and report entries. SQLite
remains the scheduler's source of truth across reconstruction; there is no
in-memory task queue. A second active persisted task or a reentrant worker
call is rejected because this stage permits one active automation worker.
