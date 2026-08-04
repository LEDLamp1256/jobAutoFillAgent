"""
Takes scraped form elements and the user's config.json, sends them to a
local Ollama instance for field-mapping, and enforces a confidence-flag
policy so low-confidence guesses never get silently injected into a form.

Returns a validated, structured dict:

    {
      "field_id_or_name": {
          "value": "<text to inject, or null>",
          "confidence": 0-100,
          "needs_review": bool
      },
      ...
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_CHAT_ENDPOINT = f"{OLLAMA_HOST}/api/chat"

# Any locally-pulled model with decent instruction-following and JSON-mode
# support works (llama3.1, qwen2.5, mistral-nemo, etc.)
MODEL_NAME = "llama3.1"

# Fields below this confidence are flagged for manual review instead of
# being auto-filled.
CONFIDENCE_THRESHOLD = 90

# Explicit override for the resume/CV file path. Left empty by default so
# the matcher never fabricates a path -- see rule 9 in the system prompt.
# If empty, _resolve_resume_path() falls back to config.json's
# documents.resume_path.
RESUME_FILE_PATH = ""  # e.g. "/home/you/documents/resume.pdf"


def _resolve_resume_path(config: dict[str, Any]) -> str:
    """Resolve the resume path: RESUME_FILE_PATH override, else
    config.json's documents.resume_path (or a common alternate key)."""
    if RESUME_FILE_PATH:
        return RESUME_FILE_PATH
    documents = config.get("documents", {}) if isinstance(config, dict) else {}
    if not isinstance(documents, dict):
        return ""
    for key in ("resume_path", "resume_file_path", "cv_path"):
        value = documents.get(key)
        if value:
            return value
    return ""

# Network / generation settings
REQUEST_TIMEOUT_SECONDS = 90
LLM_TEMPERATURE = 0.0  # deterministic, low-creativity matching

# Ollama silently truncates prompts that exceed this context window rather
# than erroring. 16384 comfortably covers most ATS forms; raise if
# truncation symptoms appear (see the token-estimate warning below).
OLLAMA_NUM_CTX = 16384

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aiMatcher")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class OllamaConnectionError(Exception):
    """Raised when the local Ollama server can't be reached at all."""


class OllamaResponseError(Exception):
    """Raised when Ollama responds, but the payload is malformed/unusable."""


class MatchValidationError(Exception):
    """Raised when the LLM's JSON doesn't match the expected schema."""


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class FieldMatch:
    """A single resolved form field, post-validation."""
    field_id: str
    value: Optional[str]
    confidence: int
    needs_review: bool
    reason: Optional[str] = None


@dataclass
class MatchResult:
    """Full result of a matching pass over one job application form."""
    matches: dict[str, FieldMatch] = field(default_factory=dict)
    raw_llm_response: Optional[str] = None

    def to_injection_dict(self) -> dict[str, str]:
        """Fields safe to auto-fill (passed the confidence threshold)."""
        return {
            fid: m.value
            for fid, m in self.matches.items()
            if not m.needs_review and m.value is not None
        }

    def review_queue(self) -> dict[str, FieldMatch]:
        """Fields the human needs to look at / fill in manually."""
        return {fid: m for fid, m in self.matches.items() if m.needs_review}


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

