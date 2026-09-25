"""Conservative, browser-independent resolution of known application questions."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .domain import Answer, AnswerScope, AnswerSource, ControlType, QuestionObservation


class ProfileError(ValueError):
    """Candidate configuration is malformed; no fallback values are invented."""


@dataclass(frozen=True)
class CandidateProfile:
    personal_info: Mapping[str, Any]
    work_history: tuple[Mapping[str, Any], ...]
    education: tuple[Mapping[str, Any], ...]
    skills: Mapping[str, Any]
    qa_bank: Mapping[str, Mapping[str, Any]]
    documents: Mapping[str, Any]
    application_preferences: Mapping[str, Any]

    @classmethod
    def from_json(cls, path: str | Path) -> CandidateProfile:
        try:
            with Path(path).open(encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"could not load candidate profile: {type(exc).__name__}") from exc
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Any) -> CandidateProfile:
        if not isinstance(data, dict):
            raise ProfileError("candidate profile must be an object")
        objects = ("personal_info", "skills", "qa_bank", "documents", "application_preferences")
        lists = ("work_history", "education")
        for key in objects:
            if key not in data or not isinstance(data[key], dict):
                raise ProfileError(f"{key} must be an object")
        for key in lists:
            if key not in data or not isinstance(data[key], list) or not all(
                isinstance(item, dict) for item in data[key]
            ):
                raise ProfileError(f"{key} must be a list of objects")
        if "address" in data["personal_info"] and not isinstance(data["personal_info"]["address"], dict):
            raise ProfileError("personal_info.address must be an object")
        for key in ("full_name", "first_name", "last_name", "preferred_name", "email", "phone",
                    "linkedin_url", "github_url", "portfolio_url"):
            if key in data["personal_info"] and not isinstance(data["personal_info"][key], str):
                raise ProfileError(f"personal_info.{key} must be text")
        for key in ("authorized_to_work_us", "requires_visa_sponsorship", "willing_to_relocate"):
            if key in data["personal_info"] and not isinstance(data["personal_info"][key], bool):
                raise ProfileError(f"personal_info.{key} must be boolean")
        for key, value in data["personal_info"].get("address", {}).items():
            if key in {"street", "city", "state", "zip_code", "country"} and not isinstance(value, str):
                raise ProfileError(f"personal_info.address.{key} must be text")
        if not all(isinstance(key, str) and isinstance(value, dict) and
                   isinstance(value.get("answer"), (str, bool, int, float))
                   for key, value in data["qa_bank"].items()):
            raise ProfileError("qa_bank entries require a scalar answer")
        if not all(value.get("scope") in (None, "global", "application") and
                   ("application_id" not in value or isinstance(value["application_id"], str))
                   for value in data["qa_bank"].values()):
            raise ProfileError("qa_bank scope or application_id is malformed")
        data = deepcopy(data)
        return cls(
            personal_info=dict(data["personal_info"]),
            work_history=tuple(dict(item) for item in data["work_history"]),
            education=tuple(dict(item) for item in data["education"]),
            skills=dict(data["skills"]),
            qa_bank={key: dict(value) for key, value in data["qa_bank"].items()},
            documents=dict(data["documents"]),
            application_preferences=dict(data["application_preferences"]),
        )


class CanonicalStatus(str, Enum):
    MATCHED = "matched"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CanonicalResult:
    status: CanonicalStatus
    semantic_key: str | None = None
    rule: str | None = None
    reason: str = ""


def _norm(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (value or "").casefold()).split())


_ALIASES: dict[str, tuple[str, ...]] = {
    "personal.first_name": ("first name", "legal first name", "given name", "confirm first name"),
    "personal.last_name": ("last name", "legal last name", "family name", "surname"),
    "personal.full_name": ("full name", "legal name", "legal full name"),
    "personal.preferred_name": ("preferred name", "preferred first name"),
    "personal.email": ("email", "email address", "e mail address", "confirm email", "confirm email address"),
    "personal.phone": ("phone", "phone number", "mobile phone", "telephone number"),
    "personal.address.street": ("street address", "address line 1", "street address line 1"),
    "personal.address.city": ("city", "city of residence"),
    "personal.address.state": ("state", "state province", "state or province"),
    "personal.address.postal": ("zip code", "postal code", "zip postal code"),
    "personal.address.country": ("country", "country of residence"),
    "personal.linkedin": ("linkedin", "linkedin url", "linkedin profile"),
    "personal.github": ("github", "github url", "github profile"),
    "personal.portfolio": ("portfolio", "portfolio url", "personal website"),
    "employment.us_authorized": ("are you authorized to work in the us", "are you legally authorized to work in the united states", "authorized to work in the united states", "us work authorization"),
    "employment.sponsorship": ("do you require visa sponsorship", "will you require visa sponsorship", "will you now or in the future require sponsorship for employment visa status", "requires visa sponsorship"),
    "employment.relocation": ("are you willing to relocate", "willing to relocate"),
    "education.institution": ("school", "school name", "institution", "institution name", "university"),
    "education.degree": ("degree", "degree earned"),
    "education.gpa": ("gpa", "grade point average"),
    "documents.resume": ("resume", "resume cv", "cv", "upload resume", "upload cv"),
    "desired_salary": ("desired salary", "salary expectation", "expected salary"),
    "why_this_company": ("why do you want to work here", "why do you want to work for this company"),
    "why_this_role": ("why are you interested in this role",),
    "eeo_gender": ("gender", "gender identity"),
    "eeo_race_ethnicity": ("race ethnicity", "race", "ethnicity"),
    "eeo_veteran_status": ("veteran status",),
    "eeo_disability_status": ("disability status",),
}
_LOOKUP = {alias: key for key, aliases in _ALIASES.items() for alias in aliases}
_PROFILE_KEYS = {key for key in _ALIASES if key.startswith(("personal.", "employment.", "education.", "documents."))}
_GLOBAL_KEYS = {key for key in _PROFILE_KEYS if not key.startswith("education.")}
_SENSITIVE_KEYS = {key for key in _ALIASES if key.startswith("eeo_")}


class SemanticCanonicalizer:
    def canonicalize(self, question: QuestionObservation) -> CanonicalResult:
        label = _norm(question.label)
        key = _LOOKUP.get(label)
        if key is None:
            if label in {"start date", "end date", "work authorization", "name", "address"}:
                return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="label needs more context")
            return CanonicalResult(CanonicalStatus.UNRESOLVED, reason="no explicit semantic rule")
        if key.startswith("education.") and "educat" not in _norm(question.section) and not question.record_context:
            return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="education context is missing")
        if key.startswith("personal.address.") and any(
            context in _norm(question.section) for context in ("employment", "work history", "education")
        ):
            return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="address belongs to another record context")
        if key.startswith("personal.") and any(
            context in _norm(question.section)
            for context in ("reference", "emergency contact", "supervisor", "recruiter", "employer")
        ):
            return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="field may describe another person")
        if key == "documents.resume" and question.control_type is not ControlType.FILE:
            return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="resume is not an upload control")
        if question.semantic_key and question.semantic_key != key:
            return CanonicalResult(CanonicalStatus.AMBIGUOUS, reason="observed semantic hint conflicts with label")
        return CanonicalResult(CanonicalStatus.MATCHED, key, "explicit_alias")


class ResolutionStatus(str, Enum):
    SAFE_TO_FILL = "safe_to_fill"
    UNRESOLVED = "unresolved"
    REQUIRES_REVIEW = "requires_review"


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    canonical: CanonicalResult
    answer: Answer | None = None
    reason: str = ""


@dataclass
class AnswerLedger:
    """In-memory reuse; application entries never leak into other applications."""

    _entries: dict[tuple[str, AnswerScope, str | None], Answer] = field(default_factory=dict)

    def record(self, answer: Answer) -> None:
        if answer.scope is AnswerScope.GLOBAL and answer.semantic_key not in _GLOBAL_KEYS:
            raise ValueError("this semantic key cannot be globally reused")
        if answer.source is AnswerSource.LOCAL_LLM and not answer.human_approved:
            raise ValueError("model answers require human approval before ledger reuse")
        if not answer.safe_for_automatic_fill(answer.application_id or ""):
            raise ValueError("only trusted or human-approved answers can enter the ledger")
        self._entries[(answer.semantic_key, answer.scope, answer.application_id)] = answer

    def lookup(self, semantic_key: str, application_id: str) -> Answer | None:
        for scope, owner in ((AnswerScope.APPLICATION, application_id), (AnswerScope.GLOBAL, None)):
            answer = self._entries.get((semantic_key, scope, owner))
            if answer and answer.safe_for_automatic_fill(application_id):
                return answer
        return None

    def application_human_correction(self, semantic_key: str, application_id: str) -> Answer | None:
        answer = self._entries.get((semantic_key, AnswerScope.APPLICATION, application_id))
        return answer if answer and answer.source is AnswerSource.HUMAN and answer.human_approved else None


def _profile_value(profile: CandidateProfile, key: str, question: QuestionObservation) -> Any | None:
    personal = profile.personal_info
    fields = {
        "personal.full_name": "full_name", "personal.preferred_name": "preferred_name",
        "personal.email": "email", "personal.phone": "phone",
        "personal.linkedin": "linkedin_url", "personal.github": "github_url",
        "personal.portfolio": "portfolio_url", "employment.us_authorized": "authorized_to_work_us",
        "employment.sponsorship": "requires_visa_sponsorship",
        "employment.relocation": "willing_to_relocate",
    }
    if key in {"personal.first_name", "personal.last_name"}:
        return personal.get(key.removeprefix("personal."))
    if key.startswith("personal.address."):
        address = personal.get("address", {})
        return address.get({"postal": "zip_code"}.get(key.rsplit(".", 1)[-1], key.rsplit(".", 1)[-1]))
    if key in fields:
        return personal.get(fields[key])
    if key.startswith("education."):
        entries = profile.education
        if question.record_context:
            matches = [entry for entry in entries if _norm(str(entry.get("institution", ""))) == _norm(question.record_context)]
        else:
            matches = list(entries)
        if len(matches) != 1:
            return None
        return matches[0].get(key.split(".")[1])
    if key == "documents.resume":
        return profile.documents.get("resume_path")
    return None


def _option_value(value: Any, options: tuple[str, ...]) -> str | None:
    if isinstance(value, bool):
        if options and {_norm(option) for option in options} != {"yes", "no"}:
            return None
        desired = "yes" if value else "no"
        return next((option for option in options if _norm(option) == desired), "Yes" if value else "No")
    if not isinstance(value, (str, int, float)) or not str(value).strip():
        return None
    rendered = str(value).strip()
    if not options:
        return rendered
    matches = [option for option in options if _norm(option) == _norm(rendered)]
    return matches[0] if len(matches) == 1 else None


class DeterministicAnswerResolver:
    def __init__(self, profile: CandidateProfile, ledger: AnswerLedger | None = None,
                 canonicalizer: SemanticCanonicalizer | None = None):
        self.profile = profile
        self.ledger = ledger if ledger is not None else AnswerLedger()
        self.canonicalizer = canonicalizer or SemanticCanonicalizer()

    def resolve(self, question: QuestionObservation, application_id: str) -> Resolution:
        if not application_id.strip():
            raise ValueError("application_id must be nonempty")
        canonical = self.canonicalizer.canonicalize(question)
        if canonical.status is not CanonicalStatus.MATCHED or canonical.semantic_key is None:
            return Resolution(ResolutionStatus.UNRESOLVED, canonical, reason=canonical.reason)
        key = canonical.semantic_key
        correction = self.ledger.application_human_correction(key, application_id)
        if correction:
            return self._finish(question, canonical, correction, application_id)
        if key in _PROFILE_KEYS:
            value = _profile_value(self.profile, key, question)
            if value is not None and str(value).strip():
                answer = Answer(key, str(value), AnswerSource.CANDIDATE_PROFILE,
                                AnswerScope.GLOBAL if key in _GLOBAL_KEYS else AnswerScope.APPLICATION,
                                None if key in _GLOBAL_KEYS else application_id)
                return self._finish(question, canonical, answer, application_id, raw_value=value)
        # Education answers need a record identifier, which the v2 Answer type
        # does not yet carry. Never reuse a key-only ledger answer for them.
        ledger_answer = None if key.startswith("education.") else self.ledger.lookup(key, application_id)
        if ledger_answer:
            return self._finish(question, canonical, ledger_answer, application_id)
        entry = self.profile.qa_bank.get(key)
        if entry is None and key == "employment.sponsorship":
            entry = self.profile.qa_bank.get("will_require_visa_sponsorship")
        if entry is not None:
            scope_text = entry.get("scope")
            if key in {"why_this_company", "why_this_role"}:
                if scope_text != "application" or entry.get("application_id") != application_id:
                    return Resolution(ResolutionStatus.REQUIRES_REVIEW, canonical,
                                      reason="narrative answer requires explicit application scope")
            elif scope_text == "application" and entry.get("application_id") != application_id:
                return Resolution(ResolutionStatus.UNRESOLVED, canonical, reason="Q&A answer belongs to another application")
            if isinstance(entry.get("answer"), str) and re.search(r"\[[^]]+\]", entry["answer"]):
                return Resolution(ResolutionStatus.REQUIRES_REVIEW, canonical, reason="Q&A answer contains a placeholder")
            if key in _SENSITIVE_KEYS and not str(entry["answer"]).strip():
                return Resolution(ResolutionStatus.UNRESOLVED, canonical, reason="no explicit sensitive-field preference")
            scope = AnswerScope.APPLICATION if (scope_text == "application" or key not in _GLOBAL_KEYS) else AnswerScope.GLOBAL
            answer = Answer(key, str(entry["answer"]), AnswerSource.QA_BANK, scope,
                            application_id if scope is AnswerScope.APPLICATION else None)
            return self._finish(question, canonical, answer, application_id, raw_value=entry["answer"])
        return Resolution(ResolutionStatus.UNRESOLVED, canonical, reason="no trusted configured answer")

    @staticmethod
    def _finish(question: QuestionObservation, canonical: CanonicalResult, answer: Answer,
                application_id: str, raw_value: Any | None = None) -> Resolution:
        value = _option_value(answer.value if raw_value is None else raw_value, question.options)
        if value is None:
            return Resolution(ResolutionStatus.UNRESOLVED, canonical, reason="value does not unambiguously match options")
        result = Answer(answer.semantic_key, value, answer.source, answer.scope, answer.application_id,
                        answer.confidence, answer.requires_human_approval, answer.human_approved)
        if not result.safe_for_automatic_fill(application_id):
            return Resolution(ResolutionStatus.REQUIRES_REVIEW, canonical, reason="answer is not approved for automatic filling")
        return Resolution(ResolutionStatus.SAFE_TO_FILL, canonical, result)
