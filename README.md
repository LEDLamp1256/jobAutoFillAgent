# Job Application Agent

Job Application Agent is a local macOS application for managing job application
tasks with explicit human control. Its native SwiftUI interface talks over
newline-delimited JSON (NDJSON) to a Python child process. Python owns the
persistent SQLite domain, scheduler, safety decisions, and Playwright MCP
browser sessions. Several managed browser windows can remain available while
at most one eligible task is actively automated.

**Final job submission remains human-only.** The v2 control plane has no
Submit command or autonomous final Submit action.

## Current status

The accepted v2 implementation has persistent runs, listings, application
tasks, reports, and ownership states. The backend can launch a managed Chrome
window for an eligible task's trusted application URL, observe the page, and
attempt an ordinary username/password login when an exact-host account is
configured. CAPTCHA, MFA, SSO, unusual verification, missing credentials, and
ambiguous page states yield to human intervention. A persisted `RUNNING` run is
required for autonomous scheduler ticks; opening the app does not create one.

The SwiftUI app displays runs, applications, attention items, reports, and
narrative entries. It supports Refresh, Reconnect Backend, explicit Resume,
Bring Window to Front, report review, narrative review/replacement, final
review checkoff, and owner-recorded submission. The V2-11 Python worker fills safe text and choice fields on
the current application page using deterministic candidate facts. Freshly
verified fields are marked green in Chrome; unresolved fields are marked red
where the current browser observation safely identifies them. The colors show
policy and review state, not a separate numeric confidence threshold. Pending
questions appear in the durable report. A complete one-page form can reach
`READY_FOR_REVIEW`; Next/Continue and final Submit remain human browser actions.
Job discovery, LinkedIn/Handshake scraping, and general autonomous multi-page
application navigation are not part of the current v2 app.

Older standalone scripts (`main.py`, `navigator.py`, `jobScraper.py`,
`injection.py`, and related files) remain in this repository. They are a
separate legacy workflow; their commands and behavior do not describe the
native v2 app.

## Requirements

- macOS 14 or newer and a Swift 5.10-capable Xcode/Swift toolchain.
- Python 3 with `venv` and `pip` available.
- Node.js and npm available to the install script.
- Google Chrome for the managed headed browser runtime.
- Network access for first-time dependency installation, if dependencies are
  not already cached.

This is a local development installation for this Mac. The `.app` is not a
self-contained distributable binary, App Store build, or notarized release.
Python, Node/npm, and Chrome remain local prerequisites.

## Quick setup

From a checkout of this repository on the owner's Mac:

```sh
git clone https://github.com/LEDLamp1256/jobAutoFillAgent.git
cd jobAutoFillAgent
git switch dev
Scripts/bootstrap-macos-runtime.sh
Scripts/install-macos-app.sh
```

The installer also runs the bootstrap script, so the explicit bootstrap line
may be omitted after the runtime has already been prepared. First-time
bootstrap installs the repository's Python requirements and pinned Playwright
MCP CLI into stable per-user support storage. It does not run on each app
launch. For a checkout that already exists, start at the bootstrap line.

Open Finder, choose **Go → Go to Folder**, enter
`~/Applications/Job Application Agent.app`, and open the app. Normal launch
does not require `swift run` or manually exported `JOBAGENT_*` variables.

## Install and update the macOS app

`Scripts/install-macos-app.sh` builds the existing SwiftPM frontend and
assembles a real macOS `APPL` bundle with the Python backend, a provisional
icon, and stable bundle identifier
`com.ledlamp1256.jobapplicationagent`. The canonical installed path is always
`~/Applications/Job Application Agent.app`. The app participates in Finder,
the Dock, and Command-Tab as a GUI application.

There is **one canonical installed app**. To update after source changes, quit
Job Application Agent and run `Scripts/install-macos-app.sh` again from the
checkout. The installer builds and validates a complete replacement before
swapping it into the same path. It refuses to replace a running
`JobAgentControl` process. It does not create version-numbered, date-stamped,
or branch-specific installed copies. Application data remains outside the
bundle and survives replacement.

