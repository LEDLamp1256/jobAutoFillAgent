"""Normalizer for Playwright MCP 0.0.82 text accessibility snapshots.

This deliberately covers the local fixture's roles, not arbitrary ATS markup.
The input is untrusted page data and is never interpreted as instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Mapping
from urllib.parse import urlsplit

from jobagent.domain import (
    AnswerEvidence, ApplicationObservation, ControlType, NavigationControl, NavigationKind,
    QuestionObservation, SectionAction, is_selector_status_label,
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
_GROUP = re.compile(r'^(\s*)- group ("(?:\\.|[^"\\])*")(.*)$')
_RADIO = re.compile(r'^\s*- radio ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
_BUTTON = re.compile(r'^\s*- button ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
_COMBO = re.compile(r'^(\s*)- combobox ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\](?::(?: (.*))?)?$')
_LISTBOX = re.compile(r'^(\s*)- listbox ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\](?::(?: (.*))?)?$')
_OPTION = re.compile(r'^\s*- option ("(?:\\.|[^"\\])*")(.*?)(?:\[ref=([^\]\s]{1,128})\])?(?::.*)?$')
_CHECKBOX = re.compile(r'^\s*- checkbox ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
_SWITCH = re.compile(r'^\s*- switch ("(?:\\.|[^"\\])*")(.*?)\[ref=([^\]\s]{1,128})\]$')
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
    if (re.search(r"\bsubmit\b", normalized) or normalized in
            {"apply", "apply now", "send application", "finish application", "complete application"}):
        return NavigationKind.SUBMIT
    if normalized in {"next", "continue", "save and continue", "save & continue",
                      "continue to review", "save and continue to review"}:
        return explicit.get(normalized, NavigationKind.ADVANCE)
    if normalized == "back":
        return explicit.get(normalized, NavigationKind.UNKNOWN)
    return NavigationKind.UNKNOWN


def _required(attributes: str, label: str = "") -> bool | None:
    """Prefer explicit accessibility state over a visible required marker."""
    values = {match.group(1).casefold(): match.group(2)
              for match in _ATTRIBUTE.finditer(attributes)}
    if values.get("aria-required") in {"false", "true"}:
        return values["aria-required"] == "true"
    if "required" in values:
        return True
    if "optional" in values:
        return False
    return True if label.rstrip().endswith("*") else None


def _label(value: str) -> str:
    return _scalar(value).rstrip(" *").strip()


def _with_nearby_required(attributes: str, label: str, nearby: bool | None) -> bool | None:
    explicit = _required(attributes, label)
    return explicit if explicit is not None else nearby


def _required_evidence(attributes: str, label: str, nearby: bool | None) -> str | None:
    if "[aria-required=true]" in attributes.casefold():
        return "aria_required"
    if "[required]" in attributes.casefold():
        return "accessibility_required"
    if label.rstrip().endswith("*") or nearby is True:
        return "associated_required_marker"
    return None


def _placeholder(attributes: str) -> str | None:
    for attribute in _ATTRIBUTE.finditer(attributes):
        if attribute.group(1).casefold() in {"placeholder", "aria-placeholder"}:
            value = (attribute.group(2) or "").strip().strip('"\'').strip()
            return value or None
    return None


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
        heading_names: list[tuple[int, str]] = []
        heading_lines: list[tuple[int, str]] = []
        active_section: str | None = None
        active_section_required = False
        progress: str | None = None
        questions: list[QuestionObservation] = []
        controls: list[NavigationControl] = []
        section_actions: list[SectionAction] = []
        validation: list[str] = []
        option_targets: dict[tuple[str, str], str] = {}
        group_label: str | None = None
        group_ref: str | None = None
        group_indent = -1
        group_options: list[str] = []
        checked: str | None = None
        group_required: bool | None = None
        group_button_count = 0
        combo_label: str | None = None
        combo_ref: str | None = None
        combo_indent = -1
        combo_options: list[str] = []
        combo_value: str | None = None
        combo_required: bool | None = None
        combo_is_button = False
        combo_is_typeahead = False
        combo_selected = False
        combo_is_multi = False
        combo_selected_values: list[str] = []
        combo_evidence: AnswerEvidence | None = None
        combo_placeholder: str | None = None
        combo_raw_role: str | None = None
        combo_required_evidence: str | None = None
        selected_items_indent: int | None = None
        selected_items_values: list[str] = []

        def finish_selected_items() -> None:
            nonlocal selected_items_indent, selected_items_values, combo_value, combo_evidence
            nonlocal combo_selected_values
            values = list(dict.fromkeys(value.strip() for value in selected_items_values
                                        if value.strip() and value.strip().casefold() not in {
                                            "items selected", "select", "select one", "choose"}))
            if len(values) == 1 and values[0].casefold() != (combo_label or "").casefold():
                combo_value = values[0]
                combo_evidence = AnswerEvidence.SELECTED_ITEMS
            if combo_is_multi and values:
                combo_selected_values = values
                combo_evidence = AnswerEvidence.SELECTED_ITEMS
            selected_items_indent = None
            selected_items_values = []

        def finish_group() -> None:
            nonlocal group_label, group_ref, group_options, checked, group_required
            if group_label is not None and group_options:
                questions.append(QuestionObservation(
                    label=group_label, control_type=ControlType.CHOICE,
                    semantic_key=self._aliases.get(group_label.casefold()),
                    section=headings.get(2), record_context=active_section,
                    options=tuple(group_options),
                    current_value=checked, required=group_required,
                    answer_evidence=AnswerEvidence.SELECTED_OPTION if checked else None,
                    target_ref=group_ref,
                ))
            group_label = group_ref = checked = None
            group_options = []
            group_required = None

        def finish_combo() -> None:
            nonlocal combo_label, combo_ref, combo_options, combo_value, combo_required, combo_is_button
            nonlocal combo_is_typeahead
            nonlocal combo_selected
            nonlocal combo_is_multi
            nonlocal combo_evidence
            nonlocal combo_placeholder
            nonlocal combo_raw_role, combo_required_evidence
            nonlocal combo_selected_values
            finish_selected_items()
            if combo_label is not None and combo_ref is not None:
                questions.append(QuestionObservation(
                    label=combo_label, control_type=(ControlType.MULTI_CHOICE if combo_is_multi else
                        ControlType.TYPEAHEAD if combo_is_typeahead else
                        ControlType.CHOICE if not combo_options or
                        not combo_is_button or all((combo_ref, option) in option_targets
                                                   for option in combo_options) else ControlType.UNKNOWN),
                    semantic_key=self._aliases.get(combo_label.casefold()),
                    section=headings.get(2), record_context=active_section,
                    options=tuple(combo_options),
                    current_value=combo_value,
                    answer_evidence=combo_evidence,
                    placeholder_text=combo_placeholder,
                    selection_confirmed=bool(combo_is_typeahead and combo_selected),
                    required=combo_required, target_ref=combo_ref,
                    raw_role=combo_raw_role, required_evidence=combo_required_evidence,
                    selected_values=tuple(dict.fromkeys(combo_selected_values))))
            combo_label = combo_ref = combo_value = None
            combo_options = []
            combo_required = None
            combo_is_button = False
            combo_is_typeahead = False
            combo_selected = False
            combo_is_multi = False
            combo_evidence = None
            combo_selected_values = []
            combo_raw_role = combo_required_evidence = None
            combo_placeholder = None

        lines = yaml_match.group(1).splitlines()
        captured: set[int] = set()
        nearby_label: str | None = None
        nearby_required: bool | None = None
        fallback_labels: dict[int, str] = {}
        fallback_required: dict[int, bool | None] = {}
        orphan_radios: list[tuple[str, str, str, str]] = []
        orphan_label: str | None = None
        orphan_required: bool | None = None

        def finish_orphan_radios() -> None:
            nonlocal orphan_radios, orphan_label, orphan_required
            if orphan_radios:
                ref = orphan_radios[0][1]
                label = orphan_label or "Unlabeled radio question"
                options = tuple(option for option, _, _, _ in orphan_radios)
                questions.append(QuestionObservation(
                    label=label, control_type=ControlType.CHOICE,
                    semantic_key=self._aliases.get(label.casefold()), section=headings.get(2),
                    record_context=active_section,
                    options=options, required=orphan_required, target_ref=ref,
                    current_value=next((option for option, _, flags, _ in orphan_radios
                                        if "[checked]" in flags), None),
                    answer_evidence=(AnswerEvidence.SELECTED_OPTION if any(
                        "[checked]" in flags for _, _, flags, _ in orphan_radios) else None)))
                for option, option_ref, _, _ in orphan_radios:
                    option_targets[(ref, option)] = option_ref
            orphan_radios = []
            orphan_label = None
            orphan_required = None

        for index, line in enumerate(lines):
            indent = len(line) - len(line.lstrip())
            role_line = _ROLE_LINE.match(line)
            role = role_line.group(2) if role_line else None
            if (selected_items_indent is not None and indent <= selected_items_indent and
                    line.lstrip().startswith("- ")):
                finish_selected_items()
            if orphan_radios and role not in {"radio", "generic", "text"}:
                finish_orphan_radios()
            if group_label is not None and indent <= group_indent and line.lstrip().startswith("- "):
                finish_group()
            popup_name = (_QUOTED_NAME.search(role_line.group(3).split("[ref=", 1)[0])
                          if role in {"listbox", "generic"} and role_line is not None else None)
            status_text = (role_line.group(3).split(":", 1)[-1].strip().casefold()
                           if role == "generic" and role_line is not None else "")
            selected_status = (combo_ref is not None and
                               ((popup_name is not None and
                                 _label(popup_name.group(0)).casefold() == "items selected") or
                                status_text == "items selected"))
            popup_listbox = (role == "listbox" and combo_ref is not None and
                             (indent > combo_indent or popup_name is None or
                              _label(popup_name.group(0)).casefold() in {
                                  "items selected", (combo_label or "").casefold()} or
                              (combo_is_typeahead and popup_name is not None and
                               _label(popup_name.group(0)).casefold() in {
                                   "suggestions", "results"})))
            if selected_status:
                selected_items_indent = indent
                selected_items_values = []
                status_match = _LISTBOX.match(line)
                if status_match and status_match.group(5):
                    selected_items_values.append(_scalar(status_match.group(5)))
            if (combo_label is not None and indent <= combo_indent and line.lstrip().startswith("- ")
                    and role != "option" and not popup_listbox and not selected_status):
                finish_combo()
            if match := _HEADING.match(line):
                name = _scalar(match.group(1))
                headings[int(match.group(2))] = name
                heading_names.append((int(match.group(2)), name))
                heading_lines.append((index, name))
                if int(match.group(2)) >= 3:
                    active_section = _label(match.group(1))
                    active_section_required = _required("", name) is True
                else:
                    active_section = None
                    active_section_required = False
            elif match := _PARAGRAPH.match(line):
                value = _scalar(match.group(1))
                if value.casefold().startswith("step "):
                    progress = value
            elif match := _GROUP.match(line):
                finish_group()
                group_indent = len(match.group(1))
                group_label = _label(match.group(2))
                reference = re.search(r'\[ref=([^\]\s]{1,128})\]', match.group(3))
                group_ref = reference.group(1) if reference else None
                group_required = _required(match.group(3), match.group(2))
                group_button_count = 0
                for nested in lines[index + 1:]:
                    nested_indent = len(nested) - len(nested.lstrip())
                    if nested.lstrip().startswith("- ") and nested_indent <= group_indent:
                        break
                    if _BUTTON.match(nested):
                        group_button_count += 1
            elif match := _RADIO.match(line):
                if group_label is not None:
                    captured.add(index)
                    group_required = group_required or _required(match.group(2), group_label)
                    option = _scalar(match.group(1))
                    group_options.append(option)
                    if group_ref is None:
                        group_ref = match.group(3)
                    option_targets[(group_ref, option)] = match.group(3)
                    if "[checked]" in match.group(2):
                        checked = option
                else:
                    captured.add(index)
                    if orphan_label is None:
                        orphan_label = nearby_label
                    option_required = _with_nearby_required(match.group(2), orphan_label or "",
                                                            nearby_required)
                    if orphan_required is None:
                        orphan_required = option_required
                    elif option_required is True:
                        orphan_required = True
                    orphan_radios.append((_scalar(match.group(1)), match.group(3), match.group(2), line))
            elif match := _COMBO.match(line):
                captured.add(index)
                finish_combo()
                combo_indent = len(match.group(1))
                combo_label, combo_ref = _label(match.group(2)), match.group(4)
                combo_required = _with_nearby_required(
                    match.group(3), match.group(2),
                    nearby_required if nearby_label == combo_label else None)
                combo_raw_role = "combobox"
                combo_required_evidence = _required_evidence(
                    match.group(3), match.group(2), nearby_required if nearby_label == combo_label else None)
                combo_placeholder = _placeholder(match.group(3))
                combo_is_button = False
                combo_is_typeahead = "[aria-autocomplete=list]" in match.group(3).casefold()
                combo_is_multi = ("[multiple]" in match.group(3).casefold() or
                                  "[aria-multiselectable=true]" in match.group(3).casefold())
                combo_selected = False
                combo_value = _scalar(match.group(5)) if match.group(5) else None
                combo_evidence = AnswerEvidence.SNAPSHOT_VALUE if combo_value else None
            elif (not popup_listbox and (match := _LISTBOX.match(line)) and
                  not is_selector_status_label(_label(match.group(2)))):
                captured.add(index)
                finish_combo()
                combo_indent = len(match.group(1))
                combo_label, combo_ref = _label(match.group(2)), match.group(4)
                combo_required = _with_nearby_required(match.group(3), match.group(2),
                                                       nearby_required if nearby_label == combo_label else None)
                combo_raw_role = "listbox"
                combo_required_evidence = _required_evidence(
                    match.group(3), match.group(2), nearby_required if nearby_label == combo_label else None)
                combo_placeholder = _placeholder(match.group(3))
                combo_is_multi = ("[multiple]" in match.group(3).casefold() or
                                  "[aria-multiselectable=true]" in match.group(3).casefold())
                combo_value = _scalar(match.group(5)) if match.group(5) else None
                combo_evidence = AnswerEvidence.SNAPSHOT_VALUE if combo_value else None
            elif match := _OPTION.match(line):
                if combo_ref is not None:
                    captured.add(index)
                    option = _scalar(match.group(1))
                    combo_options.append(option)
                    if selected_items_indent is not None and indent > selected_items_indent:
                        selected_items_values.append(option)
                    if match.group(3):
                        option_targets[(combo_ref, option)] = match.group(3)
                    if "[selected]" in match.group(2) and option.casefold() not in {"select", "choose", "select one"}:
                        combo_value = option
                        combo_evidence = AnswerEvidence.SELECTED_OPTION
                        combo_selected = True
                        if combo_is_multi:
                            combo_selected_values.append(option)
            elif (selected_items_indent is not None and indent > selected_items_indent and
                  role in {"generic", "text"} and role_line is not None):
                remainder = role_line.group(3).strip()
                quoted = _QUOTED_NAME.search(remainder.split("[ref=", 1)[0])
                value = _scalar(quoted.group(0)) if quoted else _scalar(
                    remainder.split(":", 1)[1]) if ":" in remainder else ""
                if value:
                    selected_items_values.append(value)
            elif match := (_CHECKBOX.match(line) or _SWITCH.match(line)):
                captured.add(index)
                label = _label(match.group(1))
                questions.append(QuestionObservation(
                    label=label, control_type=ControlType.TOGGLE,
                    semantic_key=self._aliases.get(label.casefold()), section=headings.get(2),
                    record_context=active_section,
                    required=_with_nearby_required(match.group(2), match.group(1),
                                                   nearby_required if nearby_label == label else None),
                    current_value="checked" if ("[checked]" in match.group(2) or
                                                "[aria-checked=true]" in match.group(2)) else "unchecked",
                    answer_evidence=AnswerEvidence.CHECKED_STATE,
                    target_ref=match.group(3)))
            elif match := _TEXTBOX.match(line):
                captured.add(index)
                label = _label(match.group(1))
                if "[aria-autocomplete=list]" in match.group(2).casefold():
                    finish_combo()
                    combo_indent = indent
                    combo_label, combo_ref = label, match.group(3)
                    combo_required = _with_nearby_required(match.group(2), match.group(1),
                                                           nearby_required if nearby_label == label else None)
                    combo_raw_role = "textbox"
                    combo_required_evidence = _required_evidence(
                        match.group(2), match.group(1), nearby_required if nearby_label == label else None)
                    combo_placeholder = _placeholder(match.group(2))
                    combo_is_typeahead = True
                    combo_value = _scalar(match.group(4)) if match.group(4) else None
                    combo_selected = False
                    continue
                custom_search = label.casefold() == "search" and nearby_label is not None
                if custom_search:
                    label = nearby_label
                secret = bool(re.search(r"\b(password|passphrase)\b", label, re.IGNORECASE))
                date_hint = re.search(r'\[(?:type|inputtype)=(date|month)\]', match.group(2), re.I)
                month_hint = re.search(r'\[placeholder=["\']?MM/YYYY["\']?\]', match.group(2), re.I)
                date_format = (date_hint.group(1).casefold() if date_hint else
                               "MM/YYYY" if month_hint and re.search(r"\b(date|from|to|month)\b", label, re.I)
                               else None)
                questions.append(QuestionObservation(
                    label=label, control_type=(ControlType.UNKNOWN if custom_search else
                                               ControlType.SECRET if secret else
                                               ControlType.DATE if date_format else ControlType.TEXT),
                    semantic_key=self._aliases.get(label.casefold()),
                    section=headings.get(2), record_context=active_section,
                    current_value=None if secret else _scalar(match.group(4)) if match.group(4) else None,
                    answer_evidence=AnswerEvidence.SNAPSHOT_VALUE if match.group(4) and not secret else None,
                    placeholder_text=_placeholder(match.group(2)),
                    required=_with_nearby_required(
                        match.group(2), match.group(1),
                        nearby_required if nearby_label == label else None),
                    target_ref=match.group(3),
                    date_format=date_format,
                ))
            elif match := _BUTTON.match(line):
                label = _label(match.group(1))
                normalized = " ".join(label.casefold().split())
                is_section_action = normalized in {"add", "add another", "delete", "remove"}
                is_resume_upload = normalized in {"upload resume", "upload cv", "attach resume", "attach cv"}
                choice_group = (group_label if group_label and indent > group_indent and
                                group_button_count == 1 and not is_section_action and
                                not is_resume_upload and
                                normalized not in {"more", "show more", "expand", "collapse"} and
                                _nav_kind(label, self._navigation) is NavigationKind.UNKNOWN else None)
                is_choice_button = (bool(re.search(r'\[(?:haspopup|expanded)(?:=|\])', match.group(2))) or
                                    choice_group is not None or
                                    bool(nearby_label and normalized in {
                                        "select one", "select", "choose one", "choose"}))
                if is_resume_upload:
                    questions.append(QuestionObservation(
                        label=label, control_type=ControlType.FILE,
                        semantic_key="documents.resume", section=headings.get(2),
                        record_context=active_section,
                        required=_with_nearby_required(match.group(2), label,
                                                       nearby_required if nearby_label == label else None),
                        target_ref=match.group(3)))
                elif is_section_action:
                    section_actions.append(SectionAction(label, active_section or headings.get(2),
                                                         match.group(3)))
                    if normalized.startswith("add") and active_section and active_section_required:
                        questions.append(QuestionObservation(
                            label=f"{active_section} section", control_type=ControlType.UNKNOWN,
                            section=headings.get(2), record_context=active_section, required=True))
                elif is_choice_button:
                    question_label = choice_group or nearby_label or label
                    finish_combo()
                    combo_indent = indent
                    combo_label, combo_ref = question_label, match.group(3)
                    combo_required = _with_nearby_required(
                        match.group(2), question_label,
                        True if group_required is True or nearby_required is True else
                        False if group_required is False or nearby_required is False else None)
                    combo_raw_role = "button"
                    combo_required_evidence = (None if combo_required is not True else
                        _required_evidence(match.group(2), question_label, nearby_required) or
                        ("group_required" if group_required is True else None))
                    combo_placeholder = _placeholder(match.group(2))
                    combo_is_button = True
                    combo_value = label if label.casefold() != question_label.casefold() else None
                    combo_evidence = AnswerEvidence.BUTTON_SELECTION if combo_value else None
                else:
                    controls.append(NavigationControl(label, _nav_kind(label, self._navigation), match.group(3)))
                captured.add(index)
            elif match := _ALERT.match(line):
                validation.append(_scalar(match.group(1)))
            if popup_listbox and combo_ref is not None:
                captured.add(index)
            if (role_line and role_line.group(2) in
                    {'textbox', 'combobox', 'listbox', 'checkbox', 'radio', 'switch', 'spinbutton', 'slider', 'button'}):
                if nearby_label:
                    fallback_labels[index] = nearby_label
                    fallback_required[index] = nearby_required
                nearby_label = None
                nearby_required = None
            # A nearby static label can name an otherwise unnamed custom
            # control. It is review-only; never authorize a fill from it.
            if (selected_items_indent is None and
                    (match := re.match(r'^\s*- (?:generic|text)(?: \[ref=[^\]\s]{1,128}\])?:\s*(.+)$', line))):
                candidate = _scalar(match.group(1))
                if candidate.strip() == "*" and nearby_label:
                    nearby_required = True
                elif (0 < len(candidate) <= 100 and "\n" not in candidate and
                      ('?' in candidate or '*' in candidate or
                       (index + 1 < len(lines) and
                        (next_label := re.match(
                            r'^\s*- (?:generic|text)(?: \[ref=[^\]\s]{1,128}\])?:\s*(.+)$',
                            lines[index + 1])) is not None and
                        _scalar(next_label.group(1)).strip() == '*'))):
                    nearby_label = _label(candidate) or None
                    nearby_required = _required("", candidate) if nearby_label else None
            elif re.match(r'^\s*- (?:heading|button)\b', line):
                nearby_label = None
                nearby_required = None
        finish_group()
        finish_combo()
        finish_orphan_radios()

        # Preserve every actionable role the narrow typed parsers could not
        # represent. Unknown controls are reported for human review and carry
        # no automation permission, even when a candidate answer exists.
        fallback_roles = {'textbox', 'combobox', 'listbox', 'checkbox',
                          'switch', 'spinbutton', 'slider'}
        for index, line in enumerate(lines):
            if index in captured:
                continue
            role_line = _ROLE_LINE.match(line)
            if not role_line or role_line.group(2) not in fallback_roles:
                continue
            role, remainder = role_line.group(2), role_line.group(3)
            quoted = _QUOTED_NAME.search(remainder.split('[ref=', 1)[0])
            label = (_label(quoted.group(0)) if quoted else fallback_labels.get(index))
            if not label:
                continue
            if (role in {"listbox", "combobox"} and is_selector_status_label(label)
                    and not fallback_labels.get(index)):
                # Selector status text is not a separate application question.
                continue
            attributes = {attr.group(1): attr.group(2) for attr in _ATTRIBUTE.finditer(remainder)}
            questions.append(QuestionObservation(
                label=label, control_type=ControlType.UNKNOWN,
                section=headings.get(2), record_context=active_section,
                required=_with_nearby_required(remainder, label, fallback_required.get(index)),
                target_ref=attributes.get('ref')))

        # A composite widget can repeat the same ref in two accessible roles.
        # Distinct refs with the same label may instead be repeated records.
        unique: dict[tuple[str, str], QuestionObservation] = {}
        unreferenced: list[QuestionObservation] = []
        for question in questions:
            if not question.target_ref:
                if (question.label.endswith(" section") and any(
                        prior.label == question.label and prior.section == question.section and
                        prior.record_context == question.record_context for prior in unreferenced)):
                    continue
                unreferenced.append(question)
                continue
            key = (question.target_ref, question.label.casefold())
            previous = unique.get(key)
            if previous is None or (previous.control_type is ControlType.UNKNOWN and
                                    question.control_type is not ControlType.UNKNOWN):
                unique[key] = question
        questions = [*unique.values(), *unreferenced]
        occurrences: dict[tuple[str, str, str], int] = {}
        scoped_questions: list[QuestionObservation] = []
        for question in questions:
            scope = ((question.semantic_key or question.label).casefold(),
                     (question.section or "").casefold(),
                     (question.record_context or "").casefold())
            occurrence = occurrences.get(scope, 0)
            occurrences[scope] = occurrence + 1
            scoped_questions.append(replace(question, occurrence=occurrence))
        questions = scoped_questions

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
        company = next((match.group(1).strip() for _, name in heading_names
                        if (match := re.fullmatch(r"(?:careers|jobs) at (.{2,100})", name, re.I))), None)
        generic_titles = {"application", "job application", "my information", "personal information",
                          "application questions", "contact information", "work experience",
                          "employment history", "education", "certifications", "review and submit",
                          "sign in", "log in", "review", "review application", "careers", "jobs"}
        def title_eligible(name: str) -> bool:
            return (name.casefold() not in generic_titles
                    and name.casefold() != (company or "").casefold()
                    and not name.casefold().startswith(("careers at ", "jobs at "))
                    and not name.endswith("?") and len(name) <= 180)

        form_sections = [index for index, (level, name) in enumerate(heading_names)
                         if level == 2 and name.casefold() in generic_titles]
        h1_titles = [name for level, name in heading_names if level == 1 and title_eligible(name)]
        h2_candidates = [(index, name) for index, (level, name) in enumerate(heading_names)
                         if level == 2 and len(name) >= 10 and title_eligible(name)]
        h2_titles = [name for index, name in h2_candidates
                     if any(section > index for section in form_sections)]
        if not h2_titles and len(h2_candidates) == 1:
            h2_titles = [h2_candidates[0][1]]
        job_title = (h1_titles[0] if h1_titles else h2_titles[0] if h2_titles else None)
        # Step checkpoint: the first form heading after the page heading and
        # before the first field. It distinguishes ATS steps that share one
        # page heading (for example the job title) without using live refs.
        refs = {question.target_ref for question in questions if question.target_ref}
        field_lines = [index for index, line in enumerate(lines)
                       if (ref := re.search(r'\[ref=([^\]\s]{1,128})\]', line)) and ref.group(1) in refs]
        heading_line = max((index for index, name in heading_lines if name == heading), default=-1)
        excluded = {heading.casefold(), (job_title or "").casefold(), (company or "").casefold()}
        checkpoint = next((name for index, name in heading_lines
                           if heading_line < index < min(field_lines, default=-1) and
                           name.casefold() not in excluded and not name.endswith("?") and
                           not name.casefold().startswith(("careers at ", "jobs at ")) and
                           len(name) <= 120), None)
        return NormalizedSnapshot(ApplicationObservation(
            observation_id=observation_id, location=url_match.group(1).strip(),
            heading=heading, progress_text=progress, questions=tuple(questions),
            validation_messages=tuple(validation), navigation_controls=tuple(controls),
            review_like=review_like, job_title=job_title, company=company,
            section_actions=tuple(section_actions), checkpoint=checkpoint,
        ), option_targets, diagnostic)
