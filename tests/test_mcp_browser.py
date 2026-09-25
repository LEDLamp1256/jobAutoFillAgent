import unittest
from types import SimpleNamespace

from jobagent.domain import (
    Advance, Answer, AnswerScope, AnswerSource, ApplicationSession,
    FillText, Submit,
)
from jobagent.mcp_browser import (
    MCPServerCommand, MCPToolContractError, PlaywrightMCPAdapter, validate_tool_contract,
)
from jobagent.snapshot import SnapshotNormalizer
from tests.test_snapshot import STEP_1, REVIEW


def tool(name, properties):
    types = {"submit": "boolean"}
    return SimpleNamespace(name=name, input_schema={"properties": {
        key: {"type": types.get(key, "string")} for key in properties}})


TOOLS = [tool("browser_navigate", {"url"}), tool("browser_snapshot", set()),
         tool("browser_type", {"target", "text", "submit"}),
         tool("browser_click", {"target"}), tool("browser_close", set())]


class FakeClient:
    def __init__(self, parameters, *, tools=TOOLS, snapshots=None, tool_error=None):
        self.parameters = parameters
        self.tools = tools
        self.snapshots = list(snapshots or [STEP_1])
        self.tool_error = tool_error
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "browser_snapshot":
            text = self.snapshots.pop(0)
            return SimpleNamespace(is_error=False, content=[SimpleNamespace(type="text", text=text)])
        if self.tool_error and name == "browser_type":
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(type="text", text=self.tool_error)])
        return SimpleNamespace(is_error=False, content=[])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_check_rejects_missing_or_changed_schema(self):
        with self.assertRaises(MCPToolContractError):
            validate_tool_contract(TOOLS[:-1])
        changed = [tool("browser_type", {"target", "text"}) if t.name == "browser_type" else t
                   for t in TOOLS]
        with self.assertRaises(MCPToolContractError):
            validate_tool_contract(changed)

    async def test_navigation_and_fill_map_to_narrow_tools_with_fresh_snapshot(self):
        fake = FakeClient(None, snapshots=[STEP_1, STEP_1.replace('textbox "First name" [ref=e8]',
                                                              'textbox "First name" [ref=e8]: Ada')])
        adapter = PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                       client_factory=lambda parameters: fake)
        async with adapter:
            session = ApplicationSession("app", "/job")
            observation = await adapter.navigate("http://local/fixture")
            session.record_observation(observation)
            question = observation.questions[0]
            action = FillText(question.target_ref, observation.observation_id,
                              Answer("first_name", "Ada", AnswerSource.CANDIDATE_PROFILE,
                                     AnswerScope.GLOBAL))
            result = await adapter.fill_text(action, session)
            self.assertEqual(result.observation.questions[0].current_value, "Ada")
            self.assertEqual(fake.calls[0], ("browser_navigate", {"url": "http://local/fixture"}))
            self.assertEqual(fake.calls[2], ("browser_type", {"target": "e8", "text": "Ada",
                                                               "submit": False}))
            self.assertEqual(fake.calls[3][0], "browser_snapshot")
        self.assertTrue(fake.closed)
        self.assertEqual(fake.calls[-1][0], "browser_close")

    async def test_stale_action_is_rejected_before_mcp_call(self):
        fake = FakeClient(None, snapshots=[STEP_1, STEP_1])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            session = ApplicationSession("app", "/job")
            old = await adapter.observe(); session.record_observation(old)
            fresh = await adapter.observe(); session.record_observation(fresh)
            before_calls = len(fake.calls)
            action = FillText("e8", old.observation_id,
                              Answer("first_name", "Ada", AnswerSource.CANDIDATE_PROFILE,
                                     AnswerScope.GLOBAL))
            with self.assertRaises(PermissionError):
                await adapter.fill_text(action, session)
            self.assertEqual(len(fake.calls), before_calls)

    async def test_normal_navigation_rejects_submit_without_click(self):
        fake = FakeClient(None, snapshots=[REVIEW])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        normalizer=SnapshotNormalizer(),
                                        client_factory=lambda _: fake) as adapter:
            session = ApplicationSession("app", "/job")
            observation = await adapter.observe(); session.record_observation(observation)
            submit = next(c for c in observation.navigation_controls if c.label == "Submit application")
            calls = len(fake.calls)
            with self.assertRaises(PermissionError):
                await adapter.activate_navigation(Advance(submit.target_ref, observation.observation_id), session)
            with self.assertRaises(PermissionError):
                await adapter.activate_navigation(Submit(submit.target_ref, observation.observation_id), session)
            self.assertEqual(len(fake.calls), calls)

    async def test_tool_error_is_translated_and_observation_refreshed(self):
        fake = FakeClient(None, snapshots=[STEP_1, STEP_1], tool_error="Target not found")
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            session = ApplicationSession("app", "/job")
            observation = await adapter.observe(); session.record_observation(observation)
            action = FillText("e8", observation.observation_id,
                              Answer("first_name", "Ada", AnswerSource.CANDIDATE_PROFILE,
                                     AnswerScope.GLOBAL))
            result = await adapter.fill_text(action, session)
            self.assertEqual(result.outcome.status.value, "target_missing")
            self.assertNotEqual(result.observation.observation_id, observation.observation_id)

    async def test_contract_failure_closes_client_process(self):
        fake = FakeClient(None, tools=TOOLS[:-1])
        adapter = PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                       client_factory=lambda _: fake)
        with self.assertRaises(MCPToolContractError):
            await adapter.__aenter__()
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
