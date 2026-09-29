# V2-9B macOS control and review frontend

For the later local `.app` build and canonical Finder install, see
`docs/v2-macos-app.md`. The `swift run` instructions below remain a development
route.

`Package.swift` defines a macOS 14 SwiftUI executable, a small reusable core,
and XCTest tests. It uses Foundation, Combine, and SwiftUI only. The local
`.app` assembly is documented separately. The repository has no Xcode project.

The native app owns one Python child process. `ControlPlaneProcess` starts
`python -m jobagent.control_plane_stdio --db PATH` with the repository as its
working directory, writes one JSON request line, reads one JSON response line,
and checks the request ID. The actor serializes requests. Stderr stays separate
from protocol stdout. A closed pipe or exited child changes the connection to
Disconnected; the UI stays open and offers an explicit Reconnect button.
Reconnect starts a new child and reloads state from SQLite. It does not retry
forever. The Python store remains authoritative.

For local development, launch from the repository root with these environment
variables set to your own paths:

```sh
export JOBAGENT_BACKEND_ROOT="$PWD"
export JOBAGENT_DB_PATH="/private/tmp/jobagent-control-dev.sqlite3"
export JOBAGENT_PYTHON="$(command -v python3)"
swift run JobAgentControl
```

`JOBAGENT_BACKEND_ROOT` and `JOBAGENT_DB_PATH` are required. The Python
executable defaults to `/usr/bin/python3`; use `JOBAGENT_PYTHON` to choose a
project environment. Use a test database for development, not an owner's real
application database. No personal path is compiled into the app.

`AppStore` is the single observable state owner. It loads runs, applications,
attention, the selected task, report, and narratives through typed `Codable`
models. The UI offers manual Refresh and polls every 20 seconds while active.
Refresh only re-queries a connected backend. Reconnect Backend is enabled when
the backend is disconnected or in error; it explicitly starts a new child and
reloads SQLite state. Both toolbar controls have visible labels and tooltips.
Report refresh does not send commands and does not save a narrative editor's
local draft. Selecting a task only queries details. The separate Bring Window
to Front button sends the foreground command; an unavailable managed window
produces the backend's `WINDOW_UNAVAILABLE` error in the UI.

Resume, report-item review, narrative approval/replacement, final-review
checkoff, and Record as Submitted are sent only from explicit buttons. A narrative replacement is sent
only on Save Replacement; typing stays local. The backend decides whether each
transition is legal and returns structured errors. Swift cannot encode actor
or authorization action fields in its fixed request enum. The UI has no browser
Submit control. Python derives Needs Review, Ready for Final Review, Ready to
Submit, and Submitted from durable status, pending report count, and final
review checkoff. Record as Submitted appears only when Python says it is
available; a confirmation dialog reminds the owner to submit manually on the
employer website first. The confirmed action records the owner's submission
through the existing guarded SQLite transition and does not use the browser.
The managed window is not automatically closed or released.

Narrative Review shows a unique approved human replacement as the current answer
and keeps its matching approved AI draft in a collapsible history section. The
pair must share task, page, question label, and semantic key, with the replacement
later in report order. Ambiguous entries stay separate rather than inventing a
replacement relationship. Both original and replacement remain persisted.

The original development build is a Swift package. The local `.app` installer
assembles its executable and Python backend without changing this frontend.
