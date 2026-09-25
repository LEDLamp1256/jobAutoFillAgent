# V2-2 Playwright MCP browser slice

This slice uses Python MCP SDK `mcp>=2,<3` through its asynchronous `Client(StdioServerParameters(...))` API. Verification used **mcp 2.2.0**, **@playwright/mcp 0.0.82**, Node **24.11.1**, and headless Google Chrome **153.0.8010.53** on macOS. The repository does not install or configure the MCP server globally. The adapter accepts a `MCPServerCommand`, so the caller supplies the executable and arguments.

For a local Mac test, install the Python requirements into a virtual environment and install the pinned server into an isolated directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install --prefix /private/tmp/jobagent-v2-playwright-mcp --no-save @playwright/mcp@0.0.82
```

Node and Google Chrome must be installed separately. The real test invokes the server with `--headless --isolated --no-webmcp --browser chrome`; it neither uses the normal Chrome profile nor configures authentication or persistent storage. It serves the tracked fixture on a loopback ephemeral port and closes both server processes afterward.

Pure tests, including the V2-0/V2-1 regression suite:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Run the one real browser test explicitly:

```sh
JOB_AGENT_RUN_MCP_BROWSER_TESTS=1 \
JOB_AGENT_PLAYWRIGHT_MCP_CLI=/private/tmp/jobagent-v2-playwright-mcp/node_modules/@playwright/mcp/cli.js \
.venv/bin/python -m unittest tests.test_mcp_integration -v
```

The pinned server's `browser_snapshot` result was observed as a **text content block**: a page URL line and a fenced YAML-like accessibility snapshot. `structured_content` was absent. The narrow normalizer extracts the local fixture's headings, progress paragraph, textboxes, radio group/options, alerts, and buttons. It does not parse raw DOM, screenshots, or page instructions. The fixture snapshots did not expose `required` attributes, so `required` remains false unless a future snapshot provides it. Text and radio values are represented when visible. Snapshot refs are retained only for the current observation; every action obtains a new snapshot.

Only explicitly classified navigation labels may be used as routine navigation. The fixture test supplies `Continue` and `Back` classifications. A submit-like label cannot be reclassified as ordinary navigation, and the public adapter has no submit operation. The test stops at final review without submitting. This stage has no controller, answer ledger, LLM, Workday logic, login, or application recovery.
