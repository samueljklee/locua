"""Actual Amplifier retention/count integration; CPU fake service, no model/GUI."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ExecutionStateSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_facts_survive_measured_compaction_without_rewriting_history(self):
        try:
            import amplifier_core
            import amplifier_module_context_simple
            import amplifier_module_loop_streaming
        except ImportError:
            self.skipTest("Optional Amplifier extra absent")
        from amplifier_core.models import ToolResult
        from locua.amplifier_provider import LocalAmplifierProvider, _hash, native_request
        from locua.amplifier_session import execute_session, SYSTEM
        from locua.arithmetic_input import InputWitness
        from locua.execution_state import render_execution_facts

        original = "Update the reviewed entry exactly, preserve its neighbor, and verify both."
        owner = SimpleNamespace(
            evidence={"request_sha256": _hash(original), "events": [], "verification": {}},
            _scopes={}, _latest={}, _observations={}, _window_records={}, _cancellation=None,
            exploration=SimpleNamespace(needs=[]))
        reminders = []
        produced_results = []
        services = []

        def facts():
            value = render_execution_facts(owner)
            reminders.append(value)
            return value

        class CountingService:
            """Exact serialized-view fixture count, not a tokenizer benchmark."""
            def __init__(self, **kwargs):
                self.counted = []; self.generated = []; self.closed = False
                services.append(self); kwargs["on_started"](self)

            def info(self):
                return {"test_double": True, "counts": "serialized characters/4; no model/tokenizer"}

            def size(self, messages, tools):
                return len(json.dumps({"messages": messages, "tools": tools},
                    ensure_ascii=False, separators=(",", ":"))) // 4 + 1

            def count(self, messages, tools):
                self.counted.append(deepcopy((messages, tools)))
                return {"input_tokens": self.size(messages, tools), "output_tokens": 0,
                    "generation_calls": 0, "tokenizer_only": True,
                    "request_sha256": _hash({"messages": messages, "tools": tools})}

            def generate(self, messages, tools, **kwargs):
                payload = deepcopy((messages, tools))
                if payload != self.counted[-1]:
                    raise AssertionError("Dispatched view differs from final measured view")
                self.generated.append(payload)
                number = len(self.generated)
                output = (f"<tool_call>\n<function=read_page>\n<parameter=page>\n{number}\n"
                          "</parameter>\n</function>\n</tool_call>" if number <= 3 else
                          "The evidence remains incomplete; no completion is claimed.")
                return {"raw_output": output, "finish_reason": "stop", "generation_calls": 1,
                    "request_id": "fake-" + str(number), "dispatched": False,
                    "usage": {"input_tokens": self.size(messages, tools), "output_tokens": 10},
                    "timing": {"generation_ms": 0.1, "worker_total_ms": 0.2}, "model_info": self.info()}

            def close(self):
                self.closed = True

        class Tool:
            name = "read_page"
            description = "Read a synthetic evidence page; never edit an application."
            input_schema = {"type": "object", "properties": {"page": {"type": "integer"}},
                            "required": ["page"]}

            async def execute(self, args):
                page = args["page"]
                if page == 1:
                    # One genuine ledger revision, followed by read-only pages
                    # that leave it unchanged across subsequent provider calls.
                    witness = InputWitness()
                    owner._scopes["scope:reviewed"] = {"status": "approved", "goals": [
                        {"id": "entry", "kind": "text", "target": "Entry",
                         "value": "  exact\nΩ  ", "evidence_plane": "editor_buffer"}],
                        "preserves": [], "limitations": [], "unresolved_requirements": [],
                        "covers_entire_request": True, "issued_press_effects": [], "witness": witness}
                    owner.evidence["events"].append({"sequence": 1, "tool": "locua_verify",
                        "result": {"status": "unverified", "scopes": {"scope:reviewed": {
                            "goals": [{"goal_id": "entry", "matched": False,
                                "reason": "bound_predicate_not_met", "evidence": {
                                    "snapshot_id": "retained-s1", "observed_at_ns": 1234,
                                    "control_id": "editor:1", "actual": "old value",
                                    "property": "value", "plane": "editor_buffer"}}], "preserves": []}}}})
                result = ToolResult(success=True, output={"receipt": "page-" + str(page),
                    "payload": "x" * (66000 if page == 3 else 20000)})
                produced_results.append(result.model_dump())
                return result

        with tempfile.TemporaryDirectory() as tmp, patch(
                "locua.engine_adapter.runtime_environment", return_value=nullcontext()):
            provider = LocalAmplifierProvider(model="qwen38", service_factory=CountingService,
                                              out=Path(tmp) / "provider")
            try:
                result = await execute_session(original, provider, [Tool()],
                    out=Path(tmp) / "session", execution_facts=facts)
            finally:
                await provider.close()

        self.assertEqual(len(services), 1)
        self.assertTrue(services[0].closed)
        self.assertEqual(result["session_cleanup"], "closed")
        self.assertEqual(len(provider.records), 4)
        self.assertEqual(len(reminders), 4, "Request recounts must not regenerate reminder bodies")
        self.assertNotEqual(reminders[0], reminders[1])
        self.assertEqual(reminders[1:], [reminders[1]] * 3)
        self.assertEqual(result["config"]["session"]["orchestrator"]["config"]
                         ["ephemeral_injection_mode"], "persist")
        self.assertTrue(any(e["event"] == "context:compaction" for e in result["events"]))
        self.assertTrue(any(r["budget_decision"]["measurement"]["input_tokens"] > 24576
                            for r in provider.budget_records), "Must exercise preflight overflow and compaction")
        self.assertGreater(len(provider.budget_records), len(provider.records))
        self.assertTrue(all(r["generation_calls"] == 0 for r in provider.budget_records))

        def assert_pairs(messages):
            calls = [c["id"] for m in messages if m["role"] == "assistant"
                     for c in m.get("tool_calls", [])]
            replies = [json.loads(m["content"])["tool_call_id"] for m in messages if m["role"] == "tool"]
            self.assertEqual(sorted(calls), sorted(replies))
            self.assertEqual(len(calls), len(set(calls)))

        for index, record in enumerate(provider.records):
            native = record["native_request"]
            self.assertEqual(native["messages"][0]["content"],
                             SYSTEM + "\nORIGINAL USER REQUEST (retain throughout):\n" + original)
            self.assertFalse(any(m["role"] == "system" for m in native["messages"][1:]))
            self.assertEqual(native["tools"], provider.records[0]["native_request"]["tools"])
            self.assertTrue(any(reminders[index] in m["content"] for m in native["messages"]
                                if m["role"] == "user"), "Latest complete ledger must survive")
            measured = provider.budget_records[record["exact_budget_measurement"]["sequence"] - 1]
            self.assertEqual(native["messages"], measured["native_request"]["messages"])
            self.assertEqual(native["tools"], measured["native_request"]["tools"])
            measured_tokens = measured["budget_decision"]["measurement"]["input_tokens"]
            self.assertEqual(measured_tokens, record["generation"]["usage"]["input_tokens"])
            without = [m for m in native["messages"] if reminders[index] not in m["content"]]
            self.assertGreater(measured_tokens, services[0].size(without, native["tools"]),
                               "Count must include the reminder, not just prior usage")
            assert_pairs(native["messages"])

        canonical = result["transcript"]
        injected = [m for m in canonical if (m.get("metadata") or {}).get("persisted") is True]
        self.assertEqual(len(injected), 2, "Stable reminder must not append each iteration")
        self.assertEqual(sum(reminders[0] in m["content"] for m in injected), 1)
        self.assertEqual(sum(reminders[1] in m["content"] for m in injected), 1)
        self.assertTrue(all(m["role"] == "user" for m in injected))
        real_users = [m["content"] for m in canonical if m["role"] == "user"
                      and not (m.get("metadata") or {}).get("ephemeral")]
        self.assertEqual(real_users, [original])
        canonical_native = native_request({"messages": canonical,
            "tools": provider.records[0]["request"]["tools"]}, model="qwen38",
            structured_tool_results=provider._structured_tool_results)
        assert_pairs(canonical_native["messages"])
        outputs = [json.loads(m["content"])["output"] for m in canonical_native["messages"]
                   if m["role"] == "tool"]
        self.assertEqual(outputs, produced_results, "Original full tool results must remain byte-semantic equivalent")
        latest = json.loads(reminders[-1])
        self.assertFalse(latest["data"]["verification"][0]["matched"])
        self.assertEqual(latest["data"]["verification"][0]["readback"]["actual"], "old value")
        self.assertFalse(latest["action_authority"])
        self.assertFalse(latest["current_state_proven"])


if __name__ == "__main__":
    unittest.main()
