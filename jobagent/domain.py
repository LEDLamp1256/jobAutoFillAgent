"""Pure application state and submission contracts; no browser implementation."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from datetime import date
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias


class ControlType(str, Enum):
    TEXT = "text"
    DATE = "date"
    SECRET = "secret"
    CHOICE = "choice"
    MULTI_CHOICE = "multi_choice"
    TYPEAHEAD = "typeahead"
    TOGGLE = "toggle"
    FILE = "file"
    UNKNOWN = "unknown"


class AnswerEvidence(str, Enum):
    SNAPSHOT_VALUE = "snapshot_value"
    SELECTED_OPTION = "selected_option"
    SELECTED_ITEMS = "selected_items"
    CHECKED_STATE = "checked_state"
    BUTTON_SELECTION = "button_selection"
    DOM_VALUE = "dom_value"
    DOM_ASSOCIATED = "dom_associated"


_EMPTY_ANSWER_CAPTIONS = frozenset({
    "select", "select one", "select an option", "please select", "please select an option",
    "choose", "choose one", "choose an option", "please choose", "please choose an option",
    "none selected", "no selection", "not selected", "-- select --", "no file chosen",
    "no file selected", "nothing selected",
    "none", "empty",
})


@dataclass(frozen=True)
class CurrentAnswerState:
    value: str | None  # Runtime only; never put this in a report or diagnostic.
    evidence: AnswerEvidence | None
    placeholder: bool
    satisfied: bool


class NavigationKind(str, Enum):
    ADVANCE = "advance"
    BACK = "back"
    SUBMIT = "submit"
    UNKNOWN = "unknown"


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


QuestionIdentity: TypeAlias = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class QuestionObservation:
    label: str
    control_type: ControlType = ControlType.UNKNOWN
    semantic_key: str | None = None
    section: str | None = None
    record_context: str | None = None
    options: tuple[str, ...] = ()
    required: bool | None = None  # None means the browser did not establish optionality.
    current_value: str | None = None
    target_ref: str | None = None  # Valid only for this browser observation.
    occurrence: int = 0  # Distinguishes repeated labels within one structural context.
    selection_confirmed: bool = False  # A typeahead search string alone is not an answer.
    date_format: str | None = None  # Browser-observed syntax: date, month, or MM/YYYY.
    answer_evidence: AnswerEvidence | None = None
    placeholder_text: str | None = None
    raw_role: str | None = None  # Diagnostic evidence from the current observation only.
    required_evidence: str | None = None
    selected_values: tuple[str, ...] = ()  # Explicit live multiselect selections only.
    discovery_source: str = "accessibility"
    state_conflict: bool = False

    def answer_state(self) -> CurrentAnswerState:
        if self.state_conflict:
            return CurrentAnswerState(None, None, False, False)
        value = _normalized(self.current_value)
        placeholder = (not value or value in _EMPTY_ANSWER_CAPTIONS or
                       value == _normalized(self.label) or
                       (self.placeholder_text is not None and
                        value == _normalized(self.placeholder_text)))
        if self.control_type is ControlType.MULTI_CHOICE:
            selected = tuple(item.strip() for item in self.selected_values
                             if _normalized(item) and
                             _normalized(item) not in _EMPTY_ANSWER_CAPTIONS and
                             _normalized(item) != _normalized(self.label))
            return CurrentAnswerState(", ".join(selected) if selected else None,
                                      self.answer_evidence, not selected, bool(selected))
        if self.control_type is ControlType.TOGGLE and value == "unchecked":
            placeholder = True  # Presence of an untouched checkbox is not an answer.
        if self.control_type is ControlType.DATE and not placeholder:
            fmt = self.date_format
            if fmt == "date" or (fmt is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
                try:
                    date.fromisoformat(value)
                except ValueError:
                    placeholder = True
            elif fmt == "month" or (fmt is None and re.fullmatch(r"\d{4}-\d{2}", value)):
                placeholder = re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None
            elif fmt == "MM/YYYY" or (fmt is None and re.fullmatch(r"\d{2}/\d{4}", value)):
                placeholder = re.fullmatch(r"(0[1-9]|1[0-2])/\d{4}", value) is None
            else:
                placeholder = True  # A partial/unrecognized date needs review.
        search_only = (self.control_type in {ControlType.TYPEAHEAD, ControlType.CHOICE} and
                       self.raw_role in {"input", "textbox", "combobox"} and
                       self.answer_evidence is AnswerEvidence.DOM_VALUE and
                       not self.selection_confirmed)
        satisfied = bool(not placeholder and not search_only and
                         (self.control_type is not ControlType.TYPEAHEAD or self.selection_confirmed))
        return CurrentAnswerState(self.current_value if satisfied else None,
                                  self.answer_evidence, placeholder, satisfied)

    def identity(self) -> QuestionIdentity:
        """A local descriptor, not a claim of universal semantic equivalence."""
        return (
            _normalized(self.semantic_key),
            "" if self.semantic_key else _normalized(self.label),
            _normalized(self.section),
            _normalized(self.record_context),
            str(self.occurrence),
        )

    def report_identity(self) -> str:
        """Stable scoped identity without a live ref, value, or widget type."""
        return json.dumps(self.identity(), separators=(",", ":"), ensure_ascii=False)


# Accessible status text that selector widgets expose while open or after a
# choice. It names widget state, never an application question.
SELECTOR_STATUS_LABELS = frozenset({"items selected", "options expanded", "options collapsed"})


def is_selector_status_label(label: str | None) -> bool:
    return " ".join((label or "").casefold().split()) in SELECTOR_STATUS_LABELS


def _diagnostic_label(value: str | None) -> str | None:
    if value is None:
        return None
    bounded = value[:160]
    bounded = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email]", bounded)
    bounded = re.sub(r"https?://\S+", "[url]", bounded)
    bounded = re.sub(r"\b\d{5,}\b", "[number]", bounded)
    return bounded


def current_field_diagnostic(question: QuestionObservation, *, automation_supported: bool,
                             blocking: bool, report_group: str | None = None,
                             manual_resolution: str | None = None) -> dict[str, object]:
    """Opt-in, value-free local trace for one fresh logical question.

    ``satisfied`` and ``blocking`` always describe the fresh machine
    observation; a human attestation is reported beside them, never merged in.
    """
    answer = question.answer_state()
    return {
        "label": _diagnostic_label(question.label),
        "question_identity": sha256(question.report_identity().encode()).hexdigest()[:16],
        "section": _diagnostic_label(question.section),
        "record_context": _diagnostic_label(question.record_context),
        "raw_role": question.raw_role,
        "interaction_kind": question.control_type.value,
        "requiredness": ("required" if question.required is True else
                         "optional" if question.required is False else "unknown"),
        "current_value_present": answer.satisfied,
        "search_text_present": (question.control_type in {ControlType.TYPEAHEAD, ControlType.CHOICE}
                                and question.raw_role in {"input", "textbox", "combobox"}
                                and bool(question.current_value) and not answer.placeholder
                                and not answer.satisfied),
        "committed_selection_present": (question.control_type in {ControlType.TYPEAHEAD,
                                                                    ControlType.CHOICE} and answer.satisfied),
        "required_evidence": question.required_evidence,
        "current_answer": "[redacted]" if answer.satisfied else None,
        "answer_evidence": answer.evidence.value if answer.evidence else None,
        "placeholder": answer.placeholder, "satisfied": answer.satisfied,
        "automation_capability": "supported" if automation_supported else "unsupported",
        "resolver_capability": "not_evaluated",
        "blocking": blocking, "report_group": report_group,
        "manual_resolution": manual_resolution,
        "discovery_source": question.discovery_source,
        "state_conflict": question.state_conflict,
    }


@dataclass(frozen=True)
class NavigationControl:
    label: str
    kind: NavigationKind = NavigationKind.UNKNOWN
    target_ref: str | None = None


@dataclass(frozen=True)
class SectionAction:
    label: str
    section: str | None = None
    target_ref: str | None = None  # Current observation only.


@dataclass(frozen=True)
class DiscoverySummary:
    raw_actionable_count: int = 0
    accessibility_question_count: int = 0
    dom_recovered_field_count: int = 0
    ignored_reasons: tuple[tuple[str, int], ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class ApplicationObservation:
    observation_id: str
    location: str
    heading: str | None = None
    progress_text: str | None = None
    questions: tuple[QuestionObservation, ...] = ()
    validation_messages: tuple[str, ...] = ()
    navigation_controls: tuple[NavigationControl, ...] = ()
    review_like: bool = False
    job_title: str | None = None
    company: str | None = None
    section_actions: tuple[SectionAction, ...] = ()
    discovery_summary: DiscoverySummary | None = None
    # Form-step heading shown between the page heading and the first field
    # (for example "My Information" under a job-title heading).
    checkpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("observation_id must be nonempty")


PAGE_SCOPE_SEPARATOR = " › "


def page_scope(observation: ApplicationObservation) -> str | None:
    """Durable per-task page/checkpoint key; never uses live refs or DOM ids.

    An explicit progress indicator wins. Otherwise the step heading refines
    the page heading, because some ATS pages keep the job title as their only
    stable top heading across every step.
    """
    if observation.progress_text:
        return observation.progress_text
    if observation.checkpoint and observation.heading and observation.checkpoint != observation.heading:
        return f"{observation.heading}{PAGE_SCOPE_SEPARATOR}{observation.checkpoint}"
    return observation.heading or observation.checkpoint


def is_coarser_page_scope(older: str | None, newer: str | None) -> bool:
    """True when ``older`` is the pre-checkpoint key that ``newer`` refines."""
    return bool(older and newer and newer.startswith(older + PAGE_SCOPE_SEPARATOR))


@dataclass(frozen=True)
class ObservationFingerprint:
    """Equality means apparently equivalent semantic state, never failure."""

    parts: tuple[object, ...]


def semantic_fingerprint(observation: ApplicationObservation) -> ObservationFingerprint:
    questions = tuple(sorted((
        q.identity(),
        q.control_type.value,
        tuple(_normalized(option) for option in q.options),
        q.required,
        _normalized(q.current_value),
        tuple(_normalized(value) for value in q.selected_values),
        q.selection_confirmed,
        q.date_format,
    ) for q in observation.questions))
    controls = tuple(sorted((c.kind.value, _normalized(c.label)) for c in observation.navigation_controls))
    section_actions = tuple(sorted((_normalized(c.section), _normalized(c.label))
                                   for c in observation.section_actions))
    return ObservationFingerprint((
        _normalized(observation.location),
        _normalized(observation.heading),
        _normalized(observation.progress_text),
        questions,
        tuple(sorted(_normalized(message) for message in observation.validation_messages)),
        controls,
        section_actions,
        observation.review_like,
    ))


class AnswerSource(str, Enum):
    CANDIDATE_PROFILE = "candidate_profile"
    QA_BANK = "qa_bank"
    LEDGER = "ledger"
    DETERMINISTIC_RULE = "deterministic_rule"
    LOCAL_LLM = "local_llm"
    HUMAN = "human"


class AnswerScope(str, Enum):
    GLOBAL = "global"
    APPLICATION = "application"


@dataclass(frozen=True)
class Answer:
    semantic_key: str
    value: str
    source: AnswerSource
    scope: AnswerScope
    application_id: str | None = None
    confidence: float | None = None
    requires_human_approval: bool = False
    human_approved: bool = False

    def __post_init__(self) -> None:
        if not self.semantic_key.strip():
            raise ValueError("answer semantic_key must be nonempty")
        if self.scope is AnswerScope.APPLICATION and not self.application_id:
            raise ValueError("application-scoped answer requires application_id")
        if self.scope is AnswerScope.GLOBAL and self.application_id is not None:
            raise ValueError("global answer cannot carry application_id")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")

    def reusable_in(self, application_id: str) -> bool:
        return (self.scope is AnswerScope.GLOBAL or self.application_id == application_id) and (
            not self.requires_human_approval or self.human_approved
        )

    def safe_for_automatic_fill(self, application_id: str) -> bool:
        """A nonempty uncertain answer stays pending until a human approves it."""
        return self.reusable_in(application_id) and bool(self.value) and (
            self.human_approved or
            (self.confidence is None and self.source is not AnswerSource.LOCAL_LLM) or
            (self.confidence is not None and self.confidence >= .90)
        )


@dataclass(frozen=True)
class FillText:
    target_ref: str
    observation_id: str
    answer: Answer


@dataclass(frozen=True)
class ChooseOption:
    target_ref: str
    observation_id: str
    answer: Answer


@dataclass(frozen=True)
class RevealOptions:
    target_ref: str
    observation_id: str


@dataclass(frozen=True)
class SearchOptions:
    target_ref: str
    observation_id: str
    answer: Answer


@dataclass(frozen=True)
class Toggle:
    target_ref: str
    observation_id: str
    answer: Answer


@dataclass(frozen=True)
class UploadDocument:
    target_ref: str
    observation_id: str
    document_key: str
    answer: Answer


@dataclass(frozen=True)
class Advance:
    target_ref: str
    observation_id: str


@dataclass(frozen=True)
class GoBack:
    target_ref: str
    observation_id: str


@dataclass(frozen=True)
class Submit:
    target_ref: str
    observation_id: str


RoutineAction: TypeAlias = FillText | ChooseOption | RevealOptions | SearchOptions | Toggle | UploadDocument | Advance | GoBack
ApplicationAction: TypeAlias = RoutineAction | Submit


class ActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    STATE_CHANGED = "state_changed"
    VALIDATION_BLOCKED = "validation_blocked"
    TARGET_MISSING = "target_missing"
    AMBIGUOUS_TARGET = "ambiguous_target"
    NO_PROGRESS = "no_progress"
    FAILED = "failed"


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus
    detail: str = ""
    before: ObservationFingerprint | None = None
    after: ObservationFingerprint | None = None


@dataclass(frozen=True)
class SessionActionRecord:
    """Action diagnostics without a reusable browser target."""

    kind: str
    semantic_key: str | None
    before_observation_id: str
    after_observation_id: str
    outcome: ActionStatus


@dataclass(frozen=True)
class ResolutionTrace:
    observation_id: str
    semantic_key: str | None
    mapping_source: AnswerSource
    answer_source: AnswerSource | None
    status: str


@dataclass(frozen=True)
class StepTransition:
    from_heading: str | None
    from_progress: str | None
    to_heading: str | None
    to_progress: str | None


class ApplicationOutcome(str, Enum):
    IN_PROGRESS = "in_progress"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED_VERIFIED = "submitted_verified"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
    ABANDONED = "abandoned"
    FAILED = "failed"


class SubmissionPermission(str, Enum):
    LOCKED = "locked"
    APPROVED = "approved"


@dataclass(frozen=True)
class HumanApproval:
    """Event supplied by the future trusted human-review surface."""

    application_id: str
    observation_id: str
    fingerprint: ObservationFingerprint


@dataclass(frozen=True)
class SubmissionPermit:
    """Proof that ActionPolicy approved one submit target for one observation."""

    application_id: str
    observation_id: str
    target_ref: str
    _seal: object = field(repr=False, compare=False)


_PERMIT_SEAL = object()


@dataclass
class ApplicationSession:
    application_id: str
    job_url: str
    company: str | None = None
    observations: list[ApplicationObservation] = field(default_factory=list)
    question_history: dict[QuestionIdentity, list[QuestionObservation]] = field(default_factory=dict)
    resolved_questions: dict[QuestionIdentity, Answer] = field(default_factory=dict)
    unresolved_questions: set[QuestionIdentity] = field(default_factory=set)
    validation_problems: tuple[str, ...] = ()
    uploaded_documents: set[str] = field(default_factory=set)
    action_history: list[SessionActionRecord] = field(default_factory=list)
    resolution_history: list[ResolutionTrace] = field(default_factory=list)
    step_history: list[StepTransition] = field(default_factory=list)
    validation_history: list[tuple[str, ...]] = field(default_factory=list)
    outcome: ApplicationOutcome = ApplicationOutcome.IN_PROGRESS
    submission_permission: SubmissionPermission = SubmissionPermission.LOCKED
    _approval: HumanApproval | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("application_id must be nonempty")

    @property
    def current_observation(self) -> ApplicationObservation | None:
        return self.observations[-1] if self.observations else None

    @property
    def previous_observation(self) -> ApplicationObservation | None:
        return self.observations[-2] if len(self.observations) > 1 else None

    def record_observation(self, observation: ApplicationObservation) -> None:
        if self.current_observation and observation.observation_id == self.current_observation.observation_id:
            raise ValueError("new observation must have a new observation_id")
        if self.outcome in {ApplicationOutcome.SUBMITTED_VERIFIED,
                            ApplicationOutcome.SUBMITTED_UNVERIFIED, ApplicationOutcome.ABANDONED,
                            ApplicationOutcome.FAILED}:
            raise ValueError("cannot observe after a terminal state")
        was_submitting = self.outcome is ApplicationOutcome.SUBMITTING
        self._approval = None
        self.submission_permission = SubmissionPermission.LOCKED
        self.observations.append(observation)
        self.validation_problems = observation.validation_messages
        if observation.validation_messages:
            self.validation_history.append(observation.validation_messages)
        for question in observation.questions:
            self.question_history.setdefault(question.identity(), []).append(question)
        if not was_submitting:
            self.outcome = (ApplicationOutcome.READY_FOR_REVIEW if observation.review_like
                            else ApplicationOutcome.IN_PROGRESS)

    def mark_unresolved(self, question: QuestionObservation) -> None:
        self.unresolved_questions.add(question.identity())

    def record_answer(self, question: QuestionObservation, answer: Answer) -> None:
        if question.semantic_key and question.semantic_key != answer.semantic_key:
            raise ValueError("answer does not match question identity")
        if not answer.safe_for_automatic_fill(self.application_id):
            raise ValueError("answer is uncertain, unapproved, or out of scope")
        self.resolved_questions[question.identity()] = answer
        self.unresolved_questions.discard(question.identity())

    def record_upload(self, document_key: str) -> None:
        self.uploaded_documents.add(document_key)

    def approve_submission(self, approval: HumanApproval) -> None:
        observation = self.current_observation
        if self.outcome is not ApplicationOutcome.READY_FOR_REVIEW or observation is None:
            raise PermissionError("application is not at final review")
        if (approval.application_id != self.application_id or
                approval.observation_id != observation.observation_id or
                approval.fingerprint != semantic_fingerprint(observation)):
            raise PermissionError("approval does not match current application review state")
        self._approval = approval
        self.submission_permission = SubmissionPermission.APPROVED
        self.outcome = ApplicationOutcome.APPROVED

    def abandon(self) -> None:
        self._approval = None
        self.submission_permission = SubmissionPermission.LOCKED
        self.outcome = ApplicationOutcome.ABANDONED

    def fail(self) -> None:
        self._approval = None
        self.submission_permission = SubmissionPermission.LOCKED
        self.outcome = ApplicationOutcome.FAILED

    def start_submission(self, permit: SubmissionPermit) -> None:
        observation = self.current_observation
        if (permit._seal is not _PERMIT_SEAL or observation is None or
                permit.application_id != self.application_id or
                permit.observation_id != observation.observation_id or
                self.outcome is not ApplicationOutcome.APPROVED or
                self.submission_permission is not SubmissionPermission.APPROVED):
            raise PermissionError("valid submission permit required")
        self._approval = None
        self.submission_permission = SubmissionPermission.LOCKED
        self.outcome = ApplicationOutcome.SUBMITTING

    def finish_submission(self, *, verified: bool) -> None:
        if self.outcome is not ApplicationOutcome.SUBMITTING:
            raise ValueError("submission was not started")
        self.outcome = (ApplicationOutcome.SUBMITTED_VERIFIED if verified
                        else ApplicationOutcome.SUBMITTED_UNVERIFIED)


class ActionPolicy:
    """Only this boundary turns an approved Submit into an executable permit."""

    @staticmethod
    def authorize(action: ApplicationAction, session: ApplicationSession) -> SubmissionPermit | None:
        observation = session.current_observation
        if observation is None:
            raise PermissionError("no current observation")
        if action.observation_id != observation.observation_id:
            raise PermissionError("action target belongs to a previous observation")
        if session.outcome in {ApplicationOutcome.SUBMITTING, ApplicationOutcome.SUBMITTED_VERIFIED,
                               ApplicationOutcome.SUBMITTED_UNVERIFIED, ApplicationOutcome.ABANDONED,
                               ApplicationOutcome.FAILED}:
            raise PermissionError("application is no longer interactive")

        if isinstance(action, (Advance, GoBack, Submit)):
            matching = [control for control in observation.navigation_controls
                        if control.target_ref == action.target_ref and control.target_ref is not None]
            if len(matching) != 1:
                raise PermissionError("navigation target missing or ambiguous")
            control = matching[0]
            required_kind = (NavigationKind.ADVANCE if isinstance(action, Advance) else
                             NavigationKind.BACK if isinstance(action, GoBack) else NavigationKind.SUBMIT)
            if control.kind is not required_kind:
                raise PermissionError("navigation control is unknown or has another purpose")
            if isinstance(action, Submit):
                approval = session._approval
                if (session.submission_permission is not SubmissionPermission.APPROVED or
                        session.outcome is not ApplicationOutcome.APPROVED or approval is None or
                        approval.application_id != session.application_id or
                        approval.observation_id != observation.observation_id or
                        approval.fingerprint != semantic_fingerprint(observation)):
                    raise PermissionError("human approval for this final review is required")
                return SubmissionPermit(session.application_id, observation.observation_id,
                                        action.target_ref, _PERMIT_SEAL)
            return None

        matching_questions = [question for question in observation.questions
                              if question.target_ref == action.target_ref and question.target_ref is not None]
        if len(matching_questions) != 1:
            raise PermissionError("question target missing or ambiguous")
        question = matching_questions[0]
        if isinstance(action, RevealOptions):
            if question.control_type is not ControlType.CHOICE or question.options:
                raise PermissionError("option reveal requires an observed closed choice")
            return None
        if question.semantic_key and question.semantic_key != action.answer.semantic_key:
            raise PermissionError("answer does not match question identity")
        if not action.answer.safe_for_automatic_fill(session.application_id):
            raise PermissionError("answer is uncertain, unapproved, or out of scope")
        expected_type = (ControlType.TEXT if isinstance(action, FillText) else
                         ControlType.CHOICE if isinstance(action, ChooseOption) else
                         ControlType.TYPEAHEAD if isinstance(action, SearchOptions) else
                         ControlType.TOGGLE if isinstance(action, Toggle) else ControlType.FILE)
        if not (question.control_type is expected_type or
                (isinstance(action, FillText) and question.control_type is ControlType.DATE) or
                (isinstance(action, ChooseOption) and question.control_type is ControlType.TYPEAHEAD)):
            raise PermissionError("action does not match the observed control type")
        if isinstance(action, ChooseOption) and (
                not question.options or action.answer.value not in question.options):
            raise PermissionError("chosen answer is not an observed option")
        return None
