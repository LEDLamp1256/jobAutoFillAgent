"""Playwright MCP 0.0.82 adapter over the official Python MCP stdio client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from jobagent.browser import BrowserActionResult
from jobagent.domain import (
    ActionOutcome, ActionPolicy, ActionStatus, Advance, ApplicationObservation,
    ApplicationSession, ChooseOption, ControlType, FillText, GoBack, NavigationKind,
    semantic_fingerprint,
)
from jobagent.snapshot import NormalizedSnapshot, SnapshotDiagnostic, SnapshotNormalizer


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
        self._last: NormalizedSnapshot | None = None
        self._observation_counter = 0
        self.discovered_tools: tuple[str, ...] = ()

    async def __aenter__(self) -> PlaywrightMCPAdapter:
        parameters = StdioServerParameters(
            command=self._server.command, args=list(self._server.args), cwd=self._server.cwd,
        )
        self._client_context = self._client_factory(parameters)
        self._client = await self._client_context.__aenter__()
        try:
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
            return
        try:
            if client is not None and "browser_close" in self.discovered_tools:
                await client.call_tool("browser_close", {})
        finally:
            await context.__aexit__(None, None, None)

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

    async def observe(self) -> ApplicationObservation:
        result = await self._call("browser_snapshot", {})
        # Playwright MCP 0.0.82 returns a text envelope with a YAML snapshot;
        # structured_content is absent. Ignore any prose outside the bounded block.
        text = "\n".join(item.text for item in result.content if getattr(item, "type", None) == "text")
        self._observation_counter += 1
        self._last = self._normalizer.normalize(text, f"observation-{self._observation_counter}")
        return self._last.observation

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

    async def choose_option(self, action: ChooseOption, session: ApplicationSession) -> BrowserActionResult:
        before = self._check_action(action, session)
        assert self._last is not None
        target = self._last.option_targets.get((action.target_ref, action.answer.value))
        if target is None:
            raise PermissionError("option has no current browser reference")
        return await self._mutate("browser_click", {"target": target}, before)

    async def activate_navigation(self, action: Advance | GoBack,
                                  session: ApplicationSession) -> BrowserActionResult:
        if not isinstance(action, (Advance, GoBack)):
            raise PermissionError("only typed non-submit navigation is exposed")
        before = self._check_action(action, session)
        return await self._mutate("browser_click", {"target": action.target_ref}, before)

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
