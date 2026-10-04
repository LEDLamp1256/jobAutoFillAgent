"""Playwright MCP 0.0.82 adapter over the official Python MCP stdio client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from typing import Callable

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from jobagent.browser import BrowserActionResult
from jobagent.dom_discovery import DOM_DISCOVERY_SCRIPT, merge_dom_observation
from jobagent.domain import (
    ActionOutcome, ActionPolicy, ActionStatus, Advance, AnswerEvidence, ApplicationObservation,
    ApplicationSession, ChooseOption, ControlType, FillText, GoBack, NavigationKind, RevealOptions,
    SearchOptions, Toggle, UploadDocument,
    semantic_fingerprint,
)
from jobagent.snapshot import NormalizedSnapshot, SnapshotDiagnostic, SnapshotEmpty, SnapshotNormalizer


class MCPBrowserError(RuntimeError):
    pass


class MCPToolContractError(MCPBrowserError):
    pass


@dataclass(frozen=True)
class MCPServerCommand:
    command: str
    args: tuple[str, ...]
    cwd: Path | None = None


_REQUIRED_SCHEMAS = {
    "browser_navigate": {"url"},
    "browser_snapshot": set(),
    "browser_type": {"target", "text", "submit"},
    "browser_click": {"target"},
    "browser_close": set(),
    "browser_evaluate": {"element", "target", "function"},
    "browser_select_option": {"target", "values"},
}


def validate_tool_contract(tools: list[object]) -> None:
    available = {tool.name: tool.input_schema for tool in tools}
    missing = set(_REQUIRED_SCHEMAS) - set(available)
    if missing:
        raise MCPToolContractError(f"Playwright MCP missing tools: {', '.join(sorted(missing))}")
    for name, properties in _REQUIRED_SCHEMAS.items():
        schema = available[name]
        actual = set(schema.get("properties", {}))
        if not properties <= actual:
            raise MCPToolContractError(
                f"Playwright MCP {name} lacks expected properties: {', '.join(sorted(properties - actual))}"
            )
    expected_types = {("browser_navigate", "url"): "string",
                      ("browser_type", "target"): "string",
                      ("browser_type", "text"): "string",
                      ("browser_type", "submit"): "boolean",
                      ("browser_click", "target"): "string"}
    for (name, property_name), expected in expected_types.items():
        actual_type = available[name]["properties"][property_name].get("type")
        if actual_type != expected:
            raise MCPToolContractError(f"Playwright MCP {name}.{property_name} must be {expected}")


def _status_for_error(message: str) -> ActionStatus:
    lower = message.casefold()
    if "strict mode violation" in lower or "multiple elements" in lower:
        return ActionStatus.AMBIGUOUS_TARGET
    if "not found" in lower or "no element" in lower or "not attached" in lower or "timeout" in lower:
        return ActionStatus.TARGET_MISSING
    return ActionStatus.FAILED


class PlaywrightMCPAdapter:
    """A narrow, sequential BrowserPort; no public raw MCP tool access."""

    def __init__(self, server: MCPServerCommand, *, normalizer: SnapshotNormalizer | None = None,
                 client_factory: Callable[..., object] = Client):
        self._server = server
        self._normalizer = normalizer or SnapshotNormalizer()
        self._client_factory = client_factory
        self._client_context = None
        self._client = None
        self._server_stderr = None
        self._last: NormalizedSnapshot | None = None
        self._observation_counter = 0
        self.discovered_tools: tuple[str, ...] = ()

    async def __aenter__(self) -> PlaywrightMCPAdapter:
        parameters = StdioServerParameters(
            command=self._server.command, args=list(self._server.args), cwd=self._server.cwd,
        )
        try:
            # MCP tool arguments include login credentials. Never inherit raw
            # server stderr into the backend or SwiftUI diagnostic stream.
            self._server_stderr = open(os.devnull, "w", encoding="utf-8")
            transport = stdio_client(parameters, errlog=self._server_stderr)
            self._client_context = self._client_factory(transport)
            self._client = await self._client_context.__aenter__()
            result = await self._client.list_tools()
            validate_tool_contract(result.tools)
            self.discovered_tools = tuple(tool.name for tool in result.tools)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        context, client = self._client_context, self._client
        self._client_context = self._client = None
        self._last = None
        if context is None:
            if self._server_stderr is not None:
                self._server_stderr.close()
                self._server_stderr = None
            return
        try:
            if client is not None and "browser_close" in self.discovered_tools:
                await client.call_tool("browser_close", {})
        finally:
            try:
                await context.__aexit__(None, None, None)
            finally:
                if self._server_stderr is not None:
                    self._server_stderr.close()
                    self._server_stderr = None

    async def _call(self, name: str, arguments: dict) -> object:
        if self._client is None:
            raise MCPBrowserError("Playwright MCP adapter is not connected")
        result = await self._client.call_tool(name, arguments)
        if result.is_error:
            detail = "\n".join(item.text for item in result.content if getattr(item, "type", None) == "text")
            raise MCPBrowserError(detail or f"{name} returned an MCP tool error")
        return result

    async def navigate(self, url: str) -> ApplicationObservation:
        self._last = None
        await self._call("browser_navigate", {"url": url})
        return await self.observe()

    async def managed_tabs(self) -> str:
        """Read the live MCP tab list; never use its text as a persisted identity."""
        if "browser_tabs" not in self.discovered_tools:
            raise MCPToolContractError("Playwright MCP tab management is unavailable")
        result = await self._call("browser_tabs", {"action": "list"})
        return "\n".join(item.text for item in result.content
                         if getattr(item, "type", None) == "text")

    async def _page_marker(self, expression: str) -> object:
        if "browser_evaluate" not in self.discovered_tools:
            raise MCPToolContractError("Playwright MCP page identity is unavailable")
        result = await self._call("browser_evaluate", {"function": expression})
        response = "\n".join(item.text for item in result.content
                             if getattr(item, "type", None) == "text")
        match = re.search(r"(?ms)^### Result\n(.*?)(?=^### |\Z)", response)
        if match is None:
            raise MCPToolContractError("Playwright MCP page identity result is unavailable")
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            raise MCPToolContractError("Playwright MCP page identity result is invalid") from None

    async def set_managed_page_token(self, token: str) -> None:
        key = json.dumps("jobagent-v2-managed-page")
        value = json.dumps(token)
        result = await self._page_marker(
            f"() => {{ sessionStorage.setItem({key}, {value}); return true; }}")
        if result is not True:
            raise MCPToolContractError("managed page identity could not be established")

    async def managed_page_token_matches(self, token: str) -> bool:
        key = json.dumps("jobagent-v2-managed-page")
        value = json.dumps(token)
        return await self._page_marker(
            f"() => sessionStorage.getItem({key}) === {value}") is True

    async def bring_managed_tab_to_front(self) -> None:
        # Each managed adapter owns one isolated browser. Tab selection is an
        # explicit foreground action, never part of observation or lookup.
        await self._call("browser_tabs", {"action": "select", "index": 0})

    async def observe(self) -> ApplicationObservation:
        self._last = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.5 * attempt)
            result = await self._call("browser_snapshot", {})
            # Playwright MCP 0.0.82 returns a text envelope with a YAML snapshot;
            # structured_content is absent. Ignore prose outside the bounded block.
            text = "\n".join(item.text for item in result.content if getattr(item, "type", None) == "text")
            self._observation_counter += 1
            try:
                fresh = self._normalizer.normalize(text, f"observation-{self._observation_counter}")
            except SnapshotEmpty:
                if attempt == 2:
                    raise
                continue
            fresh = await self._read_current_control_states(fresh)
            if "browser_evaluate" in self.discovered_tools:
                try:
                    result = await self._call("browser_evaluate", {"function": DOM_DISCOVERY_SCRIPT})
                    content = "\n".join(item.text for item in result.content
                                        if getattr(item, "type", None) == "text")
                    match = re.search(r"(?ms)^### Result\n(.*?)(?=^### |\Z)", content)
                    if match:
                        fresh = replace(fresh, observation=merge_dom_observation(
                            fresh.observation, json.loads(match.group(1).strip())))
                except (MCPBrowserError, ValueError, TypeError):
                    # Preserve the fresh accessibility observation if the
                    # bounded supplemental read is unavailable.
                    pass
            self._last = fresh
            return fresh.observation
        raise AssertionError("snapshot retry bound was not reached")

    async def _read_current_control_states(self, fresh: NormalizedSnapshot) -> NormalizedSnapshot:
        """Read explicit DOM state on current snapshot refs; never infer from page prose."""
        if "browser_evaluate" not in self.discovered_tools:
            return fresh
        questions = list(fresh.observation.questions)
        controlled_listboxes: dict[int, str] = {}
        listbox_ids: dict[int, str] = {}
        reads = 0
        for index, question in enumerate(questions):
            if (reads >= 64 or (question.control_type not in
                                {ControlType.TEXT, ControlType.UNKNOWN, ControlType.TOGGLE} and
                                question.answer_state().satisfied and
                                question.required is not None and
                                question.answer_evidence is not AnswerEvidence.BUTTON_SELECTION) or
                    not question.target_ref or
                    question.control_type not in {ControlType.TEXT, ControlType.CHOICE,
                                                  ControlType.MULTI_CHOICE, ControlType.TOGGLE,
                                                  ControlType.UNKNOWN, ControlType.FILE,
                                                  ControlType.TYPEAHEAD, ControlType.DATE} or
                    re.search(r"password|passphrase|secret", question.label, re.I)):
                continue
            reads += 1
            function = """(element) => {
              if (!element || !element.isConnected) return null;
              const short = value => typeof value === 'string' && value.length <= 200 ? value.trim() : null;
              const role = element.getAttribute('role');
              const group = element.closest('[role=group],fieldset');
              const groupLabel = short(group?.getAttribute('aria-label') ||
                                       group?.querySelector('legend')?.textContent);
              const associatedRequired = groupLabel?.endsWith('*') === true ||
                  group?.getAttribute('aria-required') === 'true';
              const required = element.getAttribute('aria-required') === 'false' ? false :
                  element.getAttribute('aria-required') === 'true' ||
                  element.required === true || associatedRequired ||
                  element.querySelector?.('[required],[aria-required="true"]') ? true : null;
              const requiredEvidence = required !== true ? null :
                  element.required === true ? 'html_required' :
                  element.getAttribute('aria-required') === 'true' ? 'aria_required' :
                  group?.getAttribute('aria-required') === 'true' ? 'group_required' :
                  groupLabel?.endsWith('*') === true ? 'associated_required_marker' : null;
              const state = value => ({kind: 'control_state', value, required, requiredEvidence,
                                       rawRole: role || element.tagName.toLowerCase()});
              if (element.matches('input[type=checkbox]') ||
                  role === 'checkbox' || role === 'switch')
                return { ...state(element.checked === true || element.getAttribute('aria-checked') === 'true'
                  ? 'checked' : 'unchecked'), kind: 'toggle_state' };
              if (element.matches('input[type=radio]') || role === 'radio')
                return state(element.checked === true || element.getAttribute('aria-checked') === 'true'
                  ? 'checked' : 'unchecked');
              if (element.matches('select[multiple]'))
                return { ...state(null), kind: 'multi_state',
                         selectedValues: [...element.selectedOptions].map(option => short(option.textContent))
                           .filter(Boolean) };
              if (element.matches('select'))
                return state(short(element.selectedOptions?.[0]?.textContent));
              if (element.matches('input,textarea')) {
                if (element.type === 'password') return null;
                const value = short(element.value);
                const autocomplete = element.getAttribute('aria-autocomplete');
                if (autocomplete === 'list' || autocomplete === 'both')
                  return {kind: 'typeahead', value,
                          listId: element.getAttribute('aria-controls'), required};
                if (element.type === 'date' || element.type === 'month')
                  return {kind: 'date', format: element.type, value, required};
                if (element.getAttribute('placeholder') === 'MM/YYYY')
                  return {kind: 'date', format: 'MM/YYYY', value, required};
                return state(value);
              }
              const ariaValue = short(element.getAttribute('aria-valuetext'));
              if (ariaValue) return state(ariaValue);
              const selected = element.querySelector('[aria-selected="true"],[selected]');
              if (role === 'listbox')
                return {kind: 'listbox', id: element.id,
                        value: selected ? short(selected.getAttribute('aria-label') || selected.textContent) : null,
                        selectedValues: element.getAttribute('aria-multiselectable') === 'true'
                          ? [...element.querySelectorAll('[aria-selected="true"],[selected]')]
                              .map(item => short(item.getAttribute('aria-label') || item.textContent)).filter(Boolean)
                          : null,
                        required};
              if (selected) return state(short(selected.getAttribute('aria-label') || selected.textContent));
              if (element.matches('button,[role=button]')) {
                const linked = (element.getAttribute('aria-controls') ||
                                element.getAttribute('aria-owns') || '').split(/\\s+/)
                  .filter(Boolean).map(id => element.ownerDocument.getElementById(id)).filter(Boolean);
                const containers = [...linked, element.parentElement, element.parentElement?.parentElement]
                  .filter(Boolean);
                for (const container of containers) {
                  if (!linked.includes(container)) {
                    const triggers = [...container.querySelectorAll(
                      'button,[role=button],[role=combobox]')];
                    if (triggers.length !== 1 || triggers[0] !== element) continue;
                  }
                  const statuses = container.matches?.('[aria-label="items selected"]')
                    ? [container] : [...container.querySelectorAll('[aria-label="items selected"]')];
                  if (statuses.length === 0 && linked.includes(container)) {
                    const selected = [...container.querySelectorAll('[aria-selected=true]')]
                      .filter(item => item.getClientRects().length > 0);
                    if (selected.length === 1)
                      return state(short(selected[0].getAttribute('aria-label') || selected[0].textContent));
                  }
                  if (statuses.length !== 1) continue;
                  const status = statuses[0];
                  if (status.getClientRects().length === 0) continue;
                  const options = [...status.querySelectorAll('[role=option],[aria-selected=true]')];
                  if (options.length === 1)
                    return state(short(options[0].getAttribute('aria-label') || options[0].textContent));
                  if (options.length === 0 && status.children.length === 1)
                    return state(short(status.children[0].getAttribute('aria-label') || status.children[0].textContent));
                }
                return state(short(element.textContent));
              }
              return state(null);
            }"""
            try:
                result = await self._call("browser_evaluate", {
                    "element": "observed application control", "target": question.target_ref,
                    "function": function,
                })
                content = "\n".join(item.text for item in result.content
                                    if getattr(item, "type", None) == "text")
                match = re.search(r"(?ms)^### Result\n(.*?)(?=^### |\Z)", content)
                value = json.loads(match.group(1).strip()) if match else None
                if isinstance(value, dict):
                    kind = value.get("kind")
                    if isinstance(value.get("required"), bool):
                        question = replace(question, required=value["required"])
                        questions[index] = question
                    if isinstance(value.get("rawRole"), str):
                        question = replace(question, raw_role=value["rawRole"][:32])
                        questions[index] = question
                    if isinstance(value.get("requiredEvidence"), str):
                        question = replace(question, required_evidence=value["requiredEvidence"][:40])
                        questions[index] = question
                    state = value.get("value")
                    if not isinstance(state, str) or len(state) > 200:
                        state = None
                    selected_values = value.get("selectedValues")
                    if (not isinstance(selected_values, list) or len(selected_values) > 20 or
                            not all(isinstance(item, str) and len(item) <= 200
                                    for item in selected_values)):
                        selected_values = None
                    if kind == "toggle_state":
                        questions[index] = replace(question, control_type=ControlType.TOGGLE,
                                                   current_value=state,
                                                   answer_evidence=AnswerEvidence.CHECKED_STATE)
                    elif kind == "typeahead":
                        questions[index] = replace(question, control_type=ControlType.TYPEAHEAD,
                                                   current_value=state,
                                                   answer_evidence=AnswerEvidence.DOM_VALUE)
                        if isinstance(value.get("listId"), str) and 0 < len(value["listId"]) <= 128:
                            controlled_listboxes[index] = value["listId"]
                    elif kind == "date" and value.get("format") in {"date", "month", "MM/YYYY"}:
                        questions[index] = replace(question, control_type=ControlType.DATE,
                                                   date_format=value["format"],
                                                   current_value=state,
                                                   answer_evidence=AnswerEvidence.DOM_VALUE)
                    elif kind == "listbox":
                        if isinstance(value.get("id"), str) and 0 < len(value["id"]) <= 128:
                            listbox_ids[index] = value["id"]
                        if selected_values is not None:
                            questions[index] = replace(question, control_type=ControlType.MULTI_CHOICE,
                                                       selected_values=tuple(selected_values),
                                                       answer_evidence=AnswerEvidence.DOM_VALUE)
                        elif state and state.strip():
                            questions[index] = replace(question, current_value=state.strip(),
                                                       answer_evidence=AnswerEvidence.DOM_VALUE)
                    elif kind == "multi_state" and selected_values is not None:
                        questions[index] = replace(question, control_type=ControlType.MULTI_CHOICE,
                                                   selected_values=tuple(selected_values),
                                                   answer_evidence=AnswerEvidence.DOM_VALUE)
                    elif kind == "control_state":
                        observed = replace(question, current_value=state.strip() or None
                                           if state is not None else None,
                                           answer_evidence=AnswerEvidence.DOM_VALUE)
                        if (question.control_type in {ControlType.TEXT, ControlType.DATE} or
                                observed.answer_state().satisfied or
                                not question.answer_state().satisfied):
                            questions[index] = observed
                elif isinstance(value, str) and len(value) <= 200:
                    observed = replace(question, current_value=value.strip() or None,
                                       answer_evidence=AnswerEvidence.DOM_VALUE)
                    if (question.control_type in {ControlType.TEXT, ControlType.DATE} or
                            observed.answer_state().satisfied or
                            not question.answer_state().satisfied):
                        questions[index] = observed
            except (MCPBrowserError, ValueError, TypeError):
                # The snapshot remains authoritative when targeted DOM state is unavailable.
                continue
        # The accessibility snapshot can expose an ARIA suggestion list as a
        # sibling of its textbox. Prefer the live aria-controls link; a single
        # clearly named suggestion list is the bounded structural fallback.
        option_targets = dict(fresh.option_targets)
        consumed: set[int] = set()
        typeaheads = [i for i, q in enumerate(questions) if q.control_type is ControlType.TYPEAHEAD]
        suggestions = [i for i, q in enumerate(questions)
                       if q.control_type is ControlType.CHOICE and q.options and
                       q.label.casefold() in {"suggestions", "results"}]
        for index in typeaheads:
            question = questions[index]
            if question.options or not question.target_ref:
                continue
            candidates = [i for i in suggestions if i not in consumed and
                          controlled_listboxes.get(index) == listbox_ids.get(i) and
                          controlled_listboxes.get(index) is not None]
            if not candidates and len(typeaheads) == len(suggestions) == 1:
                candidates = suggestions
            if len(candidates) != 1:
                continue
            popup_index = candidates[0]
            popup = questions[popup_index]
            committed = bool(popup.current_value and question.current_value and
                             " ".join(popup.current_value.casefold().split()) ==
                             " ".join(question.current_value.casefold().split()))
            questions[index] = replace(question, options=popup.options,
                                       selection_confirmed=question.selection_confirmed or committed,
                                       answer_evidence=(AnswerEvidence.SELECTED_OPTION if committed else
                                                        question.answer_evidence))
            for option in popup.options:
                target = option_targets.pop((popup.target_ref, option), None)
                if target is not None:
                    option_targets[(question.target_ref, option)] = target
            consumed.add(popup_index)
        return replace(fresh, observation=replace(
            fresh.observation, questions=tuple(q for i, q in enumerate(questions) if i not in consumed)),
            option_targets=option_targets)

    def diagnostic_for(self, observation_id: str) -> SnapshotDiagnostic | None:
        """Return only the bounded diagnostic for the current observation."""
        if self._last is None or self._last.observation.observation_id != observation_id:
            return None
        return self._last.diagnostic

    def _check_action(self, action, session: ApplicationSession) -> ApplicationObservation:
        if self._last is None:
            raise PermissionError("fresh observation required before an action")
        current = self._last.observation
        if session.current_observation is None or session.current_observation.observation_id != current.observation_id:
            raise PermissionError("session has not recorded the current browser observation")
        if action.observation_id != current.observation_id:
            raise PermissionError("action uses a stale observation")
        ActionPolicy.authorize(action, session)
        return current

    async def _mutate(self, tool: str, args: dict, before: ApplicationObservation) -> BrowserActionResult:
        # Invalidate before MCP receives the call. Even a failed action can alter the page.
        self._last = None
        error: MCPBrowserError | None = None
        try:
            await self._call(tool, args)
        except Exception as exc:
            error = exc if isinstance(exc, MCPBrowserError) else MCPBrowserError(str(exc))
        after = await self.observe()
        old_fp, new_fp = semantic_fingerprint(before), semantic_fingerprint(after)
        if error is not None:
            status = _status_for_error(str(error))
        elif (after.validation_messages and after.validation_messages != before.validation_messages and
              (after.heading, after.progress_text) == (before.heading, before.progress_text)):
            status = ActionStatus.VALIDATION_BLOCKED
        elif old_fp != new_fp:
            status = ActionStatus.STATE_CHANGED
        else:
            status = ActionStatus.NO_PROGRESS
        return BrowserActionResult(ActionOutcome(status, str(error) if error else "", old_fp, new_fp), after)

    async def fill_text(self, action: FillText, session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        return await self._mutate("browser_type", {
            "target": action.target_ref, "text": action.answer.value, "submit": False,
        }, before)

    async def search_options(self, action: SearchOptions,
                             session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        return await self._mutate("browser_type", {
            "target": action.target_ref, "text": action.answer.value, "submit": False,
        }, before)

    async def choose_option(self, action: ChooseOption, session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        assert self._last is not None
        target = self._last.option_targets.get((action.target_ref, action.answer.value))
        if target is None:
            # A native select exposes its own current snapshot ref and visible
            # options; browser_select_option is a narrow typed MCP operation.
            question = next(q for q in before.questions if q.target_ref == action.target_ref)
            if question.discovery_source == "merged" and question.raw_role != "select":
                return await self.act_on_dom_choice(question, before.observation_id,
                                                    "choose", action.answer.value, session)
            if action.answer.value not in question.options or "browser_select_option" not in self.discovered_tools:
                raise PermissionError("option has no current browser reference")
            return await self._mutate("browser_select_option", {
                "element": "observed choice field", "target": action.target_ref,
                "values": [action.answer.value],
            }, before)
        return await self._mutate("browser_click", {"target": target}, before)

    async def toggle(self, action: Toggle, session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        if action.answer.value.casefold() not in {"yes", "true", "checked",
                                                  "no", "false", "unchecked"}:
            raise PermissionError("toggle needs an explicit boolean answer")
        question = next(q for q in before.questions if q.target_ref == action.target_ref)
        desired = ("checked" if action.answer.value.casefold() in {"yes", "true", "checked"}
                   else "unchecked")
        if question.current_value == desired:
            raise PermissionError("toggle already has the desired state")
        return await self._mutate("browser_click", {"target": action.target_ref}, before)

    async def upload_document(self, action: UploadDocument,
                              session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        path = Path(action.answer.value).expanduser()
        if (action.document_key != "documents.resume" or
                action.answer.semantic_key != "documents.resume" or
                "browser_file_upload" not in self.discovered_tools or
                not path.is_absolute() or not path.is_file() or
                path.suffix.casefold() not in {".pdf", ".doc", ".docx"}):
            raise PermissionError("configured resume upload is unavailable")
        # Clicking the observed upload control opens the chooser. The next MCP
        # call supplies only the explicit configured document. Refs are invalid
        # from the first browser mutation onward.
        self._last = None
        error = False
        try:
            await self._call("browser_click", {"target": action.target_ref})
            await self._call("browser_file_upload", {"paths": [str(path)]})
        except MCPBrowserError:
            error = True
        after = await self.observe()
        status = (ActionStatus.FAILED if error else
                  ActionStatus.STATE_CHANGED if semantic_fingerprint(before) != semantic_fingerprint(after)
                  else ActionStatus.NO_PROGRESS)
        return BrowserActionResult(ActionOutcome(status, "upload_failed" if error else ""), after)

    async def reveal_options(self, action: RevealOptions,
                             session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        return await self._mutate("browser_click", {"target": action.target_ref}, before)

    async def act_on_dom_choice(self, question, observation_id: str, mode: str,
                                value: str | None, session: ApplicationSession) -> BrowserActionResult:
        """Act on one current DOM-recovered selector without creating a durable ref."""
        if self._last is None or self._last.observation.observation_id != observation_id or \
                session.current_observation is None or session.current_observation.observation_id != observation_id:
            raise PermissionError("DOM choice action requires the current observation")
        before = self._last.observation
        matches = [q for q in before.questions if q.identity() == question.identity()]
        if (len(matches) != 1 or matches[0].discovery_source not in {"dom_fallback", "merged"} or
                matches[0].control_type not in {ControlType.CHOICE, ControlType.TYPEAHEAD} or
                mode not in {"open", "search", "choose"} or
                (mode == "choose" and (not value or
                 sum(" ".join(option.casefold().split()) == " ".join(value.casefold().split())
                     for option in matches[0].options) != 1)) or
                (mode == "search" and not value)):
            raise PermissionError("DOM choice target or exact option is unavailable")
        label = json.dumps(question.label)
        operation = json.dumps(mode)
        answer = json.dumps(value)
        script = f"""() => {{
          const label = {label}, mode = {operation}, answer = {answer};
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const visible = node => node && node.getClientRects().length > 0;
          const root = document.querySelector('main,[role="main"]') ||
                       document.querySelector('form') || document.body;
          const labels = [...root.querySelectorAll('label,legend,[aria-label]')]
            .filter(node => visible(node) && norm(node.getAttribute('aria-label') ||
              (node.matches('label,legend') ? node.textContent : ''))
              .replace(/\\s*\\*\\s*$/, '') === norm(label));
          const fields = [];
          for (const node of labels) {{
            for (let field = node.parentElement, depth = 0; field && depth < 5;
                 field = field.parentElement, depth++) {{
              const inputs = [...field.querySelectorAll('input:not([type=hidden]),[role=combobox]')]
                .filter(visible);
              const triggers = [...field.querySelectorAll('[aria-haspopup=listbox],button,[role=button]')]
                .filter(item => visible(item) && !item.closest('[role=option],[class*=token],[class*=chip]'));
              if (inputs.length + triggers.length > 0 && inputs.length + triggers.length <= 3) {{
                fields.push(field); break;
              }}
            }}
          }}
          const unique = [...new Set(fields)];
          if (unique.length !== 1) return false;
          const field = unique[0];
          const input = [...field.querySelectorAll('input:not([type=hidden]),[role=combobox]')]
            .find(visible);
          const trigger = [...field.querySelectorAll('[aria-haspopup=listbox],button,[role=button]')]
            .find(node => visible(node) && !node.closest('[role=option],[class*=token],[class*=chip]'));
          if (mode === 'open') {{ (trigger || input)?.click(); return !!(trigger || input); }}
          if (mode === 'search') {{
            if (!input || !('value' in input)) return false;
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (!setter) return false;
            input.focus(); setter.call(input, answer);
            input.dispatchEvent(new Event('input', {{bubbles: true}})); return true;
          }}
          const ids = [input, trigger].filter(Boolean).flatMap(node =>
            (node.getAttribute('aria-controls') || '').split(/\\s+/)).filter(Boolean);
          const roots = [field, ...ids.map(id => document.getElementById(id)).filter(Boolean)];
          const options = [...new Set(roots.flatMap(node => [...node.querySelectorAll('[role=option]')]))]
            .filter(node => visible(node) && !node.closest('[aria-label="items selected"]') &&
              norm(node.getAttribute('aria-label') || node.textContent) === norm(answer));
          if (options.length !== 1) return false;
          options[0].click(); return true;
        }}"""
        self._last = None
        result = await self._call("browser_evaluate", {"function": script})
        content = "\n".join(item.text for item in result.content
                            if getattr(item, "type", None) == "text")
        succeeded = bool(re.search(r"(?m)^true\s*$", content))
        after = await self.observe()
        status = (ActionStatus.STATE_CHANGED if succeeded else ActionStatus.TARGET_MISSING)
        return BrowserActionResult(ActionOutcome(status), after)

    async def activate_navigation(self, action: Advance | GoBack,
                                  session: ApplicationSession) -> BrowserActionResult:
        if not isinstance(action, (Advance, GoBack)):
            raise PermissionError("only typed non-submit navigation is exposed")
        before = self._check_action(action, session)
        return await self._mutate("browser_click", {"target": action.target_ref}, before)

    async def annotate_field(self, target_ref: str, observation_id: str,
                             state: str) -> None:
        """Presentation only, addressed by one current snapshot ref."""
        if state not in {"verified", "needs-review", "clear"}:
            raise ValueError("unknown Job Agent annotation")
        if self._last is None or self._last.observation.observation_id != observation_id:
            raise PermissionError("annotation requires the current observation")
        matches = [q for q in self._last.observation.questions if q.target_ref == target_ref]
        if len(matches) != 1 or not target_ref:
            raise PermissionError("annotation target is missing or ambiguous")
        # Only the fixed enum is interpolated. The page label and value are never
        # executable input. The site keeps its own inline style and field value.
        function = """(element) => {
          if (!element || !element.isConnected) return false;
          let style = document.getElementById('jobagent-review-style');
          if (!style) {
            style = document.createElement('style');
            style.id = 'jobagent-review-style';
            style.textContent = '[data-jobagent-review-state="verified"] { outline: 3px solid #16803c !important; outline-offset: 2px !important; } [data-jobagent-review-state="needs-review"] { outline: 3px solid #c52222 !important; outline-offset: 2px !important; }';
            document.head.appendChild(style);
          }
          if (STATE === 'clear') element.removeAttribute('data-jobagent-review-state');
          else element.setAttribute('data-jobagent-review-state', STATE);
          return true;
        }""".replace("STATE", json.dumps(state))
        result = await self._call("browser_evaluate", {
            "element": "observed application field", "target": target_ref, "function": function,
        })
        content = "\n".join(item.text for item in result.content
                            if getattr(item, "type", None) == "text")
        if not re.search(r"(?m)^true\s*$", content):
            raise MCPToolContractError("field annotation was not confirmed")

    def _login_observation(self, observation_id: str) -> ApplicationObservation:
        if self._last is None or self._last.observation.observation_id != observation_id:
            raise PermissionError("login action requires a fresh matching observation")
        observation = self._last.observation
        heading = (observation.heading or "").casefold()
        if observation.review_like or not any(term in heading for term in ("sign in", "log in", "login")):
            raise PermissionError("login action requires an unambiguous login page")
        return observation

    async def _login_mutate(self, tool: str, arguments: dict) -> ApplicationObservation:
        self._last = None
        try:
            await self._call(tool, arguments)
        except Exception:
            raise MCPBrowserError("login browser action failed") from None
        return await self.observe()

    async def fill_login_identity(self, target_ref: str, observation_id: str,
                                  username: str) -> ApplicationObservation:
        observation = self._login_observation(observation_id)
        matches = [q for q in observation.questions if q.target_ref == target_ref and
                   q.control_type is ControlType.TEXT and q.label.strip().casefold() in
                   {"email", "email address", "username", "user name"}]
        if len(matches) != 1 or not username:
            raise PermissionError("login identity target is missing or ambiguous")
        return await self._login_mutate("browser_type", {
            "target": target_ref, "text": username, "submit": False})

    async def fill_login_password(self, target_ref: str, observation_id: str,
                                  password: str) -> ApplicationObservation:
        observation = self._login_observation(observation_id)
        matches = [q for q in observation.questions if q.target_ref == target_ref and
                   q.control_type is ControlType.SECRET]
        if len(matches) != 1 or not password:
            raise PermissionError("login password target is missing or ambiguous")
        return await self._login_mutate("browser_type", {
            "target": target_ref, "text": password, "submit": False})

    async def activate_login(self, target_ref: str, observation_id: str) -> ApplicationObservation:
        observation = self._login_observation(observation_id)
        sign_ins = [control for control in observation.navigation_controls if
                    control.label.strip().casefold() in {"sign in", "log in", "login"}]
        if (len(sign_ins) != 1 or sign_ins[0].target_ref != target_ref or
                any(control.kind is NavigationKind.SUBMIT for control in observation.navigation_controls)):
            raise PermissionError("login control is missing, ambiguous, or terminal")
        return await self._login_mutate("browser_click", {"target": target_ref})
