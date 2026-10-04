import unittest
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jobagent.domain import (
    Advance, Answer, AnswerScope, AnswerSource, ApplicationObservation, ApplicationSession,
    ControlType, QuestionObservation,
    FillText, RevealOptions, Submit, Toggle, UploadDocument,
)
from jobagent.mcp_browser import (
    MCPServerCommand, MCPToolContractError, PlaywrightMCPAdapter, validate_tool_contract,
)
from jobagent.snapshot import NormalizedSnapshot, SnapshotAccessChallenge, SnapshotEmpty, SnapshotNormalizer
from tests.test_snapshot import STEP_1, REVIEW


def tool(name, properties):
    types = {"submit": "boolean", "values": "array"}
    return SimpleNamespace(name=name, input_schema={"properties": {
        key: {"type": types.get(key, "string")} for key in properties}})


TOOLS = [tool("browser_navigate", {"url"}), tool("browser_snapshot", set()),
         tool("browser_type", {"target", "text", "submit"}),
         tool("browser_click", {"target"}), tool("browser_close", set()),
         tool("browser_evaluate", {"element", "target", "function"}),
         tool("browser_select_option", {"target", "values"})]

LOGIN = '''### Page
- Page URL: https://example.test/login
### Snapshot
```yaml
- main [ref=e2]:
  - heading "Sign In" [level=1] [ref=e3]
  - textbox "Email Address" [ref=e4]
  - textbox "Password" [ref=e5]
  - button "Sign In" [ref=e6]
```
'''

EMPTY = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml

