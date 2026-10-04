"""Bounded sequential application loop; never exposes a submit operation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from urllib.parse import urlsplit

from jobagent.browser import BrowserPort, BrowserActionResult
from jobagent.batch_domain import FailureDiagnostic
from jobagent.failure_diagnostics import safe_failure
from jobagent.domain import (
    ActionPolicy, ActionStatus, Advance, AnswerSource, ApplicationObservation, ApplicationOutcome,
    ApplicationSession, ChooseOption, ControlType, FillText, NavigationKind, RevealOptions, SearchOptions,
    QuestionObservation, ResolutionTrace, SessionActionRecord, StepTransition, Toggle,
    UploadDocument,
    semantic_fingerprint,
)
from jobagent.resolution import (
    AsyncAnswerResolver, CanonicalStatus, DeterministicAnswerResolver, DeterministicAsyncResolver,
    ResolutionStatus,
)


def _norm(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _has_answer(question: QuestionObservation) -> bool:
    return question.answer_state().satisfied


def _trusted_location(location: str) -> bool:
    parts = urlsplit(location)
    return parts.scheme.lower() == "https" or (parts.scheme.lower() == "http" and
                                                parts.hostname in {"localhost", "127.0.0.1"})


@dataclass(frozen=True)
class StepSignature:
    """Stable step evidence; excludes values, validation, IDs, and browser refs."""

    location: str
    heading: str
    progress: str
    questions: tuple[tuple[str, ...], ...]
    controls: tuple[tuple[str, str], ...]
    review_like: bool


def step_signature(observation: ApplicationObservation) -> StepSignature:
    return StepSignature(
        _norm(observation.location), _norm(observation.heading), _norm(observation.progress_text),
        tuple(sorted(q.identity() for q in observation.questions)),
        tuple(sorted((c.kind.value, _norm(c.label)) for c in observation.navigation_controls)),
        observation.review_like,
    )


class AdvanceClassification(str, Enum):
    PROGRESSED = "progressed"
    NEW_QUESTIONS = "new_questions"
    VALIDATION_BLOCKED = "validation_blocked"
    NO_PROGRESS = "no_progress"
    FAILED = "failed"


def classify_advance(before: ApplicationObservation, after: ApplicationObservation,
                     browser_status: ActionStatus) -> AdvanceClassification:
    if browser_status in {ActionStatus.FAILED, ActionStatus.TARGET_MISSING, ActionStatus.AMBIGUOUS_TARGET}:
        return AdvanceClassification.FAILED
    old, new = step_signature(before), step_signature(after)
    marker_changed = (old.heading, old.progress, old.review_like) != (new.heading, new.progress, new.review_like)
    structure_changed = (old.questions, old.controls) != (new.questions, new.controls)
    if marker_changed or (old.location != new.location and structure_changed):
        return AdvanceClassification.PROGRESSED
    old_questions = {q.identity() for q in before.questions}
    new_questions = {q.identity() for q in after.questions}
    if after.validation_messages:
        return AdvanceClassification.VALIDATION_BLOCKED
    if new_questions - old_questions:
        return AdvanceClassification.NEW_QUESTIONS
    return AdvanceClassification.NO_PROGRESS


class ControllerStop(str, Enum):
    PAGE_ADVANCED = "page_advanced"
    STOPPED_BEFORE_ADVANCE = "stopped_before_advance"
    READY_FOR_REVIEW = "ready_for_review"
    NEEDS_REVIEW = "needs_review"
    VALIDATION_BLOCKED = "validation_blocked"
    NO_PROGRESS = "no_progress"
    ACTION_LIMIT_REACHED = "action_limit_reached"
    FAILED = "failed"


@dataclass(frozen=True)
class ControllerLimits:
    max_cycles: int = 80
    max_actions: int = 40
    max_step_transitions: int = 10
    max_same_step_actions: int = 20

    def __post_init__(self) -> None:
        if min(self.max_cycles, self.max_actions, self.max_step_transitions,
               self.max_same_step_actions) < 1:
            raise ValueError("all controller limits must be positive")


@dataclass(frozen=True)
class ControllerResult:
    stop: ControllerStop
    session: ApplicationSession
    reason: str = ""
    fields: tuple[CurrentPageField, ...] = ()
    failure_stage: str | None = None
    failure_diagnostic: FailureDiagnostic | None = None


@dataclass(frozen=True)
class CurrentPageField:
    """Final live question and its nonsecret current-page decision."""

    question: QuestionObservation
    semantic_key: str | None
    action: str
    source: AnswerSource | None = None
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.action in {"filled_text", "selected_option", "selected_toggle",
                               "uploaded_document", "confirmed_trusted"}

    @property
    def needs_review(self) -> bool:
        return self.reason is not None


_ADVANCE_LABELS = {"next", "continue", "save and continue", "save & continue"}


class ApplicationController:
    """One application, one browser, one mutation at a time."""

    def __init__(self, browser: BrowserPort,
                 resolver: AsyncAnswerResolver | DeterministicAnswerResolver,
                 limits: ControllerLimits | None = None):
        self.browser = browser
        self.resolver = (DeterministicAsyncResolver(resolver)
                         if isinstance(resolver, DeterministicAnswerResolver) else resolver)
        self.limits = limits or ControllerLimits()
        self.active_stage = "observe_snapshot"

    async def run(self, session: ApplicationSession, *, allow_advance: bool = True,
                  initial_observation: ApplicationObservation | None = None,
                  current_page_only: bool = False,
                  require_trusted_location: bool = False,
                  manually_resolved: frozenset[str] = frozenset()) -> ControllerResult:
        if session.current_observation is not None:
            raise ValueError("V2-4 controller starts with a new in-memory session")
        if current_page_only:
            return await self._run_current_page(session, initial_observation,
                                                require_trusted_location, manually_resolved)
        actions = same_step_actions = transitions = 0
        advance_needs_repair = False
        try:
            session.record_observation(initial_observation if initial_observation is not None
                                       else await self.browser.navigate(session.job_url))
            for _cycle in range(self.limits.max_cycles):
                observation = session.current_observation
                assert observation is not None
                if require_trusted_location and not _trusted_location(observation.location):
                    return ControllerResult(ControllerStop.FAILED, session, "insecure_page")
                if observation.review_like:
                    return ControllerResult(ControllerStop.READY_FOR_REVIEW, session)
                if any(c.kind is NavigationKind.SUBMIT for c in observation.navigation_controls):
                    return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                            "terminal control is visible outside recognized review")

                mutated = False
                pending: list[QuestionObservation] = []
                for question in observation.questions:
                    resolution = await self.resolver.resolve(question, session.application_id)
                    session.resolution_history.append(ResolutionTrace(
                        observation.observation_id, resolution.canonical.semantic_key,
                        resolution.mapping_source,
                        resolution.answer.source if resolution.answer else None,
                        resolution.status.value))
                    if _has_answer(question):
                        if (resolution.status is ResolutionStatus.SAFE_TO_FILL and
                                resolution.answer is not None and
                                _norm(question.current_value) == _norm(resolution.answer.value)):
                            session.record_answer(question, resolution.answer)
                        # A fresh observed human answer is satisfaction even if
                        # the widget cannot be manipulated by automation.
                        continue
                    if resolution.status is not ResolutionStatus.SAFE_TO_FILL or resolution.answer is None:
                        session.mark_unresolved(question)
                        if question.required is not False:
                            pending.append(question)
                        continue
                    answer = resolution.answer
                    if question.control_type not in {ControlType.TEXT, ControlType.CHOICE} or not question.target_ref:
                        session.mark_unresolved(question)
                        pending.append(question)
                        continue
                    if actions >= self.limits.max_actions or same_step_actions >= self.limits.max_same_step_actions:
                        return ControllerResult(ControllerStop.ACTION_LIMIT_REACHED, session,
                                                "browser action or same-step action limit")
                    action = (FillText(question.target_ref, observation.observation_id, answer)
                              if question.control_type is ControlType.TEXT else
                              ChooseOption(question.target_ref, observation.observation_id, answer))
                    ActionPolicy.authorize(action, session)
                    result = (await self.browser.fill_text(action, session)
                              if isinstance(action, FillText) else await self.browser.choose_option(action, session))
                    actions += 1
                    same_step_actions += 1
                    self._record_action(session, question.control_type.value, answer.semantic_key, observation, result)
                    session.record_observation(result.observation)
                    if result.outcome.status in {ActionStatus.FAILED, ActionStatus.TARGET_MISSING,
                                                 ActionStatus.AMBIGUOUS_TARGET}:
                        session.fail()
                        return ControllerResult(ControllerStop.FAILED, session, result.outcome.status.value)
                    if semantic_fingerprint(observation) == semantic_fingerprint(result.observation):
                        return ControllerResult(ControllerStop.NO_PROGRESS, session,
                                                "answer action did not change observed state")
                    session.record_answer(question, answer)
                    self.resolver.record_answer(answer)
                    advance_needs_repair = False
                    mutated = True
                    break  # Reinterpret every question against the fresh observation.
                if mutated:
                    continue
                if pending:
                    return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                            "visible question lacks a safe answer or supported action")
                if advance_needs_repair:
                    stop = (ControllerStop.VALIDATION_BLOCKED if observation.validation_messages
                            else ControllerStop.NO_PROGRESS)
                    return ControllerResult(stop, session, "new questions produced no repair action")
                if not allow_advance:
                    return ControllerResult(ControllerStop.STOPPED_BEFORE_ADVANCE, session,
                                            "safe current-step actions complete; advancement disabled")
                advance_labels = [c for c in observation.navigation_controls
                                  if _norm(c.label) in _ADVANCE_LABELS]
                controls = [c for c in observation.navigation_controls
                            if c.kind is NavigationKind.ADVANCE and _norm(c.label) in _ADVANCE_LABELS]
                if len(advance_labels) != 1 or len(controls) != 1 or not controls[0].target_ref:
                    return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                            "no unique safe advance control")
                if actions >= self.limits.max_actions or same_step_actions >= self.limits.max_same_step_actions:
                    return ControllerResult(ControllerStop.ACTION_LIMIT_REACHED, session,
                                            "browser action or same-step action limit")
                action = Advance(controls[0].target_ref, observation.observation_id)
                ActionPolicy.authorize(action, session)
                result = await self.browser.activate_navigation(action, session)
                actions += 1
                same_step_actions += 1
                classification = classify_advance(observation, result.observation, result.outcome.status)
                classified_status = {
                    AdvanceClassification.PROGRESSED: ActionStatus.STATE_CHANGED,
                    AdvanceClassification.NEW_QUESTIONS: ActionStatus.STATE_CHANGED,
                    AdvanceClassification.VALIDATION_BLOCKED: ActionStatus.VALIDATION_BLOCKED,
                    AdvanceClassification.NO_PROGRESS: ActionStatus.NO_PROGRESS,
                    AdvanceClassification.FAILED: ActionStatus.FAILED,
                }[classification]
                self._record_action(session, "advance", None, observation, result,
                                    classified_status)
                session.record_observation(result.observation)
                if classification is AdvanceClassification.PROGRESSED:
                    transitions += 1
                    session.step_history.append(StepTransition(
                        observation.heading, observation.progress_text,
                        result.observation.heading, result.observation.progress_text))
                    same_step_actions = 0
                    if transitions > self.limits.max_step_transitions:
                        return ControllerResult(ControllerStop.ACTION_LIMIT_REACHED, session,
                                                "logical step transition limit")
                    continue
                if classification is AdvanceClassification.NEW_QUESTIONS:
                    advance_needs_repair = True
                    continue  # New fields are resolved before any further advance.
                if classification is AdvanceClassification.VALIDATION_BLOCKED:
                    return ControllerResult(ControllerStop.VALIDATION_BLOCKED, session,
                                            "advance remained on step with validation errors")
                if classification is AdvanceClassification.NO_PROGRESS:
                    return ControllerResult(ControllerStop.NO_PROGRESS, session,
                                            "advance did not change the logical step")
                session.fail()
                return ControllerResult(ControllerStop.FAILED, session, result.outcome.detail or "advance failed")
            return ControllerResult(ControllerStop.ACTION_LIMIT_REACHED, session, "controller cycle limit")
        except Exception as exc:
            session.fail()
            return ControllerResult(ControllerStop.FAILED, session, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _record_action(session: ApplicationSession, kind: str, semantic_key: str | None,
                       before: ApplicationObservation, result: BrowserActionResult,
                       status: ActionStatus | None = None) -> None:
        session.action_history.append(SessionActionRecord(
            kind, semantic_key, before.observation_id, result.observation.observation_id,
            status or result.outcome.status))

    async def _run_current_page(self, session: ApplicationSession,
                                initial: ApplicationObservation | None,
                                require_trusted_location: bool,
                                manually_resolved: frozenset[str]) -> ControllerResult:
        """Resolve one observed page and allow at most one safe forward transition."""
        completed: dict[tuple[str, ...], tuple[str, AnswerSource, str]] = {}
        declined: dict[tuple[str, ...], str] = {}
        revealed: set[tuple[str, ...]] = set()
        searched: set[tuple[str, ...]] = set()
        limit_reached = False
        stage = "observe_snapshot"
        self.active_stage = stage
        try:
            session.record_observation(initial if initial is not None else await self.browser.observe())
            for cycle in range(self.limits.max_cycles):
                observation = session.current_observation
                assert observation is not None
                if require_trusted_location and not _trusted_location(observation.location):
                    return ControllerResult(ControllerStop.FAILED, session, "insecure_page")
                if cycle >= min(self.limits.max_actions, self.limits.max_same_step_actions) and cycle:
                    limit_reached = True
                    break
                changed = False
                for question in observation.questions:
                    identity = question.identity()
                    if question.report_identity() in manually_resolved:
                        continue
                    if identity in completed:
                        continue
                    if _has_answer(question):
                        continue
                    if identity in declined:
                        continue
                    if (question.control_type is ControlType.TYPEAHEAD and not question.options and
                            identity not in searched and
                            (question.target_ref or hasattr(self.browser, "act_on_dom_choice"))):
                        if question.current_value:
                            declined[identity] = "selection_unverified"
                            continue
                        resolution = await self.resolver.resolve(question, session.application_id)
                        if (resolution.status is ResolutionStatus.SAFE_TO_FILL and resolution.answer is not None and
                                not _has_answer(question)):
                            searched.add(identity)
                            if question.target_ref:
                                stage = "select_option_search"
                                self.active_stage = stage
                                action = SearchOptions(question.target_ref, observation.observation_id,
                                                       resolution.answer)
                                ActionPolicy.authorize(action, session)
                                result = await self.browser.search_options(action, session)
                            else:
                                try:
                                    stage = "select_option_search"
                                    self.active_stage = stage
                                    result = await self.browser.act_on_dom_choice(
                                        question, observation.observation_id, "search",
                                        resolution.answer.value, session)
                                except PermissionError:
                                    declined[identity] = "missing_or_ambiguous_target"
                                    continue
                            self._record_action(session, "search_options", resolution.answer.semantic_key,
                                                observation, result)
                            stage = "verify"
                            self.active_stage = stage
                            session.record_observation(result.observation)
                            if (result.outcome.status in {ActionStatus.FAILED, ActionStatus.TARGET_MISSING,
                                                          ActionStatus.AMBIGUOUS_TARGET} or
                                    not any(q.identity() == identity and q.options
                                            for q in result.observation.questions)):
                                declined[identity] = "live_options_unavailable"
                            changed = True
                            break
                    if (question.control_type is ControlType.CHOICE and not question.options and
                            identity not in revealed and
                            (question.target_ref and hasattr(self.browser, "reveal_options") or
                             not question.target_ref and hasattr(self.browser, "act_on_dom_choice"))):
                        if question.raw_role in {"radio", "group"}:
                            declined[identity] = "live_options_unavailable"
                            continue
                        probe = getattr(self.resolver, "resolve_closed_choice", None)
                        candidate = (await probe(question, session.application_id)) if probe else None
                        if (candidate is None or candidate.status is not ResolutionStatus.SAFE_TO_FILL or
                                candidate.answer is None):
                            declined[identity] = "no_safe_answer"
                            continue
                        revealed.add(identity)
                        if question.target_ref:
                            stage = "locate_control"
                            self.active_stage = stage
                            action = RevealOptions(question.target_ref, observation.observation_id)
                            ActionPolicy.authorize(action, session)
                            result = await self.browser.reveal_options(action, session)
                        else:
                            try:
                                stage = "locate_control"
                                self.active_stage = stage
                                result = await self.browser.act_on_dom_choice(
                                    question, observation.observation_id, "open", None, session)
                            except PermissionError:
                                declined[identity] = "missing_or_ambiguous_target"
                                continue
                        self._record_action(session, "reveal_options", None, observation, result)
                        stage = "verify"
                        self.active_stage = stage
                        session.record_observation(result.observation)
                        if (step_signature(observation) != step_signature(result.observation) or
                                not any(q.identity() == identity and q.options
                                        for q in result.observation.questions)):
                            declined[identity] = "live_options_unavailable"
                        changed = True
                        break
                    resolution = await self.resolver.resolve(question, session.application_id)
                    session.resolution_history.append(ResolutionTrace(
                        observation.observation_id, resolution.canonical.semantic_key,
                        resolution.mapping_source,
                        resolution.answer.source if resolution.answer else None,
                        resolution.status.value))
                    if resolution.status is not ResolutionStatus.SAFE_TO_FILL or resolution.answer is None:
                        continue
                    answer = resolution.answer
                    if answer.semantic_key in {"why_this_company", "why_this_role"} or answer.semantic_key.startswith("eeo_"):
                        continue
                    if _has_answer(question):
                        continue
                    if question.control_type not in {ControlType.TEXT, ControlType.DATE, ControlType.CHOICE,
                                                     ControlType.TYPEAHEAD, ControlType.TOGGLE,
                                                     ControlType.FILE}:
                        continue
                    if question.control_type is ControlType.TYPEAHEAD and not question.options:
                        continue
                    if question.control_type is ControlType.DATE:
                        pattern = {"date": r"\d{4}-\d{2}-\d{2}",
                                   "month": r"\d{4}-\d{2}",
                                   "MM/YYYY": r"(?:0[1-9]|1[0-2])/\d{4}"}.get(question.date_format)
                        if pattern is None or re.fullmatch(pattern, answer.value) is None:
                            declined[identity] = "date_format_unavailable"
                            continue
                    if (question.control_type is ControlType.TOGGLE and
                            answer.value.casefold() not in {"yes", "true", "checked",
                                                            "no", "false", "unchecked"}):
                        continue
                    if (question.control_type is ControlType.TOGGLE and
                            question.current_value == "unchecked" and
                            answer.value.casefold() in {"no", "false", "unchecked"}):
                        completed[identity] = ("confirmed_trusted", answer.source,
                                               answer.semantic_key)
                        continue
                    if question.control_type is ControlType.FILE:
                        path = Path(answer.value).expanduser()
                        if (answer.semantic_key != "documents.resume" or
                                not path.is_absolute() or not path.is_file() or
                                path.suffix.casefold() not in {".pdf", ".doc", ".docx"} or
                                not hasattr(self.browser, "upload_document")):
                            declined[identity] = "known_document_unavailable"
                            continue
                    dom_choice = (not question.target_ref and
                                  question.control_type in {ControlType.CHOICE, ControlType.TYPEAHEAD} and
                                  hasattr(self.browser, "act_on_dom_choice"))
                    if ((not question.target_ref and not dom_choice) or
                            (question.target_ref and sum(q.target_ref == question.target_ref
                                                         for q in observation.questions) != 1) or
                            sum(q.identity() == identity for q in observation.questions) != 1):
                        declined[identity] = "missing_or_ambiguous_target"
                        continue
                    action = (None if dom_choice else
                              FillText(question.target_ref, observation.observation_id, answer)
                              if question.control_type in {ControlType.TEXT, ControlType.DATE} else
                              ChooseOption(question.target_ref, observation.observation_id, answer)
                              if question.control_type in {ControlType.CHOICE, ControlType.TYPEAHEAD} else
                              Toggle(question.target_ref, observation.observation_id, answer)
                              if question.control_type is ControlType.TOGGLE else
                              UploadDocument(question.target_ref, observation.observation_id,
                                             "documents.resume", answer))
                    if action is not None:
                        ActionPolicy.authorize(action, session)
                    if dom_choice:
                        try:
                            stage = "select_option"
                            self.active_stage = stage
                            result = await self.browser.act_on_dom_choice(
                                question, observation.observation_id, "choose", answer.value, session)
                        except PermissionError:
                            declined[identity] = "missing_or_ambiguous_target"
                            continue
                    else:
                        stage = ("fill" if isinstance(action, FillText) else
                                 "select_option" if isinstance(action, ChooseOption) else
                                 "toggle" if isinstance(action, Toggle) else "upload")
                        self.active_stage = stage
                        result = (
                              await self.browser.fill_text(action, session)
                              if isinstance(action, FillText) else
                              await self.browser.choose_option(action, session)
                              if isinstance(action, ChooseOption) else
                              await self.browser.toggle(action, session)
                              if isinstance(action, Toggle) else
                              await self.browser.upload_document(action, session))
                    self._record_action(session, question.control_type.value, answer.semantic_key,
                                        observation, result)
                    stage = "verify"
                    self.active_stage = stage
                    session.record_observation(result.observation)
                    fresh = result.observation
                    if step_signature(observation) != step_signature(fresh):
                        declined[identity] = "unable_to_freshly_verify"
                        break
                    matches = [q for q in fresh.questions if q.identity() == identity]
                    if (result.outcome.status in {ActionStatus.FAILED, ActionStatus.TARGET_MISSING,
                                                  ActionStatus.AMBIGUOUS_TARGET} or
                            len(matches) != 1 or
                            not (matches[0].answer_state().satisfied or
                                 isinstance(action, Toggle) and
                                 matches[0].current_value == "unchecked" and
                                 answer.value.casefold() in {"no", "false", "unchecked"}) or
                            (matches[0].current_value != (
                                "checked" if answer.value.casefold() in {"yes", "true", "checked"}
                                else "unchecked") if isinstance(action, Toggle)
                             else Path(answer.value).name.casefold() != _norm(matches[0].current_value)
                             if isinstance(action, UploadDocument) else
                             _norm(matches[0].current_value) != _norm(answer.value))):
                        declined[identity] = "unable_to_freshly_verify"
                    else:
                        session.record_answer(question, answer)
                        self.resolver.record_answer(answer)
                        completed[identity] = ("filled_text" if isinstance(action, FillText)
                                               else "selected_option" if dom_choice or isinstance(action, ChooseOption)
                                               else "selected_toggle" if isinstance(action, Toggle)
                                               else "uploaded_document", answer.source, answer.semantic_key)
                    changed = True
                    break
                if not changed:
                    break
            else:
                limit_reached = True

            observation = session.current_observation
            assert observation is not None
            if require_trusted_location and not _trusted_location(observation.location):
                return ControllerResult(ControllerStop.FAILED, session, "insecure_page")
            fields: list[CurrentPageField] = []
            for question in observation.questions:
                identity = question.identity()
                if question.report_identity() in manually_resolved:
                    fields.append(CurrentPageField(question, question.semantic_key,
                                                   "human_resolved"))
                    continue
                resolution = await self.resolver.resolve(question, session.application_id)
                key = resolution.canonical.semantic_key
                answer = resolution.answer if resolution.status is ResolutionStatus.SAFE_TO_FILL else None
                reason = declined.get(identity)
                action = "deferred"
                source = None
                if (answer is not None and (_has_answer(question) or
                        (identity in completed and question.control_type is ControlType.TOGGLE and
                         question.current_value == "unchecked")) and
                        question.control_type in {ControlType.TEXT, ControlType.DATE, ControlType.CHOICE,
                                                  ControlType.TYPEAHEAD, ControlType.TOGGLE,
                                                  ControlType.FILE} and
                        answer.semantic_key not in {"why_this_company", "why_this_role"} and
                        not answer.semantic_key.startswith("eeo_")):
                    if (reason != "unable_to_freshly_verify" and question.current_value == (
                            "checked" if answer.value.casefold() in {"yes", "true", "checked"}
                            else "unchecked") if question.control_type is ControlType.TOGGLE
                            else Path(answer.value).name.casefold() == _norm(question.current_value)
                            if question.control_type is ControlType.FILE else
                            _norm(question.current_value) == _norm(answer.value)):
                        session.record_answer(question, answer)
                        previous = completed.get(identity)
                        action = previous[0] if previous else "confirmed_trusted"
                        source = answer.source
                    elif _has_answer(question) and reason != "unable_to_freshly_verify":
                        # A fresh committed value may be a human choice that
                        # differs from the candidate profile. Do not replace
                        # it or keep an older unresolved blocker alive.
                        action = "manual_complete"
                        reason = None
                    else:
                        reason = "unable_to_freshly_verify"
                elif _has_answer(question) and reason != "unable_to_freshly_verify":
                    # A human-entered value is complete for this page, but has no
                    # trusted automated provenance and receives no GREEN marker.
                    action = "manual_complete"
                    reason = None
                elif reason is None and question.control_type in {
                        ControlType.FILE, ControlType.SECRET, ControlType.UNKNOWN}:
                    reason = "unsupported_control"
                elif reason is None:
                    if limit_reached and answer is not None:
                        reason = "action_limit_reached"
                    elif key in {"why_this_company", "why_this_role"}:
                        reason = "narrative_deferred"
                    elif key and key.startswith("eeo_"):
                        reason = "requires_human_review"
                    elif answer is not None and not question.target_ref:
                        reason = "missing_or_ambiguous_target"
                    elif resolution.status is ResolutionStatus.REQUIRES_REVIEW:
                        reason = "requires_human_review"
                    elif resolution.reason == "value does not unambiguously match options":
                        reason = "ambiguous_option_mapping"
                    elif resolution.reason == "live options unavailable":
                        reason = "live_options_unavailable"
                    elif resolution.canonical.status is CanonicalStatus.AMBIGUOUS:
                        reason = "ambiguous_semantic_mapping"
                    elif question.control_type is ControlType.TOGGLE:
                        reason = "no_safe_answer"
                    elif question.required is False:
                        action = "optional_skipped"
                    else:
                        reason = "no_safe_answer"
                if action == "deferred" and reason is None:
                    reason = "unaccounted_field"
                if reason is not None:
                    session.mark_unresolved(question)
                fields.append(CurrentPageField(question, key, action, source, reason))

            items = tuple(fields)
            if limit_reached:
                return ControllerResult(ControllerStop.ACTION_LIMIT_REACHED, session,
                                        "current-page action limit", items)
            if any(field.needs_review and field.question.required is True for field in items):
                return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                        "required current-page question requires human review", items)
            controls = observation.navigation_controls
            submit = [control for control in controls if control.kind is NavigationKind.SUBMIT]
            forward = [control for control in controls if control.kind is NavigationKind.ADVANCE]
            if submit:
                if len(submit) != 1 or forward or any(
                        control.kind is NavigationKind.UNKNOWN for control in controls):
                    return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                            "terminal navigation is ambiguous", items)
                return ControllerResult(ControllerStop.READY_FOR_REVIEW, session,
                                        "terminal page awaits human submission", items)
            if len(forward) == 1 and forward[0].target_ref:
                # Unknown buttons and section actions cannot authorize navigation.
                # The typed Advance policy also forbids any final submission.
                action = Advance(forward[0].target_ref, observation.observation_id)
                ActionPolicy.authorize(action, session)
                stage = "page_advance"
                self.active_stage = stage
                result = await self.browser.activate_navigation(action, session)
                self._record_action(session, "advance", None, observation, result)
                stage = "verify"
                self.active_stage = stage
                session.record_observation(result.observation)
                classification = classify_advance(observation, result.observation,
                                                  result.outcome.status)
                if classification in {AdvanceClassification.PROGRESSED,
                                      AdvanceClassification.NEW_QUESTIONS}:
                    return ControllerResult(ControllerStop.PAGE_ADVANCED, session,
                                            "fresh page observation acquired", items)
                if classification is AdvanceClassification.VALIDATION_BLOCKED:
                    fresh_fields = {field.question.identity(): field for field in items}
                    for question in result.observation.questions:
                        if question.required is True and not _has_answer(question):
                            fresh_fields[question.identity()] = CurrentPageField(
                                question, question.semantic_key, "deferred",
                                reason="site_validation_required")
                    return ControllerResult(ControllerStop.VALIDATION_BLOCKED, session,
                                            "site validation blocked progression",
                                            tuple(fresh_fields.values()))
                return ControllerResult(ControllerStop.STOPPED_BEFORE_ADVANCE, session,
                                        "forward action did not establish a new page", items)
            return ControllerResult(ControllerStop.STOPPED_BEFORE_ADVANCE, session,
                                    "terminal or navigation state is ambiguous", items)
        except Exception as exc:
            retained: list[CurrentPageField] = []
            observation = session.current_observation
            if observation is not None:
                for question in observation.questions:
                    prior = completed.get(question.identity())
                    if prior:
                        retained.append(CurrentPageField(question, prior[2], prior[0], prior[1]))
                    elif question.report_identity() in manually_resolved:
                        retained.append(CurrentPageField(question, question.semantic_key, "human_resolved"))
                    elif _has_answer(question):
                        retained.append(CurrentPageField(question, question.semantic_key, "manual_complete"))
                    elif question.required is False:
                        retained.append(CurrentPageField(question, question.semantic_key, "optional_skipped"))
                    else:
                        try:
                            resolution = await self.resolver.resolve(question, session.application_id)
                            reason = ("no_safe_answer" if resolution.status is not ResolutionStatus.SAFE_TO_FILL
                                      else "interrupted_before_completion")
                        except Exception:
                            reason = "interrupted_before_completion"
                        retained.append(CurrentPageField(question, question.semantic_key,
                                                         "deferred", reason=reason))
            session.fail()
            return ControllerResult(ControllerStop.FAILED, session, "current-page browser or policy failure",
                                    tuple(retained), stage, safe_failure(stage, exc))