## Persistent state and local configuration

The installed app uses these paths:

| Path under `~/Library/Application Support/Job Application Agent/` | Purpose |
| --- | --- |
| `applications.sqlite3` | Persistent runs, tasks, reports, and review state |
| `config.json` | Optional private local candidate/login configuration |
| `runtime/python/` | Reusable Python environment |
| `runtime/bin/node` | Link to the installed Node executable |
| `runtime/playwright-mcp/node_modules/@playwright/mcp/cli.js` | Pinned MCP browser CLI |

The installed app's Python backend is bundled under
`Job Application Agent.app/Contents/Resources/Backend`; its Swift executable
is under `Contents/MacOS`. The database and config are **not** inside the app.
The installer does not copy a repository `config.json`, migrate an existing
database from another path, or overwrite personal data. Build products are
kept under `~/Library/Caches/Job Application Agent/`.

[`config.EXAMPLE.json`](config.EXAMPLE.json) shows the candidate profile
format. Keep the live file at the Application Support `config.json` path above
for the installed app. If it contains login credentials, add a top-level
`login_accounts` object keyed by the **exact observed HTTPS login hostname**;
each entry needs `username` and `password`. This example uses only placeholders:

```json
{
  "login_accounts": {
    "login.example.test": {
      "username": "your-account-name",
      "password": "your-local-secret"
    }
  }
}
```

Merge that object into the existing profile JSON rather than replacing the
profile with the small example. Hostnames such as `example.com`,
`www.example.com`, and `auth.example.com` are distinct; the app does not guess
an account across redirects. The existing repository-root `config.json` name
is Git-ignored for development. **Never commit passwords or a live config.**
An Application Support file is outside the repository and must remain private.
Missing configuration is safe: ordinary login will pause for human handling.

## Using the control app

- Review the runs, application list, and attention-required view. Select an
  application to read its details, report, and narrative entries. Selection
  is passive and does not raise Chrome.
- **Refresh** re-queries the connected backend. Periodic polling and reads do
  not issue browser foreground commands.
- **Bring Window to Front** explicitly raises the selected task's live managed
  window. If its runtime association is missing or stale, the app reports the
  window as unavailable rather than relaunching it from this command.
- **Resume** is explicit owner authorization for a human-paused task. Python
  clears stale window assumptions and requires fresh observation on subsequent
  automation. It does not replay a previous browser action.
- Use report review and narrative review/replacement to record human decisions.
  Final-review checkoff records review; it does **not** submit the application.
- The review labels progress from **Needs Review** through **Ready for Final
  Review** and **Ready to Submit**. After manually submitting on the employer
  site, use **Record as Submitted** and confirm to record that fact locally.
  This action does not click the site's Submit button. The managed browser
  remains available if its runtime is still alive.
- **Reconnect Backend** restarts the local Python child and reloads durable
  state. It does not replay owner commands. Merely reconnecting does not create
  a new `RUNNING` batch.

The current UI is a control and review surface, not a job discovery or batch
creation interface. Existing eligible `RUNNING` runs can progress under the
Python scheduler; the app does not manufacture one on startup.

## Submission safety

There is no autonomous final Submit, no Submit button in the SwiftUI app, and
no Submit command in the trusted local control plane. The human owner performs
final submission in the browser. Browser content and local semantic model
output cannot create trusted owner authorization. Final-review checkoff is a
separate record, not a submission action.

## Browser and login safety

