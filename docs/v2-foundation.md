# V2 foundation scope

V2-0 and V2-1 provide a local behavioral fixture, standard-library tests, and pure domain contracts. They do not integrate Playwright MCP or control a browser.

Known v1 issues intentionally left untouched:

- `injection.py` fills nonempty low-confidence values and marks them red, although the README implies such values are withheld.
- `navigator.py` increments `applications_submitted` after the review routine returns, even if review was declined or submission was not verified.

V2 answer data keeps provenance, scope, and approval separate. V2 application outcomes distinguish review, submission attempts, verified submissions, and unverified submissions. `ActionPolicy` requires a current human approval before granting a submission permit; the later browser adapter must require that permit for its submit operation. Python data structures cannot authenticate a person on their own, so the trusted human-review surface remains a later integration responsibility.

Run focused tests with `python3 -m unittest discover -s tests -v`.
