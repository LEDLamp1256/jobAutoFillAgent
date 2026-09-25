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
the owner can inspect it. On a recognized sign-in page it allows manual owner
authentication followed by a fresh observation. The runner never enters
credentials, creates accounts, approves terms, or submits an application.
Without `--hold`, authentication stops as `AUTH_REQUIRED`.
Add `--config /path/to/config.json` for later stages.

Output is an in-memory diagnostic summary: hostname and sanitized path,
heading, progress text, visible question labels and control types, navigation
labels, validation count, step-signature digest, mapping provenance, action
count, and stopping reason. It excludes candidate values, URL query strings,
cookies, tokens, resume contents, and raw MCP snapshots. No diagnostic file is
written. The current normalizer covers only the tested fixture roles; an
unrecognized real snapshot must be reported as observation failure before any
generic parser correction is made.
An initially empty accessibility tree gets one read-only re-observation after
one second. An HTTP 429 access challenge is reported as `ACCESS_CHALLENGE`;
the runner does not interact with CAPTCHA or try an alternate access route.

The runner uses the existing controller limits. `deterministic` and `semantic`
disable advancement after current-step safe actions. `traverse` uses the
controller's ordinary fail-closed advance policy. Existing fixture MCP tests
remain headless and isolated. No Workday-specific behavior is implemented.
