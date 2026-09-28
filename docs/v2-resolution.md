# V2-3 deterministic answer resolution

`jobagent.resolution` consumes `QuestionObservation` data. It has no browser,
MCP, network, or model dependency. Callers must still obtain fresh observations
after browser mutations and must not treat a resolution result as an action.

`CandidateProfile.from_json()` accepts the existing `config.EXAMPLE.json`
sections: `personal_info`, `work_history`, `education`, `skills`, `qa_bank`,
`documents`, and `application_preferences`. Tests use sanitized data. Missing
facts remain missing; malformed sections raise `ProfileError`. The loader does
not log or write profile content.
For first and last names, optional explicit `personal_info.first_name` and
`personal_info.last_name` values are used. `full_name` is not split into parts.
Each Q&A entry needs an `answer` key. A scalar value is a configured answer;
`null` means no answer is configured and resolves as `UNRESOLVED`. Missing
`answer` keys and complex answer values remain invalid. The candidate config
is passed explicitly to the acceptance CLI with `--config`; secrets belong in
the separate credential provider, not this profile.

The canonicalizer uses a short explicit alias table. It returns matched,
ambiguous, or unresolved with a rule/reason. Browser refs never affect the
result. Context is required for education fields and guards address fields in
work or education sections. Labels outside the table remain unresolved.

Resolution precedence is:

1. A human-approved correction scoped to this application.
2. An available authoritative candidate-profile fact.
3. A trusted ledger entry in the application or global scope.
4. An exact configured Q&A answer.
5. Unresolved.

Basic profile facts win over ordinary ledger entries. A correction never
updates the profile. Job-specific answers, including salary and narratives,
are application-scoped. Narrative Q&A answers require an explicit matching
`scope: application` and `application_id`; placeholders require review.
Sensitive/voluntary answers require an explicit Q&A entry. Education answers
are not reused from the current key-only ledger because it cannot identify a
specific education record. This is a deliberate limit until record identity
is represented in answer provenance.

Only `SAFE_TO_FILL` carries an answer suitable for later automatic action.
Option matching allows exact case/whitespace-equivalent text and boolean
values for an exact Yes/No option set. Any ambiguous option set remains
unresolved. V2-3 does not fill browser fields or invoke an LLM.
