# V2-7 persistent batch domain

This implements the data and state foundation described in
[the batch roadmap](v2-batch-roadmap.md). It does not run discovery, a scheduler,
browser windows, SwiftUI, or submission.

`BatchStore` uses local `sqlite3`. `PRAGMA user_version = 1` identifies the
initial schema; unknown versions fail on open. Runtime databases belong outside
source control (`*.sqlite3`, `*.db`, and journal files are ignored). Tests use
temporary databases. `JobRun` holds a requested source set, limit, counters,
and lifecycle. `JobListing` holds normalized identity and seen timestamps.
`ApplicationTask` belongs to one run and listing. A task can hold an opaque,
store-generated managed window identity. `ApplicationReport` is the task's
ordered report entries, including page or step, action code, provenance,
verification, and review state.

Task status and ownership are separate. The local single-user principal is
`LOCAL_OWNER`. Human-gated methods require an immutable `HumanAuthorization`
with that actor and the specific action type. Raw strings and authorizations
for a different action are rejected. Only the trusted local control plane
should construct these values; browser content, the scheduler, and model output
are not authorization sources. Each accepted human action writes an audit
record with actor kind, action type, target kind/ID, and timestamp. There are
no accounts, credentials, or cryptographic tokens in this contract.

Active automation may pause for a
typed blocker; that transfers ownership to the human and preserves report
entries. `resume_by_human` requires a `RESUME` authorization,
returns the task to a queue, clears its old managed window identity, and records
the request time. A future scheduler must establish a fresh window observation
before taking browser action. No observation IDs, snapshots, DOM selectors,
BrowserPort target refs, process IDs, credentials, or cookies enter this schema.

Final review is a separate human-owned task state. Submission state can only
be reached through `mark_submitted_by_human` after `mark_review_checked`, with
distinct `FINAL_REVIEW` and `RECORD_SUBMISSION` authorizations and no pending
report reviews. The SQLite
constraint also requires human ownership and checkoff/submission metadata for
a submitted row. This records an externally authorized completion; it does not
click Submit. The existing single-application `ActionPolicy` remains the
browser submission boundary.

Report provenance is categorical: verified profile, deterministic, AI draft
review, human provided, unresolved, or skipped. AI draft entries are narrative
entries and begin pending review. Field answers are not copied into SQLite.
Narrative text is the sole answer content retained so a future Narrative Review
surface can display and revise drafts; callers must only pass draft text and
never credentials or authentication data. A later human edit can be a new
human-provided narrative entry. Action, reason, and failure values are codes,
not raw browser errors.

Exact identity lookup tries source plus listing ID, canonical listing URL,
then canonical application URL. When none exist, it can use a complete
company/title/location fingerprint. URL normalization removes fragments,
known tracking parameters, default ports, and trailing slashes while retaining
other query parameters such as requisition IDs. Credential-like query keys are
rejected. Stored URLs use this canonical form, so fragments and tracking data
are not retained. Conflicting strong identities are treated as possible
duplicates. Same company and title without an exact identity is a possible
duplicate: it gets a separate listing and cannot be queued until explicitly
resolved by a human. Exact matches reuse the listing. Queuing any listing with
a prior human-submitted task is rejected across runs.

Future scheduler, source adapters, and SwiftUI should call this API rather than
mutate task statuses directly. They will own window management, fresh browser
observations, narrative generation, page advancement, and human authorization
identity verification.
