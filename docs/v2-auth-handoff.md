# Human intervention and login boundary

The headed acceptance runner pauses on access challenges, MFA, email
verification, SSO, or an ambiguous login. `--hold` keeps the isolated MCP
browser open. The owner completes the action in that browser and presses Enter;
the runner then calls `observe()` and classifies the fresh state. Enter alone
never means the challenge succeeded. A still-visible challenge stops. The
runner permits at most three distinct explicit handoffs in one invocation.
There is no CAPTCHA click, solving service, browser fingerprint change,
proxy rotation, or automatic security-code entry.

`LoginOrchestrator` is separate from `ApplicationController`. It recognizes
only an unambiguous Sign In/Log In heading, one email or username textbox, one
password control, and one Sign In/Log In control. A `LoginIdentity` holds
non-secret account ID and username metadata. `CredentialProvider.get_password`
is the only secret retrieval interface. After each identity, password, and
login action, the browser returns a fresh observation; all targets are selected
again. One login attempt is permitted. A subsequent MFA, SSO, challenge,
validation error, or unchanged login state stops or returns to the owner.

The concrete MCP adapter exposes three narrowly checked login operations
alongside its existing application BrowserPort. It does not expose arbitrary
click or JavaScript execution. Password controls normalize as `SECRET`, with
no current value, and are never interpreted as candidate questions. Neither
the application session, AnswerLedger, semantic mapper, diagnostics, nor Git
receives the password. Raw MCP snapshots and browser state are transient in an
isolated temporary directory, which is removed after the run.

The test suite injects a synthetic in-memory credential provider. The real
acceptance CLI does not yet configure a provider or read a password. A future
macOS Keychain-backed implementation can satisfy `CredentialProvider` after a
real conventional login is observed and the account/service naming is agreed.
The normal candidate config remains for candidate facts and is not a password
store. No credential persistence or saved browser session is implemented.
