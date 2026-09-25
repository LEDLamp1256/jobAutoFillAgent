# V2-4 bounded application controller

`ApplicationController.run(session)` navigates to one application URL, records
the observation, resolves visible questions, performs at most one typed browser
action, and interprets the fresh observation returned by `BrowserPort`. It
repeats until review or an explicit stop condition. It never creates a
submission action or human approval. The caller owns browser lifecycle.

The full semantic fingerprint detects any meaningful observed change,
including filled values and validation messages. `step_signature()` excludes
those changing details and browser refs. Advance classification uses heading,
progress, review state, structural question/control changes, and location
together: URL or fingerprint change alone does not establish progress.
Newly revealed questions stay on the current logical step and enter the
ordinary resolver path. A validation error without an actionable new field
stops as `VALIDATION_BLOCKED`; a same-step advance without explanation stops
as `NO_PROGRESS`. Neither is blindly retried.

An unresolved required question stops as `NEEDS_REVIEW`. `required=None`
means the snapshot did not establish optionality and also stops. Only an
explicit `required=False` may be skipped. Playwright MCP 0.0.82 snapshots
do not reliably establish optionality for every control, so the current
normalizer emits `None` unless it sees a required marker.
Skipped optional questions remain in the session's unresolved set. Controls
outside the current text/choice BrowserPort slice stop for review.

Default ceilings are **80 cycles**, **40 browser actions**, **10 logical step
transitions**, and **20 same-step actions**. Reaching a ceiling returns
`ACTION_LIMIT_REACHED` with the in-memory session intact. Session history
contains observations, question/resolution state, validation history, typed
action outcomes, and step transitions. Action history stores no browser refs;
historical observations can retain their original ephemeral refs for
diagnostics but are never used as action sources.

The fixture's current-employer answer is derived only when exactly one work
history entry is explicitly marked current. General employment and education
record identity remains deferred. V2-4 has no LLM, persistence, real
application submission, or human approval UI.