The login worker validates the current observed URL as HTTPS and matches its
hostname exactly to a configured account before entering the username. After
fresh observation, it repeats destination and account-continuity checks before
reading or sending the password. Credentials remain in the local config;
passwords are not stored in SQLite or application reports and are not sent to
the semantic LLM. CAPTCHA, MFA, SSO, and unusual or ambiguous verification
pause for human intervention. Raw Playwright MCP artifacts use owned temporary
directories outside the repository and app bundle. See
[`docs/v2-application-runtime.md`](docs/v2-application-runtime.md) for runtime
and recovery details.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| App does not open | Confirm macOS 14+, a completed install, and `~/Applications/Job Application Agent.app`. Re-run the installer from the checkout after quitting the app. |
| Installer reports a running app | Quit the installed app and any development `swift run JobAgentControl` copy, then retry. |
| Python runtime is missing | Run `Scripts/bootstrap-macos-runtime.sh`, then reinstall. Check that Python 3 can create a `venv`. |
| Node or MCP runtime is missing | Confirm `node` and `npm` are available, then run the bootstrap script and reinstall. If Node moved, bootstrap refreshes its support link. |
| Chrome cannot be opened | Confirm Google Chrome is installed. The app uses headed Chrome through Playwright MCP. |
| Backend shows disconnected or database error | Use **Reconnect Backend** once. Check the app's error message and permissions for its Application Support directory. Re-run bootstrap/install if a runtime component is missing. |
| Existing data seems absent | The installed app uses the Application Support database above. It does not automatically import a development or test database from another path. |

Do not delete the database or config as a first troubleshooting step.

## Developer workflow and architecture

Local `dev` is the accepted integration branch; start isolated feature work
from `dev`. `main` is not the ordinary v2 development branch. Development
`swift run` still accepts `JOBAGENT_BACKEND_ROOT`, `JOBAGENT_DB_PATH`, and
`JOBAGENT_PYTHON`; see
[`docs/v2-swiftui-control-plane.md`](docs/v2-swiftui-control-plane.md).
From the checkout, the routine checks are:

```sh
swift build
swift test
python3 -m unittest discover -s tests -p 'test_*.py' -q
macos/Tests/test_install_macos_app.sh
git diff --check
```

The installer itself performs a release Swift build. The packaging test uses
a disposable home directory and checks first install, replacement, and a
running-app refusal. Python tests need the repository's Python requirements;
Swift's Python child smoke tests may need `JOBAGENT_TEST_PYTHON` pointed at
that prepared environment. Temporary test paths are not app runtime paths.

```text
SwiftUI macOS app
  → NDJSON Python child / LocalControlPlane
  → SQLite domain and ApplicationScheduler
  → ApplicationLauncher / managed runtime
  → Playwright MCP → headed Chrome
```

The answer-resolution components prefer deterministic, trusted candidate
facts. A bounded local semantic fallback may map a question to a permitted
semantic key, but cannot invent candidate values or receive passwords.
Browser references are ephemeral, and browser changes require fresh
observation. The V2-11 worker adds bounded current-page filling and review
annotation after the accepted launcher and login boundary; automated multi-page
navigation remains future work. For deeper contracts,
see [`docs/v2-foundation.md`](docs/v2-foundation.md),
[`docs/v2-resolution.md`](docs/v2-resolution.md),
[`docs/v2-semantic-llm.md`](docs/v2-semantic-llm.md),
[`docs/v2-control-plane.md`](docs/v2-control-plane.md), and
[`docs/v2-macos-app.md`](docs/v2-macos-app.md).

## Uninstall or intentionally reset data

**Remove the app only:** quit it, then remove
`~/Applications/Job Application Agent.app`. The SQLite database, config, and
support runtime remain available for a later reinstall.

**Remove application data:** after separately backing up what you need,
remove `~/Library/Application Support/Job Application Agent/`. This destroys
the database, private config, and prepared runtime. It is a separate,
intentional action; the installer and app uninstall do not do it.

## Current limits

This stage supplies a stable local app, not public distribution, notarization,
automatic updates, or a self-contained Python/Node/Chrome package. It does not
add job-source discovery, general autonomous multi-page application traversal,
or final submission automation.
