# Batch application architecture after V2-6

This document records the next product contracts for design and review. V2-6
does not implement a batch scheduler, discovery, SwiftUI, or submission.
The existing single-application architecture remains candidate profile and
known facts → deterministic resolution → grounded local semantic mapping when
needed → `ApplicationController` / `ApplicationSession` / `AnswerLedger` →
`BrowserPort` → Playwright MCP. Candidate facts come from trusted Python
profile data. Browser references are ephemeral; every mutation is followed by
a fresh observation. Uncertainty stops or defers action. Submission is a
separate human decision.

## Intended run and ownership

A future run scans selected sources, paginates, matches unseen listings,
queues applications, opens each application in its own managed browser
window, activates Apply, and automates safe login, filling, and navigation.
Multiple windows may remain open, but initially one automation worker is
active globally and modifies only one application window at a time. A blocker
pauses only its application; other queued applications continue. CAPTCHA,
MFA, unusual verification, and
SSO remain owner actions.

Application ownership transitions from `AUTOMATION_OWNED` to `HUMAN_PAUSED`
when intervention is required. Explicit Resume triggers a fresh observation,
reclassification, and only then a return to `AUTOMATION_OWNED`. No stale
reference or failed action is replayed blindly. Each application stops at a
human review boundary before submission.

## Page-based review boundary

On a one-page form, fill every safe field and draft permitted narrative
answers before requesting a single correction and review pass. Defer
unresolved non-blocking items until that pass. On a multi-page form, apply
the same rule to each page: fill safe items, collect unresolved and review
items, advance when none remain, or pause before advancement for owner
correction. Resume re-observes the current page before continuing. Avoid
field-by-field interruption.

Future browser highlighting may mark unresolved or review-required controls
through reversible visual CSS/DOM changes. Highlighting must never change
values or submission semantics. It is recomputed after refresh or Resume;
the persistent `ApplicationReport` remains the source of truth.

Narrative drafts may use trusted candidate and job context only. When policy
permits, they may be filled, but must be labeled as AI-generated and remain
review-required before final submission. The UI will provide a dedicated
Narrative Review area for reading, editing, replacing, and approving drafts.

## Persistent report and UI

The future `ApplicationReport` records each attempted field's page or step,
visible label, semantic key, categorical provenance, action, verification
result, review requirement, and reason. Provenance categories include
`VERIFIED_PROFILE`, `DETERMINISTIC`, `AI_DRAFT_REVIEW`, `HUMAN_PROVIDED`,
`UNRESOLVED`, and `SKIPPED`. Secrets never enter the report. The report also
tracks application status, unresolved items, narratives, and review state.

SwiftUI is the future control and review plane: run configuration, session
list, status buckets, per-application report, unresolved items, narrative
drafts, Resume, review, and explicit submission approval. Selecting a session
must not foreground its browser; a separate **Bring Window to Front** action
does that. Every application requires human review/checkoff. Submission may
be clicked manually in the ATS or triggered by a one-time explicit approval
for that application in SwiftUI. There is no automatic batch submit.

## Discovery and deduplication contracts

Selectable source adapters may include LinkedIn, Handshake, Greenhouse,
Lever, Ashby, and company career sites. Each adapter yields a generic
`JobListing`; discovery stays separate from `ApplicationController`. SwiftUI
source toggles are primary. A later power-user command such as
`/run linkedin handshake` may configure the same backend.

Persistent exact identity preference is: source plus source listing ID,
canonicalized listing URL, canonicalized final application URL, then a stable
fingerprint of normalized posting metadata. Exact duplicates reuse or skip
existing records. Probable duplicates require review. Previously submitted
jobs never silently become fresh applications in later runs.

## Planned sequence

1. **V2-7:** Persistent `JobRun`, `JobListing`, `ApplicationTask`, and
   `ApplicationReport`; ownership, Resume, and deduplication identity.
2. **V2-8:** Scheduler and one-window-per-application lifecycle.
3. **V2-9:** SwiftUI control and review plane.
4. **V2-10:** ApplicationLauncher and safe login integration.
5. **V2-11:** Autonomous page-based fill and advance, resumable blockers,
   and narrative review behavior.
6. **V2-12:** Human report/review workflow and reversible browser issue
   highlighting.
7. **V2-13:** Source discovery and pagination adapters, source toggles,
   matching, and deduplication.
8. **V2-14:** Full batch orchestration.

Each stage needs its own implementation contract and acceptance review.
