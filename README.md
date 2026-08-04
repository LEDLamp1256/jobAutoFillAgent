# Job Application Autofill Agent

A local, API-free pipeline that scrapes a job application form, maps your
own `config.json` profile onto it using a local Ollama model, fills the
form with Playwright, and requires your explicit review before anything
is ever submitted. Nothing runs in the cloud, nothing leaves your
machine except the browser traffic to the job site itself.

## How it works

```
scrape → match → inject → review
```

| Stage | File | What it does |
|---|---|---|
| 1. Extraction | `jobScraper.py` | Scrapes an already-navigated Playwright page (main frame + iframes): text/select/checkbox/radio/file/date fields, grouping radios into logical `radio_group` fields and custom JS dropdowns separately. |
| 2. Matching | `aiMatcher.py` | Sends scraped fields + your `config.json` to a local Ollama model, which maps each field to a value with a confidence score (0–100) and a short justification. |
| 3. Injection | `injection.py` | Fills the page via Playwright, using resilient wait/retry logic, and outlines every touched field **green** (≥90% confidence) or **red** (needs review). |
| 4. Review Gate | `review.py` | Pauses the browser (`page.pause()`), lets you inspect/edit the filled form, and only submits after your explicit confirmation — either typing `Y` at the CLI or clicking Submit yourself. |

`main.py` orchestrates all four stages against a single job posting.
`navigator.py` extends this into a multi-job agent that searches a job
board, filters listings by your criteria, and runs the same
scrape→match→inject→review pipeline against each match — **the review
gate is never skipped, no matter how many applications you run.**

## Project files

```
main.py               Orchestrator for a single job application URL
navigator.py          Agentic multi-job state machine (SEARCH → NAVIGATION → INTERACTION → ERROR)
jobScraper.py         Playwright DOM scraping
aiMatcher.py          local LLM field matching (Ollama)
injection.py          Playwright fill + confidence-based styling
review.py             human review, verified submission, CSV audit log
login.py              one-time manual login capture for auth-gated boards (e.g. Handshake)
test.py               Integration test against a local dummy form + your real config
config.json           Your private candidate profile (never commit this — see below)
config_EXAMPLE.json   Template showing the expected config.json structure
requirements.txt
```

Generated at runtime, not tracked in source control:
- `logs/run_*.log` — one file per pipeline run
- `applications_log.csv` — one row per submission attempt (`timestamp_utc, url, status, notes`)
- `navigator_state.json` — resumable queue state for `navigator.py`
- `auth_state.json` — saved login session from `login.py`, if you use it

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# Install Ollama separately: https://ollama.com
ollama pull llama3.1
ollama serve                    # leave running in its own terminal
```

## Configuring `config.json`

This is your private candidate profile — structured JSON describing your
personal info, work history, education, skills, projects, a `qa_bank` of
common application-question answers, and a `documents` section with your
resume path. See `config_EXAMPLE.json` for a full template:

```json
{
  "personal_info": { "full_name": "...", "email": "...", "linkedin_url": "...", "..." },
  "work_history": [ { "job_title": "...", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "..." } ],
  "education": [ { "degree": "...", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "..." } ],
  "skills": { "languages": [...], "frameworks_libraries": [...] },
  "projects": [ { "name": "...", "bullet_points": [...] } ],
  "qa_bank": { "will_require_visa_sponsorship": { "answer": "No" }, "...": {} },
  "documents": { "resume_path": "/absolute/path/to/resume.pdf" }
}
```

`aiMatcher.py` reads `documents.resume_path` automatically for any
file-upload field it identifies as a resume/CV field — you don't need to
duplicate this path anywhere else. **Never commit `config.json`** — it's
already in `.gitignore`; it contains your personal data.

## Usage

**Single application:**
```bash
python main.py --url "https://boards.greenhouse.io/example/jobs/12345" \
  --config config.json --model llama3.1
```

**Integration test** (dummy local form + your real config, to sanity-check
the pipeline before pointing it at a live site):
```bash
python test.py --config config.json
```

**Multi-job agent:**
```bash
python navigator.py \
  --search-url "https://boards.greenhouse.io/example" \
  --criteria "Backend engineering internships, remote or Bay Area" \
  --config config.json --model llama3.1 \
  --max-applications 5
```

**Authenticated job boards (e.g. Handshake):** some boards require a
login before search results render at all. Capture a session once with
`login.py`, then pass it to `navigator.py` via `--auth-state`:
```bash
python login.py --url "https://app.joinhandshake.com/login" --output auth_state.json
python navigator.py \
  --search-url "https://app.joinhandshake.com/stu/postings" \
  --config config.json --model llama3.1 \
  --auth-state auth_state.json
```
Sessions eventually expire — if the navigator starts returning zero
listings again after previously working, re-run `login.py` to refresh it.

Always run headed (default) — headless disables the review gate's
`page.pause()`, which will just hang waiting for a resume that can't
happen.

## Safety design — read this before pointing it at a real account

- **Every submission goes through a human review gate.** `review.py`
  pauses the browser before any Submit click, validates which fields are
  still unresolved, and requires either your typed `Y` confirmation or
  your own manual click — plus a blocking post-click verification step
  that confirms the ATS actually accepted the submission before the
  browser is allowed to close. This is true for `main.py` and for every
  application `navigator.py` processes; it is never automated away.
- **No bot-detection evasion.** There's no human-mimicry delay logic and
  no context-rotation framed around evading tracking. `navigator.py`
  uses fresh browser contexts per application (session hygiene) and
  modest fixed delays (not hammering the target site), but this does
  **not** make automated use of a platform compliant with its Terms of
  Service. LinkedIn's and Indeed's ToS both prohibit automated scraping
  and application bots — read the target site's terms before running
  this against a real account. Greenhouse/Lever/Ashby/Workday-hosted
  career pages generally don't have this restriction.
- **Hard block on "Autofill with Resume" buttons.** Some ATS platforms
  offer their own resume-parsing autofill that would overwrite every
  field this pipeline carefully mapped from your config. `injection.py`
  refuses to click/fill anything matching that pattern, in code — not
  just via a prompt instruction, since LLM instruction-following on
  subtle distinctions isn't reliable enough to trust alone for this.

## Debugging a bad match

`aiMatcher.py` prints several `[DEBUG]` lines on every run:
- The exact `field_id`/`label`/`type` for every scraped field
- The exact `candidate_profile` JSON sent to the model
- The raw LLM response before parsing
- An expected-vs-received `field_id` diff (the decisive signal for a
  total flatline — tells you whether the model saw your data and chose
  not to use it, or never saw it correctly at all)

If confidence is high but injection times out, `injection.py` logs the
element's `outerHTML` at failure time — check for `disabled`, zero size,
or an overlay intercepting clicks.

## Known limitations

- Custom dropdown/date-picker interaction (`injection.py`) is
  heuristic — markup varies across ATS platforms, and the calendar-click
  fallback doesn't navigate months/years, only click a matching day cell.
- The generic job-card scraper in `navigator.py` is intentionally not
  tuned to any specific job board; expect to adjust its selectors for
  whatever board you point it at.
- Local model quality matters a lot. `llama3.1` is the current default;
  if you see field-mapping regressions (wrong keys, ignored instructions,
  hallucinated field types), that's often a model-capability ceiling
  rather than a prompt-wording problem — check the `[DEBUG]` raw response
  before assuming the code is at fault.