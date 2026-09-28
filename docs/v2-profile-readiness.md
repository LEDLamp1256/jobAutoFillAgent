# Candidate profile readiness after V2-6

`CandidateProfile.from_json(path)` is the normal loader for trusted candidate
facts. The acceptance CLI requires an explicit `--config` path for every fill
stage; observation needs no profile. The loader neither logs nor rewrites the
private file. `config.EXAMPLE.json` documents the expected shape.

The seven required top-level sections are `personal_info`, `work_history`,
`education`, `skills`, `qa_bank`, `documents`, and `application_preferences`.
The first five object sections must be objects; `work_history` and `education`
must be lists of objects. `personal_info.address`, when present, must be an
object. Known personal and address text fields must be text; known work
authorization, sponsorship, and relocation fields must be booleans. Each Q&A
entry must be an object with an `answer` key containing a scalar or `null`.
`null` is an intentionally unanswered item and cannot be filled. Q&A scope,
when present, is `global` or `application`; an application ID must be text.
Extra legacy sections are currently ignored by the v2 profile model.

The owner's private `config.json` has all seven top-level sections and the
expected address keys. It lacks `personal_info.first_name` and
`personal_info.last_name`. Those fields are optional in the schema, but the
real ATS First Name and Last Name questions remain unresolved until the owner
adds explicit text values. The resolver never splits `full_name`. Two existing
Q&A entries use `answer: null`; the v2 loader now accepts them as missing facts
and the resolver leaves them unresolved. No personal values or secrets belong
in this document or tracked tests.

For normal filling, the owner should add these keys to the private file with
their own values:

```json
"personal_info": {
  "first_name": "<owner-provided>",
  "last_name": "<owner-provided>"
}
```

This fragment shows the keys only; merge them into the existing
`personal_info` object. Other missing answers may remain `null` until the
owner supplies them. Passwords stay outside the candidate profile and are
available only through a separate credential provider.
