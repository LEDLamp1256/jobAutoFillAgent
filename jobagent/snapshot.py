"""Normalizer for Playwright MCP 0.0.82 text accessibility snapshots.

This deliberately covers the local fixture's roles, not arbitrary ATS markup.
The input is untrusted page data and is never interpreted as instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping

from jobagent.domain import (
    ApplicationObservation, ControlType, NavigationControl, NavigationKind,
    QuestionObservation,
)


class SnapshotFormatError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedSnapshot:
    observation: ApplicationObservation
    option_targets: Mapping[tuple[str, str], str]


_URL = re.compile(r"^- Page URL: (.+)$", re.MULTILINE)
_YAML = re.compile(r"### Snapshot\s*```yaml\n(.*?)\n```", re.DOTALL)
_HEADING = re.compile(r'^\s*- heading ("(?:\\.|[^"\\])*") \[level=(\d+)\] \[ref=(e\d+)\]')
_PARAGRAPH = re.compile(r'^\s*- paragraph \[ref=e\d+\]: (.+)$')
_TEXTBOX = re.compile(r'^\s*- textbox ("(?:\\.|[^"\\])*")(.*?)\[ref=(e\d+)\](?:: (.*))?$')
_GROUP = re.compile(r'^(\s*)- group ("(?:\\.|[^"\\])*") \[ref=(e\d+)\]')
_RADIO = re.compile(r'^\s*- radio ("(?:\\.|[^"\\])*")(.*?)\[ref=(e\d+)\]')
_BUTTON = re.compile(r'^\s*- button ("(?:\\.|[^"\\])*")(.*?)\[ref=(e\d+)\]')
_ALERT = re.compile(r'^\s*- alert \[ref=e\d+\]: (.+)$')


def _scalar(text: str) -> str:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _nav_kind(label: str, explicit: Mapping[str, NavigationKind]) -> NavigationKind:
    normalized = " ".join(label.casefold().split())
    if (normalized.startswith("submit") or normalized in
            {"apply", "apply now", "send application", "finish application", "complete application"}):
        return NavigationKind.SUBMIT
    if normalized in {"next", "continue", "save and continue", "save & continue"}:
        return explicit.get(normalized, NavigationKind.UNKNOWN)
    if normalized == "back":
        return explicit.get(normalized, NavigationKind.UNKNOWN)
    return NavigationKind.UNKNOWN


class SnapshotNormalizer:
    def __init__(self, semantic_aliases: Mapping[str, str] | None = None,
                 navigation_kinds: Mapping[str, NavigationKind] | None = None):
        self._aliases = {key.casefold(): value for key, value in (semantic_aliases or {}).items()}
        self._navigation = {" ".join(key.casefold().split()): value
                            for key, value in (navigation_kinds or {}).items()}
        if any(_nav_kind(label, {}) is NavigationKind.SUBMIT and kind is not NavigationKind.SUBMIT
               for label, kind in self._navigation.items()):
            raise ValueError("submit-like controls cannot be reclassified as ordinary navigation")

    def normalize(self, snapshot_text: str, observation_id: str) -> NormalizedSnapshot:
        if len(snapshot_text) > 200_000:
            raise SnapshotFormatError("snapshot exceeds bounded parser input")
        url_match = _URL.search(snapshot_text)
        yaml_match = _YAML.search(snapshot_text)
        if not url_match or not yaml_match:
            raise SnapshotFormatError("expected Playwright MCP page URL and YAML snapshot block")

        headings: dict[int, str] = {}
        progress: str | None = None
        questions: list[QuestionObservation] = []
        controls: list[NavigationControl] = []
        validation: list[str] = []
        option_targets: dict[tuple[str, str], str] = {}
        group_label: str | None = None
        group_ref: str | None = None
        group_indent = -1
        group_options: list[str] = []
        checked: str | None = None

        def finish_group() -> None:
            nonlocal group_label, group_ref, group_options, checked
            if group_label is not None and group_ref is not None:
                questions.append(QuestionObservation(
                    label=group_label, control_type=ControlType.CHOICE,
                    semantic_key=self._aliases.get(group_label.casefold()),
                    section=headings.get(2), options=tuple(group_options),
                    current_value=checked, target_ref=group_ref,
                ))
            group_label = group_ref = checked = None
            group_options = []

        for line in yaml_match.group(1).splitlines():
            indent = len(line) - len(line.lstrip())
            if group_label is not None and indent <= group_indent and line.lstrip().startswith("- "):
                finish_group()
            if match := _HEADING.match(line):
                headings[int(match.group(2))] = _scalar(match.group(1))
            elif match := _PARAGRAPH.match(line):
                value = _scalar(match.group(1))
                if value.casefold().startswith("step "):
                    progress = value
            elif match := _GROUP.match(line):
                finish_group()
                group_indent = len(match.group(1))
                group_label, group_ref = _scalar(match.group(2)), match.group(3)
            elif match := _RADIO.match(line):
                if group_ref is not None:
                    option = _scalar(match.group(1))
                    group_options.append(option)
                    option_targets[(group_ref, option)] = match.group(3)
                    if "[checked]" in match.group(2):
                        checked = option
            elif match := _TEXTBOX.match(line):
                label = _scalar(match.group(1))
                questions.append(QuestionObservation(
                    label=label, control_type=ControlType.TEXT,
                    semantic_key=self._aliases.get(label.casefold()),
                    section=headings.get(2), current_value=_scalar(match.group(4)) if match.group(4) else None,
                    required="[required]" in match.group(2), target_ref=match.group(3),
                ))
            elif match := _BUTTON.match(line):
                label = _scalar(match.group(1))
                controls.append(NavigationControl(label, _nav_kind(label, self._navigation), match.group(3)))
            elif match := _ALERT.match(line):
                validation.append(_scalar(match.group(1)))
        finish_group()

        heading = headings.get(2) or headings.get(1)
        if not heading:
            raise SnapshotFormatError("snapshot has no visible heading")
        if "review" in heading.casefold():
            controls = [NavigationControl(c.label, NavigationKind.UNKNOWN, c.target_ref)
                        if c.kind is NavigationKind.ADVANCE else c for c in controls]
        for label in {c.label.casefold() for c in controls if c.kind is NavigationKind.ADVANCE}:
            if sum(c.label.casefold() == label for c in controls) > 1:
                controls = [NavigationControl(c.label, NavigationKind.UNKNOWN, c.target_ref)
                            if c.label.casefold() == label else c for c in controls]
        review_like = "review" in heading.casefold() and any(
            control.kind is NavigationKind.SUBMIT for control in controls
        )
        return NormalizedSnapshot(ApplicationObservation(
            observation_id=observation_id, location=url_match.group(1).strip(),
            heading=heading, progress_text=progress, questions=tuple(questions),
            validation_messages=tuple(validation), navigation_controls=tuple(controls),
            review_like=review_like,
        ), option_targets)