```
'''


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
    async def test_unknown_on_value_is_rechecked_as_unchecked_checkbox(self):
        class CheckboxClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n{"kind":"toggle_state","value":"unchecked",'
                                          '"rawRole":"input","required":false}')])
                return await super().call_tool(name, args)

        fake = CheckboxClient(None)
        question = QuestionObservation("I have a preferred name", ControlType.UNKNOWN,
            current_value="on", required=False, target_ref="e3")
        fresh = NormalizedSnapshot(ApplicationObservation(
            "fresh", "https://example.test/apply", "Application", questions=(question,)), {})
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            updated = await adapter._read_current_control_states(fresh)
        actual = updated.observation.questions[0]
        self.assertEqual(actual.control_type, ControlType.TOGGLE)
        self.assertFalse(actual.answer_state().satisfied)
        self.assertEqual([name for name, _ in fake.calls], ["browser_evaluate", "browser_close"])

    async def test_targeted_checkbox_ignores_default_on_value_when_unchecked(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - checkbox "I have a preferred name" [ref=e3]
```
'''

        class CheckboxClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate" and "target" in args:
                    self.calls.append((name, args))
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n{"kind":"toggle_state","value":"unchecked",'
                                          '"rawRole":"input","required":false}')])
                return await super().call_tool(name, args)

        fake = CheckboxClient(None, snapshots=[page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            question = (await adapter.observe()).questions[0]
            self.assertEqual(question.control_type.value, "toggle")
            self.assertEqual(question.current_value, "unchecked")
            self.assertFalse(question.answer_state().satisfied)

    async def test_multiselect_dom_state_recovers_selection_absent_from_snapshot(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - listbox "Skills" [multiple] [ref=e3]:
    - option "Example skill" [ref=e4]
```
'''

        class SelectedClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    if "target" not in args:
                        return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                            type="text", text='### Result\n{"candidates":[]}')])
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n{"kind":"multi_state","selectedValues":["Example skill"]}')])
                return await super().call_tool(name, args)

        fake = SelectedClient(None, snapshots=[page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observed = await adapter.observe()
            question = observed.questions[0]
            self.assertEqual(question.selected_values, ("Example skill",))
            self.assertTrue(question.answer_state().satisfied)
        self.assertEqual([name for name, _ in fake.calls],
                         ["browser_snapshot", "browser_evaluate", "browser_evaluate", "browser_close"])

    async def test_known_resume_upload_uses_configured_synthetic_file_and_verifies(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - button "Upload Resume" [required] [ref=e3]
```
'''
        class UploadClient(FakeClient):
            uploaded = False

            async def call_tool(self, name, args):
                if name == "browser_file_upload":
                    self.calls.append((name, args))
                    self.uploaded = True
                    return SimpleNamespace(is_error=False, content=[])
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n"synthetic.pdf"' if self.uploaded
                        else '### Result\nnull')])
                return await super().call_tool(name, args)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"%PDF-1.4 synthetic fixture")
            fake = UploadClient(None, tools=[*TOOLS, tool("browser_file_upload", {"paths"})],
                                snapshots=[page, page])
            async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                            client_factory=lambda _: fake) as adapter:
                observed = await adapter.observe()
                self.assertEqual(observed.questions[0].control_type.value, "file")
                session = ApplicationSession("app", observed.location)
                session.record_observation(observed)
                answer = Answer("documents.resume", str(path), AnswerSource.CANDIDATE_PROFILE,
                                AnswerScope.GLOBAL)
                action = UploadDocument("e3", observed.observation_id, "documents.resume", answer)
                result = await adapter.upload_document(action, session)
                self.assertEqual(result.observation.questions[0].current_value, "synthetic.pdf")
                with self.assertRaises(PermissionError):
                    await adapter.upload_document(action, session)
            self.assertEqual([name for name, _ in fake.calls if name in {
                "browser_click", "browser_file_upload"}], ["browser_click", "browser_file_upload"])
            self.assertEqual(next(args["paths"] for name, args in fake.calls
                                  if name == "browser_file_upload"), [str(path)])

    async def test_toggle_uses_current_ref_then_verifies_fresh_checked_state(self):
        first = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - checkbox "Are you authorized to work in the US?" [required] [ref=e3]
```
'''
        second = first.replace('[required] [ref=e3]', '[checked] [required] [ref=e8]')
        fake = FakeClient(None, snapshots=[first, second])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observed = await adapter.observe()
            session = ApplicationSession("application-1", observed.location)
            session.record_observation(observed)
            answer = Answer("employment.us_authorized", "Yes", AnswerSource.CANDIDATE_PROFILE,
                            AnswerScope.GLOBAL)
            action = Toggle(observed.questions[0].target_ref, observed.observation_id, answer)
            result = await adapter.toggle(action, session)
            self.assertEqual(result.observation.questions[0].current_value, "checked")
            with self.assertRaises(PermissionError):
                await adapter.toggle(action, session)
        self.assertEqual([name for name, _ in fake.calls].count("browser_click"), 1)

    async def test_fresh_targeted_state_read_recovers_manual_choice_without_page_text(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - combobox "How Did You Hear About Us?" [required] [ref=e3]: Select One
```
'''
        class StateClient(FakeClient):
            value = None

            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    if "target" not in args:
                        return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                            type="text", text='### Result\n{"candidates":[]}')])
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text="### Result\n" + json.dumps(self.value))])
                return await super().call_tool(name, args)

        fake = StateClient(None, snapshots=[page, page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            first = await adapter.observe()
            self.assertEqual(first.questions[0].current_value, "Select One")
            fake.value = "Employee Referral"
            second = await adapter.observe()
            self.assertEqual(second.questions[0].current_value, "Employee Referral")
            self.assertNotEqual(first.observation_id, second.observation_id)
            self.assertEqual(first.questions[0].identity(), second.questions[0].identity())
        self.assertEqual([name for name, args in fake.calls
                          if name == "browser_evaluate" and "target" in args], 2 * ["browser_evaluate"])
        self.assertEqual([name for name, args in fake.calls
                          if name == "browser_evaluate" and "target" not in args],
                         2 * ["browser_evaluate"])
        self.assertFalse(any(name == "browser_click" for name, _ in fake.calls))

    async def test_button_backed_selected_value_is_read_without_any_browser_action(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - generic: Country / Territory Phone Code *
  - button "Country / Territory Phone Code" [haspopup=listbox] [ref=e3]
```
'''
        class SelectedClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    if "target" not in args:
                        return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                            type="text", text='### Result\n{"candidates":[]}')])
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n"Exampleland (+9)"')])
                return await super().call_tool(name, args)

        fake = SelectedClient(None, snapshots=[page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observed = await adapter.observe()
            question = observed.questions[0]
            self.assertTrue(question.required)
            self.assertEqual(question.current_value, "Exampleland (+9)")
            self.assertEqual(question.answer_evidence.value, "dom_value")
            self.assertTrue(question.answer_state().satisfied)
        self.assertEqual([name for name, _ in fake.calls],
                         ["browser_snapshot", "browser_evaluate", "browser_evaluate", "browser_close"])

    async def test_targeted_empty_text_clears_misleading_snapshot_value(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - textbox "State" [required] [ref=e3]: California
```
'''
        class EmptyClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text='### Result\n""')])
                return await super().call_tool(name, args)
        fake = EmptyClient(None, snapshots=[page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            question = (await adapter.observe()).questions[0]
            self.assertIsNone(question.current_value)
            self.assertFalse(question.answer_state().satisfied)

    async def test_committed_typeahead_matches_linked_selected_suggestion(self):
        page = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - textbox "Committed school" [ref=e3]: Example College
  - listbox "Suggestions" [ref=e4]:
    - option "Example College" [selected] [ref=e5]
```
'''
        class LinkedClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    if "target" not in args:
                        return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                            type="text", text='### Result\n{"candidates":[]}')])
                    value = ({"kind": "typeahead", "value": "Example College", "listId": "suggestions"}
                             if args["target"] == "e3" else
                             {"kind": "listbox", "value": "Example College", "id": "suggestions"})
                    return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                        type="text", text="### Result\n" + json.dumps(value))])
                return await super().call_tool(name, args)
        fake = LinkedClient(None, snapshots=[page])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observed = await adapter.observe()
            self.assertEqual(len(observed.questions), 1)
            question = observed.questions[0]
            self.assertEqual(question.options, ("Example College",))
            self.assertTrue(question.selection_confirmed)
            self.assertTrue(question.answer_state().satisfied)

    async def test_closed_choice_reveal_clicks_once_and_reobserves_without_answer(self):
        closed = '''### Page
- Page URL: https://example.test/apply
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Application" [level=2] [ref=e2]
  - generic: State*
  - button "Select One" [haspopup=listbox] [ref=e3]
```
'''
        opened = closed.replace('  - button "Select One" [haspopup=listbox] [ref=e3]',
            '  - button "Select One" [haspopup=listbox] [ref=e3]\n'
            '  - listbox [ref=e4]:\n    - option "California" [ref=e5]')
        fake = FakeClient(None, snapshots=[closed, opened])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            session = ApplicationSession("app", "https://example.test/apply")
            first = await adapter.observe(); session.record_observation(first)
            action = RevealOptions(first.questions[0].target_ref, first.observation_id)
            result = await adapter.reveal_options(action, session)
            self.assertEqual(result.observation.questions[0].options, ("California",))
            with self.assertRaises(PermissionError):
                await adapter.reveal_options(action, session)
        self.assertEqual([name for name, _ in fake.calls if name != "browser_evaluate"],
                         ["browser_snapshot", "browser_click", "browser_snapshot", "browser_close"])

    async def test_empty_snapshot_retries_fresh_observation_without_repeating_navigation(self):
        fake = FakeClient(None, snapshots=[EMPTY, STEP_1])
        with patch("jobagent.mcp_browser.asyncio.sleep", new_callable=AsyncMock) as sleep:
            async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                            client_factory=lambda _: fake) as adapter:
                observation = await adapter.navigate("https://example.test/apply")
                self.assertEqual(observation.observation_id, "observation-2")
                self.assertEqual(observation.heading, "Basic information")
                self.assertIsNotNone(adapter.diagnostic_for(observation.observation_id))
        self.assertEqual([name for name, _ in fake.calls if name != "browser_evaluate"],
                         ["browser_navigate", "browser_snapshot", "browser_snapshot", "browser_close"])
        sleep.assert_awaited_once_with(0.5)

    async def test_persistent_empty_snapshot_has_bounded_safe_failure(self):
        fake = FakeClient(None, snapshots=[EMPTY, EMPTY, EMPTY])
        with patch("jobagent.mcp_browser.asyncio.sleep", new_callable=AsyncMock) as sleep:
            async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                            client_factory=lambda _: fake) as adapter:
                with self.assertRaises(SnapshotEmpty):
                    await adapter.navigate("https://example.test/apply")
                self.assertIsNone(adapter._last)
        self.assertEqual([name for name, _ in fake.calls],
                         ["browser_navigate", "browser_snapshot", "browser_snapshot",
                          "browser_snapshot", "browser_close"])
        self.assertEqual([call.args for call in sleep.await_args_list], [(0.5,), (1.0,)])

    async def test_access_challenge_is_not_retried(self):
        fake = FakeClient(None, snapshots=[LOGIN.replace("Sign In", "Verify you are human")])
        with patch("jobagent.mcp_browser.asyncio.sleep", new_callable=AsyncMock) as sleep:
            async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                            client_factory=lambda _: fake) as adapter:
                with self.assertRaises(SnapshotAccessChallenge):
                    await adapter.navigate("https://example.test/apply")
        self.assertEqual([name for name, _ in fake.calls],
                         ["browser_navigate", "browser_snapshot", "browser_close"])
        sleep.assert_not_awaited()

    async def test_annotation_ref_shared_by_two_fields_is_rejected(self):
        duplicate = STEP_1.replace('textbox "Last name" [ref=e9]',
                                   'textbox "Last name" [ref=e8]')
        fake = FakeClient(None, snapshots=[duplicate])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observation = await adapter.observe()
            before = len(fake.calls)
            with self.assertRaises(PermissionError):
                await adapter.annotate_field("e8", observation.observation_id, "needs-review")
            self.assertEqual(len(fake.calls), before)

    async def test_annotation_uses_only_current_unique_field_ref_and_no_form_action(self):
        class AnnotationClient(FakeClient):
            async def call_tool(self, name, args):
                if name == "browser_evaluate":
                    self.calls.append((name, args))
                    return SimpleNamespace(is_error=False, content=[
                        SimpleNamespace(type="text", text="### Result\ntrue")])
                return await super().call_tool(name, args)
        fake = AnnotationClient(None, snapshots=[STEP_1, STEP_1])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            first = await adapter.observe()
            await adapter.annotate_field("e8", first.observation_id, "verified")
            await adapter.annotate_field("e8", first.observation_id, "verified")
            await adapter.annotate_field("e8", first.observation_id, "needs-review")
            with self.assertRaises(PermissionError):
                await adapter.annotate_field("e11", first.observation_id, "verified")
            second = await adapter.observe()
            with self.assertRaises(PermissionError):
                await adapter.annotate_field("e8", first.observation_id, "verified")
            await adapter.annotate_field("e8", second.observation_id, "clear")
        calls = [args for name, args in fake.calls if name == "browser_evaluate" and
                 "jobagent-review-style" in args.get("function", "")]
        self.assertEqual(len(calls), 4)
        self.assertEqual({args["target"] for args in calls}, {"e8"})
        self.assertEqual({name for name, _ in fake.calls} - {
            "browser_snapshot", "browser_evaluate", "browser_close"}, set())

    async def test_page_marker_uses_bounded_evaluation_result(self):
        class MarkerClient(FakeClient):
            async def call_tool(self, name, args):
                self.calls.append((name, args))
                if name == "browser_evaluate":
                    return SimpleNamespace(is_error=False, content=[
                        SimpleNamespace(type="text", text="### Result\ntrue")])
                return SimpleNamespace(is_error=False, content=[])
        fake = MarkerClient(None)
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            await adapter.set_managed_page_token("synthetic-page-token")
            self.assertTrue(await adapter.managed_page_token_matches("synthetic-page-token"))
        self.assertEqual([name for name, _ in fake.calls].count("browser_evaluate"), 2)

    async def test_login_actions_are_typed_reobserved_and_not_routine_submit(self):
        fake = FakeClient(None, snapshots=[LOGIN, LOGIN, LOGIN, STEP_1])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            first = await adapter.observe()
            second = await adapter.fill_login_identity("e4", first.observation_id, "user@example.test")
            with self.assertRaises(PermissionError):
                await adapter.fill_login_password("e5", first.observation_id, "synthetic-test-secret")
            third = await adapter.fill_login_password("e5", second.observation_id,
                                                      "synthetic-test-secret")
            after = await adapter.activate_login("e6", third.observation_id)
            self.assertEqual(after.heading, "Basic information")
            self.assertEqual([name for name, _ in fake.calls if name in {"browser_type", "browser_click"}],
                             ["browser_type", "browser_type", "browser_click"])
            self.assertNotIn("synthetic-test-secret", repr(third))

    async def test_login_method_rejects_review_submit(self):
        fake = FakeClient(None, snapshots=[REVIEW])
        async with PlaywrightMCPAdapter(MCPServerCommand("node", ("cli.js",)),
                                        client_factory=lambda _: fake) as adapter:
            observation = await adapter.observe()
            before = len(fake.calls)
            with self.assertRaises(PermissionError):
                await adapter.activate_login("e44", observation.observation_id)
            self.assertEqual(len(fake.calls), before)

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
            actions = [(name, args) for name, args in fake.calls if name != "browser_evaluate"]
            self.assertEqual(actions[2], ("browser_type", {"target": "e8", "text": "Ada",
                                                             "submit": False}))
            self.assertEqual(actions[3][0], "browser_snapshot")
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
            self.assertIsNone(adapter.diagnostic_for(old.observation_id))
            self.assertIsNotNone(adapter.diagnostic_for(fresh.observation_id))

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


class MCPStderrBoundaryTests(unittest.TestCase):
    def test_mcp_process_stderr_cannot_echo_synthetic_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / "server.py"
            server.write_text("""import sys
from mcp.server.mcpserver import MCPServer
server = MCPServer('synthetic-stderr-server')
@server.tool()
def echo(text: str) -> str:
    print(text, file=sys.stderr, flush=True)
    return 'received'
server.run()
""")
            client = Path(temporary) / "client.py"
            client.write_text("""import asyncio
import sys
import jobagent.mcp_browser as browser_module
from jobagent.mcp_browser import MCPServerCommand, PlaywrightMCPAdapter
browser_module.validate_tool_contract = lambda tools: None
async def main():
    async with PlaywrightMCPAdapter(MCPServerCommand(sys.executable, (sys.argv[1],))) as browser:
        await browser._call('echo', {'text': sys.argv[2]})
asyncio.run(main())
""")
            sentinel = "synthetic-mcp-stderr-sentinel-02"
            result = subprocess.run([sys.executable, str(client), str(server), sentinel],
                                    capture_output=True, text=True, timeout=25,
                                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(sentinel, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
