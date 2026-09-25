# V2-5 local semantic fallback

The deterministic resolver runs first. An unmatched, eligible question may
reach `GroundedSemanticResolver`, which asks a `SemanticMapper` to select one
code-supplied semantic key. `OllamaSemanticMapper` uses the local Ollama
`/api/chat` endpoint with JSON output and a configurable model. The default
`llama3.1` preserves the v1 model setting; choose an already-installed model
with `JOB_AGENT_OLLAMA_MODEL` for acceptance. V2-5 adds no dependency: the
existing `requests` requirement handles local HTTP. Install repository
requirements in the test environment before real Ollama acceptance.

The prompt contains the question label, section, repeated-record context,
control type, offered options, and a narrowed list of permitted semantic keys
with short descriptions. It contains **no candidate answer values, full
profile, Q&A answers, resume content, browser refs, or browser tools**. The
visible question is delimited as untrusted data. Redirects and nonlocal
Ollama endpoints are rejected. The model receives no MCP or filesystem tools.
The US authorization key is offered only when the visible question names the
US or United States; a generic “this country” question cannot inherit it.

Accepted model JSON has exactly `status`, `semantic_key`, and `reason`.
Statuses are `MATCH`, `NO_MATCH`, and `AMBIGUOUS`. A `MATCH` key must belong
to that request's allowlist; a non-match must have a null key. Extra keys,
including an `answer` or `confidence`, are invalid. Code then asks the
deterministic resolver for the value. A model cannot create a candidate fact.
The resolution trace records `mapping_source=local_llm` separately from the
trusted answer source, such as `candidate_profile` or `qa_bank`.

Sensitive or voluntary fields never derive substantive answers from the
model. Existing explicit deterministic Q&A preferences can still resolve.
Narrative questions require an exact configured answer or human review;
there is no prose generation. Ambiguous work/education record identity is
deferred. Unknown, malformed, or out-of-allowlist responses remain unresolved.

The in-memory cache keys question label, section, record context, control
type, options, and candidate keys; browser refs are excluded. Each key gets
at most one model call, with no malformed-response retry. A resolver instance
also has a 20-call ceiling by default. Deterministically resolved questions
make zero model calls.

Run fast tests with `python -m unittest discover -s tests`. Real local-model
acceptance is opt-in:

```sh
JOB_AGENT_RUN_OLLAMA_TESTS=1 JOB_AGENT_OLLAMA_MODEL=llama3.1 \
  python -m unittest tests.test_ollama_integration -v
```

Check `ollama --version` and `ollama list` first. Acceptance uses only an
installed local model and does not download one. The browser fixture tests
remain separately gated by `JOB_AGENT_RUN_MCP_BROWSER_TESTS=1`.
