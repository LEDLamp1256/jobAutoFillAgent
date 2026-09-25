"""Bounded sequential application loop; never exposes a submit operation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jobagent.browser import BrowserPort, BrowserActionResult
from jobagent.domain import (
    ActionPolicy, ActionStatus, Advance, ApplicationObservation, ApplicationOutcome,
    ApplicationSession, ChooseOption, ControlType, FillText, NavigationKind,
    QuestionObservation, SessionActionRecord, StepTransition, semantic_fingerprint,
)
from jobagent.resolution import DeterministicAnswerResolver, ResolutionStatus


def _norm(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


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
    if new_questions - old_questions:
        return AdvanceClassification.NEW_QUESTIONS
    if after.validation_messages:
        return AdvanceClassification.VALIDATION_BLOCKED
    return AdvanceClassification.NO_PROGRESS


class ControllerStop(str, Enum):
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


_ADVANCE_LABELS = {"next", "continue", "save and continue", "save & continue"}


class ApplicationController:
    """One application, one browser, one mutation at a time."""

    def __init__(self, browser: BrowserPort, resolver: DeterministicAnswerResolver,
                 limits: ControllerLimits | None = None):
        self.browser = browser
        self.resolver = resolver
        self.limits = limits or ControllerLimits()

    async def run(self, session: ApplicationSession) -> ControllerResult:
        if session.current_observation is not None:
            raise ValueError("V2-4 controller starts with a new in-memory session")
        actions = same_step_actions = transitions = 0
        advance_needs_repair = False
        try:
            session.record_observation(await self.browser.navigate(session.job_url))
            for _cycle in range(self.limits.max_cycles):
                observation = session.current_observation
                assert observation is not None
                if observation.review_like:
                    return ControllerResult(ControllerStop.READY_FOR_REVIEW, session)
                if any(c.kind is NavigationKind.SUBMIT for c in observation.navigation_controls):
                    return ControllerResult(ControllerStop.NEEDS_REVIEW, session,
                                            "terminal control is visible outside recognized review")

                mutated = False
                pending: list[QuestionObservation] = []
                for question in observation.questions:
                    resolution = self.resolver.resolve(question, session.application_id)
                    if resolution.status is not ResolutionStatus.SAFE_TO_FILL or resolution.answer is None:
                        session.mark_unresolved(question)
                        if question.required is not False:
                            pending.append(question)
                        continue
                    answer = resolution.answer
                    if question.current_value:
                        if _norm(question.current_value) == _norm(answer.value):
                            session.record_answer(question, answer)
                        else:
                            session.mark_unresolved(question)
                            pending.append(question)
                        continue
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
                    self.resolver.ledger.record(answer)
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
