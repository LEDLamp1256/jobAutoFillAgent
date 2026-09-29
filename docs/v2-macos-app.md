# Local macOS app install (V2-10.5)

Run `Scripts/install-macos-app.sh` from this checkout to build and install the
local app. The same command updates it later. It always replaces
`~/Applications/Job Application Agent.app`; it does not create a versioned copy
or require sudo. Quit the app before updating. The script builds the existing
SwiftPM executable, assembles a normal `APPL` bundle with `Info.plist` and a
provisional icon, validates it, and swaps the complete bundle into place.
The bundle ID is permanently `com.ledlamp1256.jobapplicationagent`. The icon
artwork is temporary; changing it later does not change the app's identity.

The installer also calls `Scripts/bootstrap-macos-runtime.sh` to prepare one
stable per-user runtime under
`~/Library/Application Support/Job Application Agent/runtime/`. It uses the
repository's existing Python requirements, pinned `@playwright/mcp@0.0.82`,
and a link to the installed Node executable. Python 3, Node/npm, and Google
Chrome are local prerequisites. Network access may be needed for the first
explicit install; app launches never download dependencies. Later installs
reuse the runtime when its requirements are present. Re-run the bootstrap
script if Node or Python moves. This is a local development install for this
Mac, not a redistributable or notarized build.

The app carries only the Python `jobagent` package in its Resources/Backend
directory. It launches that package as a child process through the existing
NDJSON control plane. It stores no user state in the bundle. The default
database is `~/Library/Application Support/Job Application Agent/applications.sqlite3`.
The optional login configuration is
`~/Library/Application Support/Job Application Agent/config.json`. Keep it
private; installation never copies an existing repository `config.json` or
overwrites either file. An existing database elsewhere is not migrated
automatically. If the runtime or database is unavailable, the app shows an
actionable connection error and offers Reconnect Backend.

The normal install needs no Terminal environment variables on launch. The
older `swift run` development route still accepts `JOBAGENT_BACKEND_ROOT`,
`JOBAGENT_DB_PATH`, and `JOBAGENT_PYTHON` as before. Build products are kept
under `~/Library/Caches/Job Application Agent/`, outside the repository. The
packaged backend does not write Python bytecode into the app, and V2-10 keeps
MCP browser artifacts in owned temporary directories outside the app and
repository.

To uninstall only the app, quit it and remove
`~/Applications/Job Application Agent.app`. That leaves the database, config,
and runtime intact. To intentionally erase user data as a separate action,
remove `~/Library/Application Support/Job Application Agent/` after backing up
anything you need. The installer does not perform that deletion.

The bundle receives only a local ad hoc signature using its stable bundle ID.
The bundle is intended for local builds. Developer ID signing, notarization,
Mac App Store packaging, universal builds, automatic updates, and `.pkg`/`.dmg`
distribution are outside this stage.
