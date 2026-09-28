# V2-9A trusted local control plane

The future SwiftUI app starts one Python backend child process per local
database. V2-9A uses newline-delimited JSON on stdin/stdout: one request line
produces one response line. EOF ends the service; the next child process opens
the same SQLite database and reconstructs application state. Stderr remains
separate for startup diagnostics. Protocol handling writes no logs to stdout.
SwiftUI can use `Process` and pipes with `Codable`. This avoids a listening
port and LAN exposure. The tradeoff is that the native app must own the child
process and reconnect after a crash. A loopback JSON service would simplify
`URLSession` integration, but adds port binding and startup management without
a current need. No push stream is implemented; the UI polls queries.

Start the backend with `python -m jobagent.control_plane_stdio --db PATH`.
Each request is `{"id":"1","method":"list_runs","params":{}}`. A response
is `{"id":"1","ok":true,"result":...}` or
`{"id":"1","ok":false,"error":{"code":"BAD_REQUEST","message":"..."}}`.
Request IDs are strings of 1–128 characters. Method names and parameter keys
are fixed; unknown keys and methods are rejected. Fields use JSON strings,
booleans, integers, arrays, objects, and explicit nulls. Stored timestamps are
UTC ISO-8601 strings. Errors use `NOT_FOUND`, `INVALID_TRANSITION`,
`NOT_RESUMABLE`, `WINDOW_UNAVAILABLE`, `BAD_REQUEST`, or `INTERNAL_ERROR`.
Normal error responses contain no traceback or raw exception text.

Queries: `list_runs`, `get_run`, `list_applications`, `get_application`,
`list_attention_required`, `get_application_report`, and
`get_narrative_entries`. Application views include listing company/title,
status, ownership, step, blocker/failure codes, pending review counts,
window association and live availability flags, and available human actions.
Window IDs themselves are not sent. Reports include existing provenance,
verification, review state, and narrative draft/revision text. Field factual
answer values are not stored or sent.

Commands: `resume_application`, `bring_window_to_front`,
`review_report_entry`, `mark_final_review_checked`, and
`replace_narrative`. The control plane creates the exact `LOCAL_OWNER`
`HumanAuthorization` for each human command. Caller-supplied actor/action
fields are rejected. The native parent must issue these commands only for
explicit owner UI actions; browser content, model output, and job data must
never be routed into them. This is a local single-user trust boundary, without
accounts or cryptographic identity. Reading or selecting a task never
foregrounds a window. There is no generic Submit command or website Submit
execution. A narrative replacement adds a human-provided revision and marks
its pending source draft reviewed; the report retains both entries.

The standalone V2-9A process intentionally has no real worker or window
runtime. It reports persisted window association separately from live window
availability, and foreground requests return `WINDOW_UNAVAILABLE` until a
managed window port is integrated. SQLite remains authoritative for runs,
tasks, reports, ownership, review, and human-action records. Transport process
objects, browser refs, snapshots, cookies, credentials, auth tokens, and
candidate factual field values do not cross this bridge.
