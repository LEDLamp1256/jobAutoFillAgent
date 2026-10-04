"""Bounded DOM evidence that supplements a fresh accessibility snapshot."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from .domain import (
    AnswerEvidence, ApplicationObservation, ControlType, DiscoverySummary,
    NavigationControl, NavigationKind, QuestionObservation, SectionAction,
)
from .snapshot import _nav_kind


DOM_DISCOVERY_SCRIPT = r"""() => {
  const root = document.querySelector('main,[role="main"]') ||
               document.querySelector('form') || document.body;
  const all = [...root.querySelectorAll(
    'input,textarea,select,button,[role="button"],[role="combobox"],[role="listbox"],'+
    '[role="checkbox"],[role="switch"],[role="radio"],[aria-haspopup]')];
  const groups = [...root.querySelectorAll('fieldset,[role="group"]')];
  const ignored = {};
  const structuralLabels = [];
  const ignore = reason => { ignored[reason] = (ignored[reason] || 0) + 1; };
  const text = node => (node?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  const selectedText = node => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('button,[role="button"]').forEach(action => action.remove());
    return text(copy);
  };
  const labelText = node => {
    if (!node) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('input,textarea,select,button,[role="combobox"],'+
      '[role="listbox"],[role="option"]').forEach(control => control.remove());
    return text(copy);
  };
  const short = value => typeof value === 'string' ? value.trim().slice(0, 200) : null;
  const visible = node => !!node && !node.closest('[hidden]') &&
    node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
  const placeholder = value => !value || /^(select( one| an option)?|please select( an option)?|choose( one| an option)?|none selected|no selection|no file chosen)$/i.test(value.trim());
  const labelled = element => {
    const ids = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const byIds = ids.map(id => labelText(document.getElementById(id))).filter(Boolean).join(' ');
    const byLabel = [...(element.labels || [])].map(labelText).filter(Boolean).join(' ');
    const byFor = element.id ? labelText(document.querySelector(`label[for="${CSS.escape(element.id)}"]`)) : '';
    return short(byIds || byLabel || byFor || element.getAttribute('aria-label') ||
                 labelText(element.closest('label')));
  };
  const controlsIn = node => [...node.querySelectorAll(
    'input:not([type="hidden"]),textarea,select,button,[role="button"],[role="combobox"]')]
    .filter(item => visible(item) && !item.closest('[aria-label="items selected"],[role="option"],'+
      '[class*="token"],[class*="chip"],[data-automation-id*="selectedItem"]'));
  const fieldContainer = (element, label) => {
    // Stop at the smallest labeled widget. A section containing several
    // fields must never lend one field another field's selected value.
    for (let node = element.parentElement, depth = 0; node && node !== root && depth < 7;
         node = node.parentElement, depth++) {
      const controls = controlsIn(node);
      if (controls.length > 4) break;
      const labels = [...node.querySelectorAll('label,legend')].filter(visible);
      const matching = labels.some(item =>
        labelText(item).replace(/\s*\*\s*$/, '').trim() === label);
      const explicit = node.getAttribute('aria-label') === label ||
        (node.getAttribute('aria-labelledby') || '').split(/\s+/).some(id =>
          labelText(document.getElementById(id)).replace(/\s*\*\s*$/, '').trim() === label);
      if ((matching || explicit) && labels.filter(item =>
          labelText(item).replace(/\s*\*\s*$/, '').trim() !== label).length === 0) return node;
    }
    return null;
  };
  const associatedSelection = (element, group, label) => {
    const linked = (element.getAttribute('aria-controls') || ' ' +
      (element.getAttribute('aria-owns') || '') + ' ' +
      (element.getAttribute('aria-describedby') || '')).split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)).filter(Boolean);
    const field = fieldContainer(element, label);
    const containers = [...linked, field, group].filter(Boolean);
    for (const container of containers) {
      if (container === group || container === field) {
        const controls = controlsIn(container);
        if (controls.length > 4 || !controls.includes(element)) continue;
      }
      const selected = [...container.querySelectorAll('[aria-selected="true"],[selected]')]
        .filter(node => visible(node) && node.tagName !== 'OPTION');
      if (selected.length === 1) {
        const value = short(selected[0].getAttribute('aria-label') || selectedText(selected[0]));
        if (value && !placeholder(value) && value !== label) return value;
      }
      const statuses = [...container.querySelectorAll('[aria-label="items selected"]')].filter(visible);
      if (statuses.length === 1) {
        const items = [...statuses[0].querySelectorAll('[role="option"],[aria-selected="true"]')]
          .map(node => short(node.getAttribute('aria-label') || selectedText(node))).filter(Boolean);
        if (items.length === 1 && !placeholder(items[0])) return items[0];
        if (!items.length && statuses[0].children.length === 1) {
          const value = text(statuses[0].children[0]);
          if (value && !placeholder(value)) return value;
        }
      }
      // Some single-select widgets render a removable chip without option or
      // selected-items roles. Only accept one chip in the same labeled field.
      if (container === field) {
        const chips = [...container.querySelectorAll(
          '[class*="token"],[class*="chip"],[data-automation-id*="selectedItem"]')]
          .filter(node => visible(node) && !node.closest('[role="option"]') &&
            node.querySelector('button,[role="button"],[aria-label*="Remove"],[aria-label*="remove"]'));
        const leaves = chips.filter(node => !chips.some(other => other !== node && node.contains(other)));
        if (leaves.length === 1) {
          const value = short(selectedText(leaves[0]));
          if (value && !placeholder(value) && value !== label) return value;
        }
        // A removable selected value need not use a token/chip class. Keep
        // the association inside the one labeled field and reject containers
        // that also contain a search input or another remove action.
        const removers = [...container.querySelectorAll(
          'button[aria-label*="remove" i],[role="button"][aria-label*="remove" i]')]
          .filter(visible);
        if (removers.length === 1) {
          const token = removers[0].parentElement;
          if (token && token !== container && !token.querySelector('input,textarea,select')) {
            const value = short(selectedText(token));
            if (value && !placeholder(value) && value !== label) return value;
          }
        }
      }
      const value = short(container.getAttribute('aria-valuetext'));
      if (value && !placeholder(value)) return value;
      if (container === group || container === field) {
        const buttons = [...container.querySelectorAll('button,[role="button"]')].filter(visible);
        const search = container.querySelector('input:not([type="hidden"]),textarea');
        if (buttons.length === 1 && !search && !buttons[0].hasAttribute('aria-haspopup')) {
          const value = text(buttons[0]);
          if (value && !placeholder(value) && value !== label) return value;
        }
      }
    }
    return null;
  };
  const candidates = [];
  for (const element of all.slice(0, 160)) {
    if (!visible(element)) { ignore('not_visible'); continue; }
    if (element.closest('[aria-label="items selected"],[role="option"],'+
        '[class*="token"],[class*="chip"],[data-automation-id*="selectedItem"]')) {
      ignore('selector_token_action'); continue;
    }
    if (element.matches('[role="listbox"]') && element.id &&
        root.querySelector(`[aria-controls~="${CSS.escape(element.id)}"],[aria-owns~="${CSS.escape(element.id)}"]`)) {
      ignore('linked_options_container'); continue;
    }
    if (element.matches('input[type="hidden"],input[type="password"]')) {
      ignore('hidden_or_secret'); continue;
    }
    const enclosingGroup = element.closest('fieldset,[role="group"]');
    const groupControls = enclosingGroup ? controlsIn(enclosingGroup) : [];
    const radioGroup = groupControls.length > 0 && groupControls.every(node =>
      node.matches('input[type="radio"],[role="radio"]'));
    const group = groupControls.length <= 3 || radioGroup ? enclosingGroup : null;
    if (enclosingGroup && !group && element.matches('button,[role="button"]') &&
        text(element) === (enclosingGroup.getAttribute('aria-label') || '')) {
      ignore('section_or_container'); structuralLabels.push(text(element)); continue;
    }
    const groupLabel = short(group?.getAttribute('aria-label') ||
      text(group?.querySelector(':scope > legend')) ||
      (group?.getAttribute('aria-labelledby') || '').split(/\s+/).map(
        id => text(document.getElementById(id))).filter(Boolean).join(' '));
    const ownLabel = labelled(element);
    const type = (element.getAttribute('type') || '').toLowerCase();
    const isButton = element.matches('button,[role="button"],input[type="button"],input[type="submit"]');
    const buttonText = isButton ? (text(element) || short(element.value)) : null;
    const label = short(((type === 'radio' || element.getAttribute('role') === 'radio') && groupLabel) ||
                        (isButton && groupLabel) || ownLabel || groupLabel ||
                        buttonText);
    if (!label) { ignore('unlabeled'); continue; }
    const cleanLabel = label.replace(/\s*\*\s*$/, '').trim();
    if (['items selected', 'options expanded', 'options collapsed']
          .includes(cleanLabel.toLowerCase().replace(/\s+/g, ' ')) &&
        element.matches('[role="listbox"],[role="status"]')) {
      ignore('selector_status'); continue;
    }
    const requiredFlag = element.getAttribute('aria-required');
    const groupRequired = group?.getAttribute('aria-required');
    const directGroupText = group ? [...group.querySelectorAll(':scope > label,:scope > legend,:scope > span')]
      .map(labelText).filter(Boolean) : [];
    const marker = label.endsWith('*') || directGroupText.some(part =>
      (part.endsWith('*') && part.replace(/\s*\*\s*$/, '').trim() === cleanLabel) ||
      (part === '*' && groupLabel === cleanLabel));
    const required = requiredFlag === 'true' ? true : requiredFlag === 'false' ? false :
      element.required === true ? true : groupRequired === 'true' ? true :
      groupRequired === 'false' ? false : marker ? true : null;
    const requiredEvidence = requiredFlag === 'true' ? 'aria_required' :
      element.required === true ? 'html_required' : groupRequired === 'true' ? 'group_required' :
      marker ? 'associated_required_marker' : null;
    const section = element.closest('section')?.querySelector(':scope > h2,:scope > h3');
    let kind = 'unknown', value = null, evidence = null, selectedValues = [], confirmed = false;
    if (element.matches('input[type="checkbox"],[role="checkbox"],[role="switch"]')) {
      kind = 'toggle'; value = element.checked === true || element.getAttribute('aria-checked') === 'true'
        ? 'checked' : 'unchecked'; evidence = 'checked_state';
    } else if (element.matches('input[type="radio"],[role="radio"]')) {
      kind = 'radio'; value = element.checked === true || element.getAttribute('aria-checked') === 'true'
        ? short(element.value || ownLabel) : null; evidence = value ? 'checked_state' : null;
    } else if (element.matches('select[multiple]')) {
      kind = 'multi_choice'; selectedValues = [...element.selectedOptions]
        .map(option => text(option)).filter(Boolean).slice(0, 20);
      evidence = 'dom_value';
    } else if (element.matches('select')) {
      kind = 'choice'; value = short(element.selectedOptions?.[0]?.textContent);
      evidence = 'dom_value';
    } else if (element.matches('input,textarea')) {
      const field = fieldContainer(element, cleanLabel);
      const selector = !!field && !!field.querySelector(
        '[aria-haspopup="listbox"],[role="listbox"],[role="option"],'+
        '[class*="token"],[class*="chip"],[data-automation-id*="selectedItem"]');
      kind = element.matches('textarea') ? 'text' :
        ['date','month'].includes(type) ? 'date' :
        element.getAttribute('aria-autocomplete') === 'list' || selector ||
        element.getAttribute('role') === 'combobox' ? 'typeahead' : 'text';
      value = short(element.value); evidence = 'dom_value';
    } else if (element.matches('[role="combobox"],[role="listbox"]')) {
      kind = 'choice'; value = short(element.getAttribute('aria-valuetext'));
      evidence = value ? 'dom_value' : null;
    } else if (isButton) {
      const namedGroup = !!groupLabel && !!group &&
        [...group.querySelectorAll('button,[role="button"]')].filter(visible).length === 1;
      const fieldLabel = namedGroup && !group.querySelector('input,textarea') && (
        group.matches('fieldset,[aria-required]') ||
        [...group.querySelectorAll('label,legend')].some(node =>
          visible(node) && labelText(node).replace(/\s*\*\s*$/, '').trim() ===
          groupLabel.replace(/\s*\*\s*$/, '').trim()) ||
        [...group.querySelectorAll(':scope > span')].some(node =>
          visible(node) && labelText(node).endsWith('*')));
      const linkedChoice = !!ownLabel && ownLabel !== buttonText &&
        (!!fieldContainer(element, ownLabel.replace(/\s*\*\s*$/, '').trim()) ||
         element.hasAttribute('aria-haspopup') || element.hasAttribute('aria-controls') ||
         element.getAttribute('aria-required') === 'true');
      kind = fieldLabel || linkedChoice
        ? 'choice' : 'button';
      value = kind === 'choice' ? buttonText : null;
      evidence = value ? 'button_selection' : null;
    }
    if (kind !== 'toggle' && kind !== 'radio' && kind !== 'multi_choice') {
      const associated = associatedSelection(element, group, cleanLabel);
      if (associated && (kind === 'typeahead' || placeholder(value) ||
                         value === cleanLabel || kind === 'button')) {
        kind = 'choice';
        value = associated; evidence = 'dom_associated'; confirmed = true;
      }
    }
    const field = fieldContainer(element, cleanLabel);
    const optionRoot = (element.getAttribute('aria-controls') || '').split(/\s+/)
      .map(id => document.getElementById(id)).find(node => node?.querySelector('[role="option"]')) || field;
    const options = kind === 'choice' || kind === 'typeahead'
      ? [...(optionRoot?.querySelectorAll('[role="option"]') || [])]
        .filter(node => visible(node) && !node.closest('[aria-label="items selected"]'))
        .map(node => short(node.getAttribute('aria-label') || selectedText(node)))
        .filter(Boolean).slice(0, 40) : [];
    candidates.push({label: cleanLabel, kind, value, evidence, selectedValues, confirmed,
      options,
      required, requiredEvidence, role: element.getAttribute('role') || element.tagName.toLowerCase(),
      placeholderText: short(element.getAttribute('placeholder') || element.getAttribute('aria-placeholder')),
      section: short(text(section)), groupIndex: group ? groups.indexOf(group) : -1,
      elementIndex: all.indexOf(element), buttonLabel: buttonText});
  }
  return {candidates: candidates.slice(0, 128), rawActionableCount: all.length,
    ignoredReasons: ignored, structuralLabels: structuralLabels.slice(0, 20),
    truncated: all.length > 160 || candidates.length > 128};
}"""


def _key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def merge_dom_observation(observation: ApplicationObservation, payload: Any) -> ApplicationObservation:
    """Merge unique structural labels only; ambiguous answers stay reviewable."""
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        return observation
    ignored = Counter({str(key)[:40]: min(value, 160) for key, value in
                       (payload.get("ignoredReasons") or {}).items()
                       if isinstance(value, int) and value >= 0})
    structural = {_key(label) for label in payload.get("structuralLabels", [])[:20]
                  if isinstance(label, str)} if isinstance(payload.get("structuralLabels"), list) else set()
    questions = [question for question in observation.questions
                 if not (_key(question.label) in structural and question.raw_role == "button" and
                         question.required is None and
                         _key(question.current_value) in {"", _key(question.label)})]
    navigation = list(observation.navigation_controls)
    sections = list(observation.section_actions)
    recovered = 0
    candidates = payload["candidates"][:128]
    grouped: dict[tuple[str, int], list[dict]] = {}
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            ignored["invalid_candidate"] += 1
            continue
        label = item["label"].strip()[:200]
        if not label:
            ignored["unlabeled"] += 1
            continue
        button_label = str(item.get("buttonLabel") or "")[:200]
        if button_label:
            nav = _nav_kind(button_label, {})
            if nav is not NavigationKind.UNKNOWN:
                if not any(_key(control.label) == _key(button_label) for control in navigation):
                    navigation.append(NavigationControl(button_label, nav))
                ignored["navigation_or_submit"] += 1
                continue
            if _key(button_label) in {"add", "add another", "delete", "remove"}:
                if not any(_key(action.label) == _key(button_label) for action in sections):
                    sections.append(SectionAction(button_label, item.get("section")))
                ignored["repeater_action"] += 1
                continue
            if item.get("kind") == "button":
                ignored["unassociated_button"] += 1
                continue
        scope = item.get("groupIndex")
        if not isinstance(scope, int) or scope < 0:
            scope = item.get("elementIndex") if isinstance(item.get("elementIndex"), int) else -1
        grouped.setdefault((_key(label), scope), []).append(item)
    groups_per_label = Counter(label for label, _ in grouped)
    for (label_key, _), group in grouped.items():
        # The input and trigger of one composite widget are one logical field.
        ranked = sorted(group, key=lambda item: (
            bool(item.get("value") and _key(item.get("value")) not in
                 {"select one", "select", "choose", "please select", label_key}),
            item.get("evidence") == "dom_associated", item.get("required") is True), reverse=True)
        item = ranked[0]
        label = str(item["label"]).strip()[:200]
        kind = {kind.value: kind for kind in ControlType}.get(item.get("kind"), ControlType.UNKNOWN)
        if item.get("kind") == "radio":
            kind = ControlType.CHOICE
        required = item.get("required") if isinstance(item.get("required"), bool) else None
        value = item.get("value") if isinstance(item.get("value"), str) else None
        if value is not None:
            value = value[:200]
        evidence = next((part for part in AnswerEvidence if part.value == item.get("evidence")), None)
        selected = item.get("selectedValues")
        selected = tuple(s[:200] for s in selected[:20] if isinstance(s, str)) if isinstance(selected, list) else ()
        options = item.get("options")
        options = tuple(s[:200] for s in options[:40] if isinstance(s, str)) if isinstance(options, list) else ()
        matches = [index for index, question in enumerate(questions)
                   if question.discovery_source != "dom_fallback" and
                   _key(question.label) == label_key and
                   (not item.get("section") or not question.section or
                    _key(question.section) == _key(item.get("section")))]
        if matches and groups_per_label[label_key] > 1:
            # A shared label across repeated records is insufficient to tie
            # one DOM value to one accessibility question.
            ignored["ambiguous_repeated_label"] += 1
            continue
        composite = False
        if len(matches) == 2 and groups_per_label[label_key] == 1:
            roles = {questions[index].raw_role for index in matches}
            composite = ("button" in roles and
                         bool(roles & {"input", "combobox", "textbox"}))
            if composite:
                # One named widget can expose both its empty input and its
                # trigger as separate accessibility questions. The DOM group
                # establishes that they are one field; neither ref is a safe
                # action target for the composite widget.
                questions.pop(matches[1])
                matches = matches[:1]
                ignored["collapsed_composite_node"] += 1
        if len(matches) > 1:
            ignored["ambiguous_field_match"] += 1
            continue
        if len(matches) == 1:
            index = matches[0]
            previous = questions[index]
            candidate = replace(previous,
                control_type=(kind if previous.control_type is ControlType.UNKNOWN or
                              previous.control_type in {ControlType.TEXT, ControlType.TYPEAHEAD} and
                              kind in {ControlType.CHOICE, ControlType.TYPEAHEAD} else
                              previous.control_type),
                required=required if required is not None else previous.required,
                required_evidence=(str(item.get("requiredEvidence"))[:40]
                                   if item.get("requiredEvidence") else previous.required_evidence),
                current_value=value, selected_values=selected or previous.selected_values,
                options=options or previous.options,
                answer_evidence=evidence, raw_role=str(item.get("role") or "")[:32] or previous.raw_role,
                placeholder_text=(str(item.get("placeholderText"))[:200]
                                  if item.get("placeholderText") else previous.placeholder_text),
                selection_confirmed=bool(item.get("confirmed")) or previous.selection_confirmed,
                target_ref=None if composite else previous.target_ref,
                discovery_source="merged")
            old_answer, new_answer = previous.answer_state(), candidate.answer_state()
            if (old_answer.satisfied and not new_answer.placeholder and
                    kind is not ControlType.TYPEAHEAD and
                    _key(old_answer.value) != _key(new_answer.value)):
                candidate = replace(candidate, state_conflict=True)
            elif old_answer.satisfied and not new_answer.satisfied and value is None:
                # No DOM answer evidence is weaker than an explicit selected
                # accessibility option. Keep the observed selection.
                candidate = replace(candidate, current_value=previous.current_value,
                                    selected_values=previous.selected_values,
                                    answer_evidence=previous.answer_evidence)
            questions[index] = candidate
        else:
            section = str(item.get("section") or "")[:200] or None
            occurrence = max((question.occurrence for question in questions
                              if _key(question.label) == label_key and
                              _key(question.section) == _key(section)), default=-1) + 1
            questions.append(QuestionObservation(
                label, kind, section=section, occurrence=occurrence,
                required=required, current_value=value, selected_values=selected,
                options=options,
                answer_evidence=evidence, raw_role=str(item.get("role") or "")[:32] or None,
                required_evidence=str(item.get("requiredEvidence") or "")[:40] or None,
                placeholder_text=str(item.get("placeholderText") or "")[:200] or None,
                selection_confirmed=bool(item.get("confirmed")), discovery_source="dom_fallback"))
            recovered += 1
    summary = DiscoverySummary(
        raw_actionable_count=min(payload.get("rawActionableCount", 0), 10000)
        if isinstance(payload.get("rawActionableCount"), int) else 0,
        accessibility_question_count=len(observation.questions),
        dom_recovered_field_count=recovered,
        ignored_reasons=tuple(sorted(ignored.items())), truncated=bool(payload.get("truncated")))
    return replace(observation, questions=tuple(questions), navigation_controls=tuple(navigation),
                   section_actions=tuple(sections), discovery_summary=summary)
