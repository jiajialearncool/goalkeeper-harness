"""Summary preparation must preserve evidence and the live Responses protocol."""

from copy import deepcopy
import json

import pytest

from goalkeeper.context import ContextManager
from goalkeeper.engine import Engine, INSTRUCTIONS
from goalkeeper.models import Features, Goal, RunConfig, RunState
from goalkeeper.store import Store


def test_summary_filter_keeps_visible_reasoning_and_business_payloads():
    arguments = json.dumps({"encrypted_content": "business field", "_customer": "keep"})
    history = [
        {
            "type": "reasoning",
            "id": "rs-1",
            "encrypted_content": "opaque provider data",
            "summary": [{"type": "summary_text", "text": "Keep decimal rounding, not float."}],
            "_boundary": True,
        },
        {
            "type": "function_call",
            "name": "write_file",
            "arguments": arguments,
            "call_id": "call-1",
            "_boundary": False,
        },
        {"type": "function_call_output", "call_id": "call-1", "output": arguments},
        {"role": "user", "content": "Latest requirement: preserve compatibility."},
    ]
    original = deepcopy(history)
    prepared = ContextManager.summary_history(history)
    assert history == original
    assert "encrypted_content" not in prepared[0] and "_boundary" not in prepared[0]
    assert prepared[0]["summary"] == original[0]["summary"]
    assert prepared[1]["arguments"] == prepared[2]["output"] == arguments
    assert prepared[1]["call_id"] == prepared[2]["call_id"] == "call-1"
    assert prepared[3] == original[3]
    prepared[0]["summary"][0]["text"] = "changed copy"
    assert history == original


@pytest.mark.parametrize("features", [Features(), Features.baseline()])
async def test_compaction_filters_summary_only_and_archives_complete_original(
    tmp_path, features, summary_provider
):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    try:
        history = [
            {"role": "user", "content": "Original goal: preserve rounding."},
            {
                "type": "reasoning",
                "encrypted_content": "OPAQUE_SUMMARY_ONLY " * 15000,
                "summary": [{"type": "summary_text", "text": "Decimal rounding chosen."}],
                "_boundary": True,
            },
            {
                "type": "function_call",
                "name": "write_file",
                "call_id": "old-call",
                "arguments": '{"path":"money.py","content":"decimal implementation"}',
            },
            {"type": "function_call_output", "call_id": "old-call", "output": "Written money.py."},
            {"type": "reasoning", "encrypted_content": "RECENT_PROTOCOL_MUST_STAY", "_boundary": True},
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "recent-call",
                "arguments": '{"path":"money.py"}',
            },
            {"type": "function_call_output", "call_id": "recent-call", "output": "decimal implementation"},
        ]
        state = store.create(
            RunState(
                workspace=str(root),
                goal=Goal(original="keep", current="keep"),
                messages=history,
                config=RunConfig(features=features),
            )
        )
        context = ContextManager(store)
        old, recent = context.split(state, force=True)
        assert context.count({"history": old}) > 38400
        before = deepcopy(history)
        provider = summary_provider("Decimal rounding chosen; money.py written. Continue verification.")
        engine = Engine(store, provider)
        compacted = await engine.compact(state)
        assert len(provider.requests) == 1
        request = provider.requests[0]
        material = json.loads(json.loads(request.input[0]["content"])["fragment"])["history"]
        assert material == context.summary_history(old)
        assert "OPAQUE_SUMMARY_ONLY" not in request.input[0]["content"]
        assert "Decimal rounding chosen." in request.input[0]["content"]
        assert history == before and compacted.messages == recent
        assembled = context.build(compacted, INSTRUCTIONS, engine.tool_specs(compacted))
        assert any(m.get("encrypted_content") == "RECENT_PROTOCOL_MUST_STAY" for m in assembled)
        events = store.events(state.id)
        started = next(e["payload"] for e in events if e["kind"] == "context_compaction_started")
        archived = json.loads(
            store.db.execute("SELECT text FROM archives WHERE id=?", (started["archive"],)).fetchone()[0]
        )
        assert archived == old
        assert started["summary_chunks"] == 1
        assert started["summary_source_original_tokens"] > 38400
        assert started["summary_source_tokens"] < 38400 and started["summary_removed_tokens"] > 0
    finally:
        store.close()


@pytest.mark.parametrize("features", [Features(), Features.baseline()])
async def test_summary_chunks_fit_serialized_request_and_preserve_escaped_unicode(
    tmp_path, features, summary_provider
):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    try:
        # Code-like quoted JSON grows when nested in the model input envelope.
        content = '中文😀 "key": "\\\\path\\n", ' * 12000
        history = [{"role": "user", "content": content}]
        state = store.create(
            RunState(
                workspace=str(root),
                goal=Goal(original="keep", current="keep"),
                messages=history,
                config=RunConfig(features=features),
            )
        )
        provider = summary_provider("Keep escaped source in archive and continue checks.")
        engine = Engine(store, provider)
        before = json.dumps({"history": engine.context.summary_history(history)}, ensure_ascii=False)
        result = await engine.compact(state)
        fragments = [json.loads(r.input[0]["content"])["fragment"] for r in provider.requests]
        assert len(fragments) > 1 and "".join(fragments) == before
        for request in provider.requests:
            assert (
                engine.context.request_tokens(request.input, request.instructions, request.tools)
                + engine.context.output_reserve(state)
                <= state.config.context_limit
            )
        assert result.summary
    finally:
        store.close()
