"""Regression checks for the R3/S2 program failures, without live model calls."""

import json

import pytest

from goalkeeper.context import ContextLimit, ContextManager
from goalkeeper.engine import Engine, INSTRUCTIONS
from goalkeeper.models import Features, Goal, ModelRequest, RunConfig, RunState, ToolResult
from goalkeeper.provider import ScriptedProvider
from goalkeeper.store import Store


@pytest.fixture
def record(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(RunState(workspace=str(root), goal=Goal(original="keep", current="keep")))
    yield store, state, ContextManager(store)
    store.close()


def transaction(number, size=2000):
    call_id = f"call-{number}"
    return [
        {"type": "reasoning", "encrypted_content": "reasoning", "_boundary": True},
        {"type": "function_call", "name": "run_command", "arguments": "{}", "call_id": call_id},
        {"type": "function_call_output", "call_id": call_id, "output": " old-work" * size},
    ]


@pytest.mark.parametrize("features", [Features(), Features.baseline()])
def test_s2_resume_user_message_does_not_expand_recent_budget(record, features):
    _, state, context = record
    state.config.features = features
    state.messages = [
        {"role": "user", "content": "initial goal", "_boundary": True},
        {"role": "user", "content": "resume handoff"},
        *[m for n in range(12) for m in transaction(n)],
    ]
    old, recent = context.split(state)
    assert len(old) > 2  # Summarize completed work, not just the initial task description.
    assert context.count(recent) <= int(state.config.context_limit * state.config.recent_fraction)
    calls = {m["call_id"] for m in recent if m.get("type") == "function_call"}
    results = {m["call_id"] for m in recent if m.get("type") == "function_call_output"}
    assert calls == results


def test_r3_oversized_completed_transaction_can_be_archived_whole(record):
    _, state, context = record
    state.messages = [{"role": "user", "content": "goal"}, *transaction(1, 17000)]
    old, recent = context.split(state)
    assert old == state.messages and recent == []


def test_pending_multi_call_group_and_goal_change_stay_together(record):
    _, state, context = record
    state.messages = [
        {"role": "user", "content": "old history " * 18000},
        {"type": "reasoning", "encrypted_content": "opaque", "_boundary": True},
        {"type": "function_call", "name": "run_command", "arguments": "{}", "call_id": "a"},
        {"type": "function_call", "name": "run_command", "arguments": "{}", "call_id": "b"},
        {"type": "function_call_output", "call_id": "a", "output": "done"},
        {"role": "user", "content": "updated goal", "_boundary": False},
    ]
    old, recent = context.split(state)
    assert len(old) == 1
    assert {m.get("call_id") for m in recent if m.get("type") == "function_call"} == {"a", "b"}
    assert recent[0]["type"] == "reasoning"
    state.messages[-1]["content"] = "changed " * 20000
    with pytest.raises(ContextLimit, match="未完成"):
        context.split(state)


@pytest.mark.parametrize("name", ["run_command", "delegate"])
def test_large_result_is_bounded_and_full_output_is_readable_only_by_its_run(record, name):
    store, state, context = record
    long_error = "AssertionError: expected decimal total; observed wrong total\n" * 20000
    output = (
        {"stdout": "test started\n", "stderr": long_error, "exit_code": 1, "timed_out": False}
        if name == "run_command"
        else {
            "role": "Worker",
            "summary": "Added tests; shared money module is still missing.",
            "patch": "+new test\n" * 12000,
            "files": ["tests/test_integration.py"],
            "checks": [
                {
                    "argv": ["python", "-m", "unittest"],
                    "result": {"ok": False, "output": long_error, "exit_code": 1},
                }
            ],
            "issues": ["money.py missing"],
            "evidence_refs": ["child-archive"],
        }
    )
    result = ToolResult(
        ok=False, output=json.dumps(output), changed_files=["tests/test_integration.py"], exit_code=1
    )
    packed = context.pack_result(state, result, name)
    assert context.count(packed) <= 4096
    body = json.loads(packed)
    assert not body["ok"] and body["exit_code"] == 1
    assert body["changed_files"] == result.changed_files
    reference = body["evidence"]["tool_output_ref"]
    raw = store.read_tool_output(state.id, reference)
    assert json.loads(raw) == json.loads(result.model_dump_json())
    with pytest.raises(PermissionError):
        store.read_tool_output("another-run", reference)
    with pytest.raises(PermissionError):
        store.read_tool_output(state.id, "../../account.json")
    part = context.read_tool_output(state, reference, 0)
    assert context.count(part.model_dump_json()) <= 4096
    assert json.loads(part.output)["next_offset"] > 0
    assert result.output == json.dumps(output)  # Keep the host's full result and merge evidence intact.


async def test_s2_compression_reaches_target_including_rules_and_tools(record, summary_provider):
    store, state, context = record
    state.messages = [
        {"role": "user", "content": "original goal"},
        {"role": "user", "content": "resume handoff"},
        *[m for n in range(12) for m in transaction(n)],
    ]
    state = store.commit(state, "history")
    provider = summary_provider("Completed earlier work; continue final checks.")
    engine = Engine(store, provider)
    compacted = await engine.compact(state)
    assert context.count(compacted.messages) <= 12800
    size = context.request_tokens(
        context.build(compacted, INSTRUCTIONS, engine.tool_specs(compacted)),
        INSTRUCTIONS,
        engine.tool_specs(compacted),
    )
    assert size <= 35200
    assert store.has_event(state.id, "context_compacted")
    assert any("old-work" in r["text"] for r in store.search(state.id, "old-work"))


async def test_preflight_blocks_oversized_child_or_summary_before_provider(record):
    store, state, context = record
    provider = ScriptedProvider([])
    engine = Engine(store, provider)
    request = ModelRequest(
        instructions="rules", input=[{"role": "user", "content": "word " * 70000}], config=RunConfig()
    )
    with pytest.raises(ContextLimit):
        await engine.call(state.id, request, "Reviewer")
    assert not provider.requests and not store.has_event(state.id, "model_started")


def test_recovery_of_completed_large_result_is_bounded_without_replaying(record):
    store, state, context = record
    state.messages = [
        {"role": "user", "content": "goal"},
        {
            "type": "function_call",
            "name": "run_command",
            "arguments": "{}",
            "call_id": "saved",
            "_boundary": True,
        },
    ]
    state = store.commit(state, "saved_response")
    result = ToolResult(ok=False, output="failure detail\n" * 18000, exit_code=1)
    store.operation(
        state.id,
        "operation",
        "completed",
        1,
        {
            "call_id": "saved",
            "name": "run_command",
            "result": result.model_dump(),
        },
    )
    provider = ScriptedProvider([])
    recovered = Engine(store, provider).recover(state)
    outputs = [m for m in recovered.messages if m.get("type") == "function_call_output"]
    assert len(outputs) == 1 and outputs[0]["call_id"] == "saved"
    assert context.count(outputs[0]["output"]) <= 4096
    reference = json.loads(outputs[0]["output"])["evidence"]["tool_output_ref"]
    assert json.loads(store.read_tool_output(state.id, reference))["output"] == result.output
    assert not provider.requests and not store.unfinished(state.id)


def test_log_pagination_handles_json_escaping_and_preserves_all_characters(record):
    store, state, context = record
    raw = '中文日志\\"\\n\n' * 3000
    reference = store.save_tool_output(state.id, raw, "run_command")
    offset, restored = 0, []
    while offset < len(raw):
        result = context.read_tool_output(state, reference, offset)
        assert context.count(result.model_dump_json()) <= 4096
        part = json.loads(result.output)
        restored.append(part["text"])
        assert part["next_offset"] > offset
        offset = part["next_offset"]
    assert "".join(restored) == raw
