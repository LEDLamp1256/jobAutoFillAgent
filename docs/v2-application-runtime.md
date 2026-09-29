# V2-11 managed application runtime

The local stdio backend uses the existing `ApplicationScheduler` and
`ApplicationWindowPort`. Set `JOB_AGENT_PLAYWRIGHT_MCP_CLI` to an installed
Playwright MCP CLI before starting the SwiftUI app or Python backend. The CLI
can also be supplied with `--mcp-cli`. Without it, the backend remains in its
safe unavailable-window mode. Each task gets one isolated headed MCP browser
session. The backend processes one bounded launch/login and current-page pass
at a time. At an application page, the existing controller resolves safe text
and choice fields deterministically, performs one mutation, obtains a fresh
observation, and verifies the result before continuing. It never activates
Next/Continue or final Submit. A one-page terminal form with no unresolved
work reaches `READY_FOR_REVIEW`; an unresolved or multi-page boundary yields
`HUMAN_PAUSED` with the managed browser available.
Only a persisted `RUNNING` run authorizes an idle automation tick. A newly
created run stays `CREATED`; starting or reconnecting the backend and serving
NDJSON requests do not start it. V2-10 adds no UI command to start a run.

The optional `--config` path defaults to the existing gitignored `config.json`.
Ordinary login accounts are read from its `login_accounts` object, keyed by the
exact login-page hostname. Each account needs a `username` and `password`.
Keep this file local and gitignored. Missing or ambiguous credentials pause the
task for human intervention. CAPTCHA, MFA, SSO, and unusual verification also
pause the task. No credential value enters SQLite, the report, or the stdio
protocol. Each credential action checks the current observed HTTPS hostname.
The second check happens after username entry and before reading the password.
Account IDs are exact hostnames, so cross-host account continuity is not
supported. Raw MCP server stderr goes to a nonpersistent null sink.

Each verified current-page field receives a nonsecret report entry with its
label, semantic key, source, action, and verification state. Pending fields
receive a reason and are reused on an unchanged Resume pass. The final fresh
observation supplies live refs for presentation-only green verified and red
needs-review markers. A manual correction is reassessed on Resume and loses
its red marker when complete; it is not recorded as an automated green fill.
Colors follow the existing answer safety policy, not an arbitrary confidence
threshold. Existing profile/Q&A resolution is the production baseline;
narrative writing, sensitive fields, toggles, and file upload remain for human
review. The pinned MCP upload tool is chooser based and this stage has no
targeted, freshly verifiable upload action. No additional model service is
required for the app.

The backend stores only the opaque window association already supported by
SQLite. A restarted backend owns no prior live browser objects, so it validates
windows against its current MCP sessions and uses scheduler recovery for stale
associations. Listing and selecting applications only read state. The existing
`bring_window_to_front` command is the explicit foreground action. The pinned
MCP tab list exposes mutable indices, not stable tab IDs. The runtime therefore
requires exactly one tab and validates a process-local page marker before tab
selection. A missing marker or extra tab makes the association unavailable.
The marker is stored in the page's session storage and can be lost on a
cross-origin navigation; this fails closed. Passive views use process-local
availability knowledge because MCP tab listing can create a blank tab when no
tab remains. Bring and automation perform live validation. MCP tab selection
does not prove native macOS window foregrounding; that remains an owner-assisted
visual acceptance check.

The pinned MCP writes implicit `page-*.yml` snapshots and other automatically
named browser output under `.playwright-mcp` in its working directory unless
configured otherwise. Each managed session now owns a separate OS-temporary
directory used as both MCP working directory and explicit `--output-dir`.
Closing the session removes that directory, including transient snapshots,
traces, and screenshots. Graceful backend exit attempts every session cleanup;
forced process termination may leave temporary files for OS cleanup. No raw
browser artifact path is persisted in SQLite or application reports.
