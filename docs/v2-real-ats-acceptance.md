# V2-6 headed ATS acceptance

This is an owner-gated diagnostic runner, not a job search or submission tool.
It launches pinned Playwright MCP through Python stdio with visible, isolated
Chrome. It never uses the owner's normal Chrome profile, saves no browser
state, and closes the browser when the command finishes. The v2 BrowserPort
has no submission method; the controller stops at final review.

Supply an owner-selected application URL. Observation alone needs no candidate
profile. Supply `--config` explicitly for filling or traversal; it is parsed
before opening the browser. Do not place a real URL or
private config in tracked tests or documentation. Set the gate for every run:

```sh
export JOB_AGENT_RUN_REAL_ATS_ACCEPTANCE=1
export JOB_AGENT_ACCEPTANCE_URL='https://YOUR-CHOSEN-APPLICATION'
export JOB_AGENT_PLAYWRIGHT_MCP_CLI=/path/to/@playwright/mcp/cli.js
python -m jobagent.acceptance --stage observe --hold
```

Run stages separately, inspecting each result before increasing scope:

| Stage | Behavior |
| --- | --- |
| `observe` | Navigate and summarize one observation; no form action. |
| `deterministic` | Fill safe known values on the current step; no advance and no model. |
| `semantic` | Same step, with the accepted local `llama3.1:8b` semantic-key fallback; no advance. |
| `traverse` | Bounded controller may use uniquely identified nonterminal navigation; stops at review or uncertainty. |

`--hold` requires a terminal. It pauses before closing the visible browser so
the owner can inspect it. Access challenges and recognized authentication
states request explicit owner input, then a fresh observation. The CLI does
not configure a real credential provider yet; login remains manual there.
The runner never creates accounts, approves terms, or submits an application.
Without `--hold`, intervention states stop as `HUMAN_INTERVENTION_REQUIRED`.
Add `--config /path/to/config.json` for later stages.

Output is an in-memory diagnostic summary: hostname and sanitized path,
heading, progress text, visible question labels and control types, navigation
labels, validation count, step-signature digest, mapping provenance, action
count, and stopping reason. It excludes candidate values, URL query strings,
cookies, tokens, resume contents, and raw MCP snapshots. No diagnostic file is
written. The normalizer covers fixture roles and the real ATS structures
verified below. An unrecognized real snapshot must be reported as observation
failure before any generic parser correction is made.
On normalization failure, the runner prints a bounded structural diagnostic:
host/path without query, role counts, a limited set of accessible control names,
state flags, and value-free heading syntax. It does not print textbox contents
or save the raw snapshot. Names can still reflect text supplied by a site, so
share this diagnostic only after reviewing it. Missing headings still fail
closed. Website-provided resume parsing or autofill is never activated.
After an owner resume, a successful normalized observation also gets a bounded
summary before an unchanged intervention state returns. It records the fresh
page's sanitized heading, progress, limited control labels and counts, login
signals, and old/new classifications. It never includes field values or raw
MCP content.
If that fresh observation classifies as unknown with no recognized questions or
navigation, the runner also prints bounded control-line evidence: role, masked
reference shape, metadata flags, line-matcher result, and generic rejection
reason. This helps distinguish reference syntax from other parser mismatches
without recording control values or raw reference IDs.
An initially empty accessibility tree gets one read-only re-observation after
one second. An HTTP 429 or visible CAPTCHA challenge is reported as an access
challenge; the runner never clicks it or tries an alternate access route.
See [v2-auth-handoff.md](v2-auth-handoff.md) for pause/resume and login bounds.

The runner uses the existing controller limits. `deterministic` and `semantic`
disable advancement after current-step safe actions. `traverse` uses the
controller's ordinary fail-closed advance policy. Existing fixture MCP tests
remain headless and isolated. No Workday-specific behavior is implemented.

## Accepted real-site milestones

An owner-opened Workday application form normalized with eight recognized
textbox questions and classified as `APPLICATION`. Heading, textbox, button,
and radio references are treated as bounded opaque tokens where real evidence
justified that parser change. A later bounded acceptance used the production
resolver, controller, BrowserPort, and MCP adapter to fill exactly one City
textbox from the trusted profile. It obtained a fresh observation, verified
the field value without printing it, remained on the application form, and
made no navigation, LLM, upload, website resume-autofill, or Submit action.

These milestones do not establish whole-page coverage. The real form still
has unsupported `Save and Continue`, some Yes/No radio and group syntax,
the `How Did You Hear About Us?` control variant, and checkbox/listbox roles.
Do not broaden their parsers without a separate evidence-backed task. The
private candidate config also needs explicit first and last names for those
questions; see [v2-profile-readiness.md](v2-profile-readiness.md).