def _build_system_prompt() -> str:
    """Builds the system prompt defining the LLM's role and hard rules."""
    return (
        "You are a precise data-mapping engine for a job application "
        "autofill tool. You are given two JSON documents: "
        "(1) a list of form fields scraped from a job application page, "
        "and (2) a candidate's structured professional profile ('config').\n\n"
        "Your ONLY job is to map each form field to the correct value "
        "from the candidate's profile.\n\n"
        "HARD RULES:\n"
        "1. Respond with ONLY valid JSON. No prose, no markdown fences, "
        "no commentary before or after.\n"
        "2. \u26a0\ufe0f CRITICAL, NON-NEGOTIABLE RULE \u26a0\ufe0f: Every field from the input "
        "form_fields list must appear as a key in your output, using its "
        "field_id value copied EXACTLY, CHARACTER-FOR-CHARACTER -- as if by "
        "copy-paste, not by retyping. field_id values are frequently ugly, "
        "long, non-semantic strings generated by JavaScript frameworks: "
        "UUIDs, hashes, or auto-generated IDs such as "
        "\"8548f6fb-ebd9-47cd-9064-bc9f6d0ceb88_input\". This ugliness is "
        "NORMAL and EXPECTED -- it is not a mistake in the input, and it is "
        "NOT your job to fix, shorten, rename, or \"clean up\" it. You MUST "
        "NEVER invent a cleaner or more semantic-looking key in its place "
        "(for example, writing \"_systemfield_phone\" or \"phone_number\" "
        "instead of the actual ugly field_id you were given). Doing so "
        "silently breaks the downstream system: it matches your output "
        "against the ORIGINAL field_id verbatim, byte for byte, and will "
        "discard any key that doesn't match exactly -- even if your "
        "invented key was more readable. If you are even slightly unsure "
        "of a field_id's exact characters, copy it directly from the input "
        "JSON rather than approximating or summarizing it. Only the VALUE "
        "you map requires semantic reasoning about the candidate profile -- "
        "the KEY requires zero reasoning, only exact reproduction.\n\n"
        "EXAMPLE -- follow this pattern exactly:\n"
        "Given this input field:\n"
        '  {"field_id": "#8548f6fb-ebd9-47cd-9064-bc9f6d0ceb88_phone", '
        '"label": "Phone Number", "type": "tel", "options": []}\n\n'
        "CORRECT output (field_id copied verbatim as the key):\n"
        '  {"#8548f6fb-ebd9-47cd-9064-bc9f6d0ceb88_phone": '
        '{"value": "555-123-4567", "confidence": 100, '
        '"reason": "Matched phone field"}}\n\n'
        "INCORRECT outputs -- every one of these is a validation failure, "
        "even though the VALUE would have been correct:\n"
        '  {"_systemfield_phone": {"value": "555-123-4567", ...}}         '
        "<- invented a semantic name instead of copying field_id\n"
        '  {"phone_number": {"value": "555-123-4567", ...}}               '
        "<- invented a semantic name instead of copying field_id\n"
        '  {"8548f6fb-ebd9-47cd-9064-bc9f6d0ceb88_phone": {...}}          '
        '<- dropped the leading "#" -- still not an exact copy\n\n'
        "3. For each field, output an object with exactly these keys:\n"
        "   - \"value\": the text/option to inject, or null if you cannot "
        "confidently determine it.\n"
        "   - \"confidence\": your confidence in this mapping, as an "
        "integer from 0 to 100.\n"
        "   - \"reason\": a short (<15 words) justification.\n"
        "4. If your confidence for a field is below "
        f"{CONFIDENCE_THRESHOLD}, you MUST still return your best-guess "
        "value (do not omit it), but the low confidence score is what "
        "flags it for human review downstream. Never fabricate data that "
        "doesn't exist anywhere in the candidate profile -- if there is "
        "no reasonable basis for an answer, set \"value\" to null and "
        "\"confidence\" to 0.\n"
        "5. For dropdown/select/radio_group fields, only choose from the "
        "provided 'options' list -- never invent an option that isn't "
        "listed.\n"
        "6. Do not guess at sensitive fields (e.g. race, disability, "
        "veteran status, salary expectations) unless the candidate "
        "profile explicitly contains that information. Otherwise return "
        "null with confidence 0.\n"
        "7. Field labels rarely match config key names exactly. Match "
        "SEMANTICALLY, not textually: form labels are natural-language "
        "phrases (\"LinkedIn URL\", \"LinkedIn Profile\", \"Your LinkedIn\"), "
        "while config keys are snake_case identifiers (linkedin_url). Treat "
        "spacing, casing, and wording differences as irrelevant. Search the "
        "ENTIRE candidate_profile object, including nested objects, not "
        "just top-level keys, before concluding a field has no match.\n"
        "8. Do NOT penalize your confidence score if a field label contains "
        "the word \"optional\". If you possess the exact matching data for "
        "an optional field in the candidate profile, fill it and return a "
        "confidence score of 100. Only lower your confidence if you are "
        "genuinely missing the data or are unsure of the semantic match — "
        "never because the field itself is optional.\n"
        "9. If a field's type is \"file\", or its label/context mentions "
        "\"Resume\", \"CV\", \"Curriculum Vitae\", or \"Upload Resume\", "
        "you MUST map its value to the EXACT string provided in "
        "resume_file_path at the top level of this payload — never from "
        "candidate_profile, and never a path you invent yourself. TRUST "
        "THE GIVEN \"type\" FIELD LITERALLY: if a field's input JSON says "
        "\"type\": \"file\", that field IS a file upload input. Do NOT "
        "reinterpret, second-guess, or claim it is actually a "
        "\"textarea\", a plain text field, or any other type — the type "
        "value is ground truth from the scraper, not something for you "
        "to infer or override. Your \"reason\" text must never contradict "
        "the type you were given (e.g. never write something like \"this "
        "is not a resume upload input but a textarea\" — that is a false "
        "statement about data you were directly given). If "
        "resume_file_path is a non-empty string, return that exact "
        "string with confidence 100. If resume_file_path is empty, "
        "return value null and confidence 0 for that field — do not "
        "fabricate a file path under any circumstance, and do not "
        "fabricate a different field type either.\n"
        "10. For fields with type \"date\" or \"custom_date_picker\", "
        "return the value as an ISO 8601 date string (YYYY-MM-DD) only. "
        "If the candidate profile contains a date for that field in any "
        "other format (e.g. \"05/12/1990\", \"December 5, 1990\"), convert "
        "it to YYYY-MM-DD. Never invent a date that isn't present "
        "somewhere in the candidate profile — if no matching date exists, "
        "return null with confidence 0.\n"
        "11. For \"radio_group\" and \"select\" fields specifically, you "
        "MUST reason SEMANTICALLY about which listed option corresponds "
        "to the candidate's real situation — an exact string match "
        "between the option text and a config value is NOT required, and "
        "you must NOT default to null/low-confidence just because no "
        "option's wording matches the config verbatim. For example: a "
        "visa-sponsorship radio_group with options like \"Yes, OPT\", "
        "\"Yes, H1B\", \"No\" should be answered by reasoning from the "
        "candidate's authorization fields (e.g. authorized_to_work_us, "
        "requires_visa_sponsorship, or a qa_bank entry like "
        "will_require_visa_sponsorship) — if the candidate does not "
        "require sponsorship, confidently select whichever \"No\"-"
        "equivalent option is listed, with high confidence. Likewise, a "
        "role/department option list like \"Product Engineering\", "
        "\"Data Science\", \"Design\" should be matched against "
        "job_titles_of_interest, skills, or field of study — pick the "
        "closest semantic match confidently rather than returning null "
        "because the exact wording differs from the config.\n"
        "12. For open-ended \"textarea\" fields (e.g. \"Tell us about a "
        "project you're proud of\", \"Why are you interested in this "
        "role?\") that have NO matching entry in qa_bank: you MUST "
        "SYNTHESIZE an original, conversational 2-4 sentence response in "
        "your own words, grounded in real facts drawn from the projects, "
        "work_history, or skills sections of candidate_profile. Do NOT "
        "copy-paste a raw bullet point or JSON fragment verbatim — that "
        "reads as an obvious data dump, not an answer a human wrote. "
        "IMPORTANT: synthesizing natural prose FROM REAL FACTS already in "
        "the profile is REQUIRED here and does NOT violate rule 4's "
        "'never fabricate' instruction. Fabrication means inventing facts "
        "that don't exist anywhere in the profile — a project never "
        "listed, a skill never mentioned. Rephrasing and connecting REAL "
        "facts into natural sentences is expected, ordinary synthesis, "
        "not fabrication. Base your confidence on how well-supported the "
        "synthesized answer is by real profile data, not on whether your "
        "exact sentence appears verbatim anywhere in candidate_profile.\n"
        "13. STRICT DATA SOURCE RULE: you must ONLY use the provided "
        "candidate_profile (config.json) data to answer fields. Do NOT "
        "use external knowledge, generic resume-writing conventions, "
        "plausible-sounding assumptions, or anything from your own "
        "training data to fill in a field. If a field is not covered "
        "anywhere in candidate_profile, return null with confidence 0 — "
        "a well-written but unsupported answer is a rule violation here, "
        "not a helpful guess. This constrains rule 12's textarea "
        "synthesis too: synthesized prose may ONLY rephrase and connect "
        "facts that are literally present in candidate_profile's "
        "projects/work_history/skills — never generic filler, invented "
        "outcomes, or invented metrics not traceable to a specific field.\n"
        "14. NEVER map any field to an ATS's own \"autofill from resume\" "
        "/ \"parse resume\" / \"auto-fill application\" trigger, even if "
        "it is scraped with field_type \"file\" or a label mentioning "
        "\"resume\". These triggers hand the ENTIRE form over to the "
        "site's own resume-parsing engine, which OVERWRITES every field "
        "you would otherwise map from candidate_profile — selecting one "
        "defeats the entire purpose of this task, which is to fill the "
        "form strictly from config.json (rule 13), not from a resume "
        "parser's own guesses. If a field's label/context contains "
        "phrasing like \"Autofill with Resume\", \"Autofill from "
        "Resume\", \"Parse Resume\", \"Use Resume to Autofill\", or "
        "similar, you MUST return null with confidence 0 for it, "
        "regardless of its declared field_type. Only map "
        "resume_file_path (per rule 9) to a field that is a genuine, "
        "plain resume/CV UPLOAD input — one whose only apparent purpose "
        "is attaching a file, never one that promises to fill out the "
        "rest of the form for you.\n"
        "15. Some forms contain MULTIPLE similarly- or identically-"
        "labeled date fields across different sections (e.g. an "
        "education entry's Start/End Date vs. EACH work_history entry's "
        "Start/End Date). The scraper prefixes generic date labels with "
        "their section heading when it can detect one (e.g. \"Education "
        "— Start Date\" vs \"Work Experience — Start Date\") — use that "
        "prefix as your primary signal for which candidate_profile array "
        "entry a date field belongs to. A field asking for a "
        "\"graduation date\", \"expected graduation\", or an "
        "education-section end/completion date MUST be answered from "
        "education[].end_date — NEVER from any work_history[].start_date "
        "or work_history[].end_date, even if both happen to be the only "
        "dates available. If a date field has no section prefix and its "
        "label is genuinely ambiguous (just \"Start Date\" with nothing "
        "else to go on), you MUST return null with LOW confidence rather "
        "than guessing which section it belongs to — an incorrect date "
        "on a real job application is worse than a blank one the "
        "candidate can fill in manually during review.\n"
        "16. Any field whose wording includes \"expected\" (e.g. "
        "\"expected graduation\"), \"graduation\", or asks whether you "
        "are \"currently enrolled\" in a degree program, MUST be "
        "answered using education[].end_date from candidate_profile — "
        "NEVER education[].start_date, and NEVER any work_history[] "
        "date, even though \"currently enrolled\" sounds present-tense "
        "like it might relate to when something started. \"Expected\" "
        "specifically and always refers to a future/completion date "
        "(when a degree is expected to be FINISHED), never a start "
        "date. This rule applies even if the field's visible text looks "
        "truncated, cut off mid-word, or otherwise incomplete — these "
        "trigger phrases are a strong, unambiguous signal on their own, "
        "and this rule overrides any weaker/more general inference you "
        "might otherwise make from a partial label.\n"
        "17. ADDRESS FIELDS: before mapping ANY field whose label mentions "
        "\"Address\", \"Street\", \"City\", \"State\", \"Province\", \"Zip\", "
        "\"Postal Code\", or \"Country\", first SCAN THE ENTIRE form_fields "
        "list for other address-related fields. If form_fields contains "
        "SEPARATE fields for city/state/zip/country (even if this "
        "particular field is just labeled \"Address\" or \"Address Line "
        "1\" with no further qualifier), you MUST map ONLY the street/"
        "house-number portion to it — from candidate_profile's "
        "personal_info.address.street (or equivalent nested key) — and "
        "map each sibling field to its own single corresponding subkey "
        "(address.city -> the City field, address.state -> the State "
        "field, address.zip_code -> the Zip field, address.country -> "
        "the Country field). NEVER concatenate multiple address "
        "components (e.g. \"123 Main St, Springfield, IL 62704\") into "
        "ONE field when separate component fields exist elsewhere on the "
        "same form — this breaks the form's own city/state/zip fields by "
        "leaving them empty while overflowing the street field. Only "
        "return a single combined full-address string for a field if "
        "form_fields contains NO separate city/state/zip fields anywhere "
        "at all — i.e. this is genuinely the ONLY address-related field "
        "on the entire form.\n"
        "18. For \"select\" or \"radio_group\" fields specifically, your "
        "\"value\" MUST be the exact TEXT of one of the listed 'options' "
        "— never a raw true/false boolean, even if the candidate_profile "
        "source field is itself a boolean (e.g. willing_to_relocate: "
        "true). Translate the boolean into whichever option text means "
        "the same thing (e.g. true -> \"Yes\", false -> \"No\") before "
        "writing your answer. A literal true/false value can never match "
        "an <option> element's text and will always fail to inject even "
        "at 100% confidence.\n"
    )


