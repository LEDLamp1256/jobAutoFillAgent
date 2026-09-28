"""Normalizer for Playwright MCP 0.0.82 text accessibility snapshots.

This deliberately covers the local fixture's roles, not arbitrary ATS markup.
The input is untrusted page data and is never interpreted as instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from jobagent.domain import (
    ApplicationObservation, ControlType, NavigationControl, NavigationKind,
    QuestionObservation,
)


class SnapshotFormatError(ValueError):
    def __init__(self, reason: str, diagnostic: SnapshotDiagnostic | None = None):
        super().__init__(reason)
        self.diagnostic = diagnostic


class SnapshotEmpty(SnapshotFormatError):
    """The page has not exposed accessibility content yet."""


class SnapshotAccessChallenge(SnapshotFormatError):
    """The site returned an access challenge rather than an application."""


@dataclass(frozen=True)
class NormalizedSnapshot:
    observation: ApplicationObservation
    option_targets: Mapping[tuple[str, str], str]
    diagnostic: SnapshotDiagnostic | None = None


@dataclass(frozen=True)
class HeadingPredicate:
    quoted_name: bool
    level: int | None
    active: bool
    ref_present: bool
    ref_shape: str
    matcher_accepts: bool


@dataclass(frozen=True)
class ControlPredicate:
    role: str
    label: str | None
    indent: int
    ref_shape: str
    flags: tuple[str, ...]
    line_matcher_accepts: bool
    rejection_reason: str | None


@dataclass(frozen=True)
class SnapshotDiagnostic:
    """Bounded structure from an untrusted snapshot; never includes field values."""

    location_pattern: str | None
    snapshot_present: bool
    line_count: int
    roles: tuple[tuple[str, int], ...]
    structures: tuple[tuple[int, str, str | None, tuple[str, ...]], ...]
    heading_shapes: tuple[str, ...]
    heading_predicates: tuple[HeadingPredicate, ...]
    control_predicates: tuple[ControlPredicate, ...]
    truncated: bool


_DIAGNOSTIC_INPUT_LIMIT = 200_000
_DIAGNOSTIC_LINES_LIMIT = 2_000
_DIAGNOSTIC_STRUCTURES_LIMIT = 48
_DIAGNOSTIC_CONTROLS_LIMIT = 48
_ROLE_LINE = re.compile(r'^([ \t]*)- ([a-z][a-z0-9_-]{0,31})\b(.*)$')
_QUOTED_NAME = re.compile(r'"(?:\\.|[^"\\])*"')
_EMAIL_VALUE = re.compile(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b')
_PHONE_VALUE = re.compile(r'(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)')
_LONG_ID = re.compile(r'\b[0-9a-f]{12,}\b|\b\d{5,}\b', re.I)
_URL_VALUE = re.compile(r'https?://\S+', re.I)
_SENSITIVE_WORD = re.compile(r'password|passphrase|secret|token|cookie|authorization', re.I)
_ATTRIBUTE = re.compile(r'\[([a-z][a-z_-]*)(?:=([^\]]+))?\]', re.I)


def _diagnostic_name(raw: str | None) -> str | None:
    if not raw:
        return None
    value = _scalar(raw)[:240]
    value = _EMAIL_VALUE.sub('[redacted-email]', value)
    value = _PHONE_VALUE.sub('[redacted-phone]', value)
    value = _LONG_ID.sub('[redacted-id]', value)
    value = _URL_VALUE.sub('[redacted-url]', value)
    if _SENSITIVE_WORD.search(value) and ':' in value:
        return '[sensitive-label]'
    return value[:100] or None


def _control_predicate(line: str, indent: int, role: str, remainder: str) -> ControlPredicate:
    matchers = {'textbox': _TEXTBOX, 'button': _BUTTON, 'group': _GROUP, 'radio': _RADIO}
    attributes = {attr.group(1): attr.group(2) for attr in _ATTRIBUTE.finditer(remainder)}
    ref = attributes.get('ref')
    ref_shape = ('e#' if re.fullmatch(r'e\d+', ref or '') else
                 'opaque_token' if re.fullmatch(r'[^\]\s]{1,128}', ref or '') else
                 'invalid' if 'ref' in attributes else 'missing')
    matched = role in matchers and matchers[role].match(line) is not None
    ref_attribute = next((attr for attr in _ATTRIBUTE.finditer(line)
                          if attr.group(1) == 'ref'), None)
    with_legacy_ref = (line[:ref_attribute.start()] + '[ref=e1]' + line[ref_attribute.end():]
                       if ref_attribute is not None else line)
    ref_format_only = (ref_shape == 'opaque_token' and role in matchers and
                       matchers[role].match(with_legacy_ref) is not None)
    reason = (None if matched else
              'unsupported_role' if role not in matchers else
              'reference_format' if ref_format_only else
              'missing_or_invalid_reference' if ref_shape in {'missing', 'invalid'} else
              'line_syntax_or_attribute_order')
    quoted = _QUOTED_NAME.match(remainder.lstrip())
    flags = tuple(flag for flag in ('required', 'disabled', 'checked', 'selected')
                  if f'[{flag}]' in line)
    return ControlPredicate(role, _diagnostic_name(quoted.group(0)) if quoted else None,
                            min(indent, 40), ref_shape, flags, matched, reason)


def snapshot_diagnostic(snapshot_text: str) -> SnapshotDiagnostic:
    """Read roles and accessible names only; never copy trailing control values."""
    bounded = snapshot_text[:_DIAGNOSTIC_INPUT_LIMIT]
    url_match = _URL.search(bounded)
    location = None
    if url_match:
        parsed = urlsplit(url_match.group(1).strip())
        path = re.sub(r'\b[0-9a-f]{8,}\b|\b\d{4,}\b', ':id', parsed.path, flags=re.I)
        location = f'{parsed.hostname or "?"}{path[:120]}'
    yaml_match = _YAML.search(bounded)
    if not yaml_match:
        return SnapshotDiagnostic(location, False, 0, (), (), (), (), (),
                                  len(snapshot_text) > len(bounded))
    lines = yaml_match.group(1).splitlines()
    counts: dict[str, int] = {}
    structures: list[tuple[int, str, str | None, tuple[str, ...]]] = []
    heading_shapes: list[str] = []
    heading_predicates: list[HeadingPredicate] = []
    control_predicates: list[ControlPredicate] = []
    retain = {'heading', 'button', 'link', 'textbox', 'combobox', 'listbox',
              'option', 'radio', 'checkbox', 'group', 'tab', 'listitem', 'alert'}
    for line in lines[:_DIAGNOSTIC_LINES_LIMIT]:
        match = _ROLE_LINE.match(line)
        if not match:
            continue
        indent, role, remainder = match.groups()
        if role in counts or len(counts) < 32:
            counts[role] = counts.get(role, 0) + 1
        if (role in {'textbox', 'button', 'group', 'radio', 'combobox', 'listbox', 'checkbox'}
                and len(control_predicates) < _DIAGNOSTIC_CONTROLS_LIMIT):
            control_predicates.append(_control_predicate(line, len(indent), role, remainder))
        if role == 'heading' and len(heading_shapes) < 8:
            # Preserve syntax and attribute order without preserving heading text or refs.
            quoted_shape = _QUOTED_NAME.search(remainder)
            shape = (remainder[:quoted_shape.start()] + '"<name>"' +
                     remainder[quoted_shape.end():]) if quoted_shape else remainder
            if not quoted_shape and ':' in shape:
                shape = shape.split(':', 1)[0] + ': <name>'
            def safe_attribute(attr: re.Match[str]) -> str:
                key, value = attr.group(1), attr.group(2)
                if key == 'level' and (value or '').isdigit():
                    return f'[level={value}]'
                if key == 'ref':
                    return '[ref=e#]' if re.fullmatch(r'e\d+', value or '') else '[ref=other]'
                return f'[{key}]'

            shape = _ATTRIBUTE.sub(safe_attribute, shape)
            heading_shapes.append(('- heading' + shape)[:120])
            attributes = {attr.group(1): attr.group(2) for attr in _ATTRIBUTE.finditer(remainder)}
            level_value = attributes.get('level')
            ref_value = attributes.get('ref')
            heading_predicates.append(HeadingPredicate(
                quoted_name=bool(re.match(r'^\s+"(?:\\.|[^"\\])*"', remainder)),
                level=int(level_value) if level_value and level_value.isdigit() else None,
                active='active' in attributes,
                ref_present='ref' in attributes,
                ref_shape=('e#' if re.fullmatch(r'e\d+', ref_value or '') else
                           'other' if 'ref' in attributes else 'missing'),
                matcher_accepts=_HEADING.match(line) is not None,
            ))
        if role not in retain or len(structures) >= _DIAGNOSTIC_STRUCTURES_LIMIT:
            continue
        # A value after [ref=...] or other attributes is deliberately ignored.
        quoted = _QUOTED_NAME.search(remainder.split('[ref=', 1)[0])
        name = _diagnostic_name(quoted.group(0)) if quoted else None
        if name is None and role in {'heading', 'button', 'link', 'tab', 'listitem', 'group'} and remainder.startswith(': '):
            name = _diagnostic_name(remainder[2:].split(' [', 1)[0])
        flags = tuple(flag for flag in ('required', 'disabled', 'checked', 'selected', 'expanded')
                      if f'[{flag}]' in line)
        structures.append((min(len(indent), 40), role, name, flags))
    return SnapshotDiagnostic(location, True, len(lines), tuple(sorted(counts.items())),
                              tuple(structures), tuple(heading_shapes), tuple(heading_predicates),
                              tuple(control_predicates),
                              len(snapshot_text) > len(bounded) or
                              len(lines) > _DIAGNOSTIC_LINES_LIMIT or
                              sum(counts.get(role, 0) for role in
                                  {'textbox', 'button', 'group', 'radio', 'combobox', 'listbox', 'checkbox'})
                              > len(control_predicates) or
                              sum(counts.get(role, 0) for role in retain) > len(structures))


_URL = re.compile(r"^- Page URL: (.+)$", re.MULTILINE)
_YAML = re.compile(r"### Snapshot\s*```yaml\n(.*?)\n```", re.DOTALL)
_HEADING = re.compile(r'^\s*- heading ("(?:\\.|[^"\\])*") \[level=(\d+)\] \[ref=([^\]\s]{1,128})\]$')
_PARAGRAPH = re.compile(r'^\s*- paragraph \[ref=e\d+\]: (.+)$')
_TEXTBOX = re.compile(r'^\s*- textbox ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\](?:: (.*))?$')
_GROUP = re.compile(r'^(\s*)- group ("(?:\\.|[^"\\])*") \[ref=(e\d+)\]')
_RADIO = re.compile(r'^\s*- radio ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
_BUTTON = re.compile(r'^\s*- button ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
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
        diagnostic = snapshot_diagnostic(snapshot_text)
        if len(snapshot_text) > 200_000:
            raise SnapshotFormatError("snapshot exceeds bounded parser input", diagnostic)
        if re.search(r"^- HTTP status: 429\s*$", snapshot_text, re.MULTILINE):
            raise SnapshotAccessChallenge("site returned HTTP 429 access challenge")
        if re.search(r"recaptcha|captcha|verify you are human|unusual traffic|i'm not a robot",
                     snapshot_text, re.IGNORECASE):
            raise SnapshotAccessChallenge("site presented a human access challenge")
        url_match = _URL.search(snapshot_text)
        yaml_match = _YAML.search(snapshot_text)
        if not url_match or not yaml_match:
            raise SnapshotFormatError("expected Playwright MCP page URL and YAML snapshot block", diagnostic)
        if not yaml_match.group(1).strip():
            raise SnapshotEmpty("accessibility snapshot is empty", diagnostic)

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
        group_required = False

        def finish_group() -> None:
            nonlocal group_label, group_ref, group_options, checked, group_required
            if group_label is not None and group_ref is not None:
                questions.append(QuestionObservation(
                    label=group_label, control_type=ControlType.CHOICE,
                    semantic_key=self._aliases.get(group_label.casefold()),
                    section=headings.get(2), options=tuple(group_options),
                    current_value=checked, required=True if group_required else None,
                    target_ref=group_ref,
                ))
            group_label = group_ref = checked = None
            group_options = []
            group_required = False

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
                    group_required = group_required or "[required]" in match.group(2)
                    option = _scalar(match.group(1))
                    group_options.append(option)
                    option_targets[(group_ref, option)] = match.group(3)
                    if "[checked]" in match.group(2):
                        checked = option
            elif match := _TEXTBOX.match(line):
                label = _scalar(match.group(1))
                secret = bool(re.search(r"\b(password|passphrase)\b", label, re.IGNORECASE))
                questions.append(QuestionObservation(
                    label=label, control_type=ControlType.SECRET if secret else ControlType.TEXT,
                    semantic_key=self._aliases.get(label.casefold()),
                    section=headings.get(2),
                    current_value=None if secret else _scalar(match.group(4)) if match.group(4) else None,
                    required=True if "[required]" in match.group(2) else None,
                    target_ref=match.group(3),
                ))
            elif match := _BUTTON.match(line):
                label = _scalar(match.group(1))
                controls.append(NavigationControl(label, _nav_kind(label, self._navigation), match.group(3)))
            elif match := _ALERT.match(line):
                validation.append(_scalar(match.group(1)))
        finish_group()

        heading = headings.get(2) or headings.get(1)
        if not heading:
            raise SnapshotFormatError("snapshot has no visible heading", diagnostic)
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
        ), option_targets, diagnostic)