def _build_user_prompt(form_fields: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """
    Bundles the scraped fields + candidate config into the payload the
    model reasons over. config is passed through exactly as loaded from
    config.json -- no flattening -- so the model reasons over the
    profile's natural nested structure (see rule 7 in the system prompt).

    resume_file_path is sent as a separate top-level key (a fixed local
    filesystem path, not profile data) so rule 9 can source Resume/CV
    fields from it specifically rather than the model inventing a path.

    The data payload comes first; the strict output-schema instruction
    comes last, immediately before generation starts, since under context
    pressure a large data payload can wash out instructions placed early
    in the prompt on smaller/quantized local models. The schema
    requirement is duplicated in the system prompt (rule 2) deliberately.
    """
    resolved_resume_path = _resolve_resume_path(config)
    payload = {
        "form_fields": form_fields,
        "candidate_profile": config,
        "resume_file_path": resolved_resume_path,
    }
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
    field_ids = [f["field_id"] for f in form_fields]

    logger.debug("candidate_profile sent to LLM:\n%s", json.dumps(config, indent=2, ensure_ascii=False))

    source = "RESUME_FILE_PATH override" if RESUME_FILE_PATH else "config.documents.resume_path fallback"
    logger.debug("resolved resume_file_path (%s): %r", source, resolved_resume_path)

    # Rough size check -- if this approaches OLLAMA_NUM_CTX, truncation is
    # the likely cause of an unrelated-looking model output.
    approx_chars = len(payload_json)
    approx_tokens = approx_chars // 4
    logger.debug(
        "user prompt payload: %d chars (~%d tokens estimated) vs OLLAMA_NUM_CTX=%d",
        approx_chars, approx_tokens, OLLAMA_NUM_CTX,
    )
    if approx_tokens > OLLAMA_NUM_CTX * 0.7:
        # Always surfaced (not gated behind --verbose): a real risk of
        # silent truncation is actionable even outside verbose mode.
        logger.warning(
            "Prompt payload is using an estimated %.0f%% of the configured "
            "context window (including system prompt + generation headroom "
            "still to come) -- truncation is a real risk. Consider raising "
            "OLLAMA_NUM_CTX further, or batching form_fields across multiple "
            "smaller match_fields() calls instead of one giant one.",
            approx_tokens / OLLAMA_NUM_CTX * 100,
        )

    return (
        f"{payload_json}\n\n"
        + "=" * 20 + " OUTPUT INSTRUCTIONS -- READ THIS LAST SECTION CAREFULLY " + "=" * 20 + "\n"
        "The ONLY valid output keys are these exact field_id strings, "
        f"copied character-for-character, {len(field_ids)} of them:\n"
        f"{json.dumps(field_ids, indent=2, ensure_ascii=False)}\n\n"
        "Your output MUST be a single, flat JSON object where the keys are "
        "STRICTLY the field_id strings listed immediately above -- nothing "
        "else. Do NOT invent new keys. Do NOT wrap the object in another "
        "key like \"fields\" or \"data\". Do NOT output a schema, a "
        "description, an \"application\"/\"candidate\" envelope, or "
        "anything resembling an API response shape -- ONLY the flat "
        "field_id -> {value, confidence, reason} mapping. For any field "
        "asking for a resume/CV upload, use resume_file_path from the data "
        "above (top-level, not inside candidate_profile) per rule 9.\n\n"
        "Return ONLY that JSON object now. No prose, no markdown fences."
    )


# --------------------------------------------------------------------------- #
# Ollama call
# --------------------------------------------------------------------------- #

def _call_ollama(system_prompt: str, user_prompt: str) -> str:
    """
    Sends a chat request to the local Ollama server and returns the raw
    text content of the model's reply.

    Raises:
        OllamaConnectionError: server unreachable / timed out.
        OllamaResponseError: server reachable but returned an error or
            an unexpected payload shape.
    """
    request_body = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": LLM_TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    try:
        response = requests.post(
            OLLAMA_CHAT_ENDPOINT,
            json=request_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError as exc:
        raise OllamaConnectionError(
            "Could not reach Ollama at "
            f"{OLLAMA_HOST}. Is 'ollama serve' running? "
            "(hint: try `ollama list` in a terminal)"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise OllamaConnectionError(
            f"Ollama did not respond within {REQUEST_TIMEOUT_SECONDS}s. "
            "The model may be too large for this machine, or still loading."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise OllamaConnectionError(f"Unexpected network error: {exc}") from exc

    if response.status_code == 404:
        raise OllamaResponseError(
            f"Model '{MODEL_NAME}' not found. Run `ollama pull {MODEL_NAME}` first."
        )
    if response.status_code != 200:
        raise OllamaResponseError(
            f"Ollama returned HTTP {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise OllamaResponseError("Ollama's HTTP response was not valid JSON.") from exc

    try:
        content = body["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise OllamaResponseError(
            f"Unexpected Ollama response shape, missing message.content: {body}"
        ) from exc

    if not content or not content.strip():
        raise OllamaResponseError("Ollama returned an empty response.")

    return content


# --------------------------------------------------------------------------- #
# Parsing & validation
# --------------------------------------------------------------------------- #

def _strip_code_fences(text: str) -> str:
    """Strips ```json ... ``` fences some models still emit despite format="json"."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_llm_json(raw_content: str) -> dict[str, Any]:
    """Parses the LLM's text output into a dict, raising OllamaResponseError with context on failure."""
    cleaned = _strip_code_fences(raw_content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OllamaResponseError(
            "Failed to parse LLM output as JSON.\n"
            f"Parser error: {exc}\n"
            f"Raw output (truncated): {cleaned[:800]}"
        ) from exc

    if not isinstance(parsed, dict):
        raise OllamaResponseError(
            f"Expected a JSON object at the top level, got {type(parsed).__name__}."
        )
    return parsed


def _normalize_boolean_to_option(value: Any, options: list[str]) -> Any:
    """
    Code-level fix for the LLM echoing a raw JSON boolean (e.g.
    willing_to_relocate: true) instead of the matching option TEXT a real
    select/radio_group actually offers (e.g. "Yes"). Only touches
    select/radio_group/dropdown-shaped fields (options is non-empty);
    free-text fields are untouched.
    """
    if not options:
        return value

    if isinstance(value, bool):
        wants_true = value
    elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
        wants_true = value.strip().lower() == "true"
    else:
        return value

    target_words = ("yes", "true") if wants_true else ("no", "false")
    for option in options:
        if option.strip().lower() in target_words:
            return option

    return value


def _validate_and_build(
    parsed: dict[str, Any], expected_field_ids: set[str], field_options: dict[str, list[str]] | None = None
) -> dict[str, FieldMatch]:
    """
    Cross-checks the LLM's mapping against the expected field schema and
    applies the confidence-threshold policy. field_options (field_id ->
    options list) enables boolean normalization for select/radio_group
    fields. Any field the LLM omitted is filled in as a forced-review
    entry rather than silently dropped.
    """
    field_options = field_options or {}
    matches: dict[str, FieldMatch] = {}

    received_ids = set(parsed.keys())
    missing_ids = expected_field_ids - received_ids
    extra_ids = received_ids - expected_field_ids
    logger.debug(
        "Expected %d field_id(s), LLM response has %d top-level key(s).",
        len(expected_field_ids), len(received_ids),
    )
    if missing_ids:
        logger.debug("field_id(s) we asked for but got NO entry back for: %s", sorted(missing_ids))
    if extra_ids:
        logger.debug("key(s) the LLM returned that we never asked about: %s", sorted(extra_ids))

    for field_id in expected_field_ids:
        entry = parsed.get(field_id)

        if not isinstance(entry, dict):
            logger.warning(
                "Field '%s' missing or malformed in LLM response; flagging for review.",
                field_id,
            )
            matches[field_id] = FieldMatch(
                field_id=field_id,
                value=None,
                confidence=0,
                needs_review=True,
                reason="No usable mapping returned by the model.",
            )
            continue

        value = entry.get("value")
        raw_confidence = entry.get("confidence", 0)
        reason = entry.get("reason")

        options = field_options.get(field_id, [])
        normalized_value = _normalize_boolean_to_option(value, options)
        if normalized_value != value:
            logger.debug(
                "Normalized boolean value for %r: %r -> %r (matched against options %s)",
                field_id, value, normalized_value, options,
            )
            value = normalized_value

        try:
            confidence = int(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0
        confidence = max(0, min(100, confidence))

        needs_review = (value is None) or (confidence < CONFIDENCE_THRESHOLD)

        matches[field_id] = FieldMatch(
            field_id=field_id,
            value=value,
            confidence=confidence,
            needs_review=needs_review,
            reason=reason,
        )

    unexpected_ids = set(parsed.keys()) - expected_field_ids
    if unexpected_ids:
        logger.warning(
            "LLM returned %d unexpected field id(s) not present in the "
            "scraped form; ignoring: %s",
            len(unexpected_ids),
            sorted(unexpected_ids),
        )

    return matches


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def match_fields(
    form_fields: list[dict[str, Any]],
    config: dict[str, Any],
) -> MatchResult:
    """
    Main entry point for Phase 3.

    Args:
        form_fields: Output of the Playwright scraper. One dict per
            field, e.g.: {"field_id": ..., "label": ..., "type": ...,
            "options": [...]}
        config: The parsed master config.json (candidate profile).

    Returns:
        MatchResult containing per-field FieldMatch objects.

    Raises:
        ValueError: if form_fields is empty or malformed.
        OllamaConnectionError / OllamaResponseError: on LLM/network failure.
    """
    if not form_fields:
        raise ValueError("form_fields is empty -- nothing to match.")

    expected_ids = set()
    for f in form_fields:
        if "field_id" not in f:
            raise ValueError(f"Form field missing required 'field_id' key: {f}")
        expected_ids.add(f["field_id"])

    for f in form_fields:
        logger.debug("field_id=%r  label=%r  type=%r", f['field_id'], f.get('label'), f.get('type'))

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(form_fields, config)

    logger.info("Sending %d field(s) to Ollama (model=%s)...", len(form_fields), MODEL_NAME)
    raw_content = _call_ollama(system_prompt, user_prompt)

    logger.debug("Raw LLM response:\n%s", raw_content)

    parsed = _parse_llm_json(raw_content)
    field_options = {f["field_id"]: f.get("options", []) for f in form_fields}
    matches = _validate_and_build(parsed, expected_ids, field_options)

    flagged = sum(1 for m in matches.values() if m.needs_review)
    logger.info(
        "Matching complete: %d/%d fields auto-fillable, %d flagged for review.",
        len(matches) - flagged,
        len(matches),
        flagged,
    )

    return MatchResult(matches=matches, raw_llm_response=raw_content)


# --------------------------------------------------------------------------- #
# Manual test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    sample_form_fields = [
        {"field_id": "input#first_name", "label": "First Name", "type": "text", "options": []},
        {"field_id": "input#last_name", "label": "Last Name", "type": "text", "options": []},
        {
            "field_id": "select#veteran_status",
            "label": "Veteran Status",
            "type": "select",
            "options": ["Yes", "No", "I don't wish to answer"],
        },
        {
            "field_id": "textarea#cover_letter",
            "label": "Why do you want to work here?",
            "type": "textarea",
            "options": [],
        },
    ]

    sample_config = {
        "personal": {"first_name": "Alex", "last_name": "Rivera"},
        "eeo": {},
    }

    try:
        result = match_fields(sample_form_fields, sample_config)
    except (OllamaConnectionError, OllamaResponseError, ValueError) as exc:
        logger.error("Matching failed: %s", exc)
    else:
        print("\n--- Auto-fillable ---")
        print(json.dumps(result.to_injection_dict(), indent=2))

        print("\n--- Needs Review ---")
        for fid, m in result.review_queue().items():
            print(f"{fid}: value={m.value!r} confidence={m.confidence} reason={m.reason!r}")