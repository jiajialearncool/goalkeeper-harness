"""Focused input must preserve evidence, exact sources and mandatory review."""

import json
from copy import deepcopy

import httpx
import pytest

from goalkeeper.engine import Engine
from goalkeeper.models import RunConfig, ModelRequest
from goalkeeper.provider import ResponsesProvider, ProviderError, ScriptedProvider
from test_efficiency import setup as setup, transaction


def focus(store, state, review=False):
    state.config.focused_context_enabled = True
    state.config.final_review_required = review
    return store.commit(state, "focused_fixture")


def remember_read(store, state, result, call="read"):
    state.messages.extend(
        transaction("read_file", {"path": result.evidence["path"]}, call, result.model_dump())
    )
    state.messages[-1]["_file_read"] = {
        **result.evidence,
        "call_id": call,
        "goal_version": state.goal.version,
    }
    return store.commit(state, "read_fixture")


def test_work_view_removes_complete_old_pairs_but_preserves_sources_and_failures(setup):
    store, state, engine, _ = setup
    state = focus(store, state)
    raw = "old code line\n" * 5000
    state.messages = [
        *transaction("write_file", {"path": "a.py", "content": raw}, "old", {"ok": True, "output": "saved"}),
        *transaction(
            "write_file",
            {"path": "b.py", "content": raw},
            "failed",
            {"ok": False, "output": "error evidence"},
        ),
        {"role": "user", "content": "Keep the original business constraint", "_boundary": True},
        {"role": "assistant", "content": "Decision: preserve compatibility", "_boundary": True},
        {"type": "function_call", "name": "write_file", "call_id": "unfinished", "arguments": "{}"},
    ]
    state = store.commit(state, "history")
    original = deepcopy(state.messages)
    view = engine.context.work_messages(state)
    assert state.messages == original
    assert not any(m.get("call_id") == "old" for m in view)
    assert [m for m in view if m.get("call_id") == "failed"] == original[2:4]
    assert view[-1]["call_id"] == "unfinished"
    assert "business constraint" in json.dumps(view) and "preserve compatibility" in json.dumps(view)
    assert "call:old" in json.dumps(view)
    saved = json.loads(store.source(state.id, "call:old")["text"])
    assert json.loads(saved[0]["arguments"])["content"] == raw
    assert engine.context.count(view) < engine.context.count(original) * 0.6


async def test_file_dedup_requires_current_visible_body_and_can_be_bypassed(setup):
    store, state, engine, local = setup
    state = focus(store, state)
    p = state.root / "result.txt"
    p.write_text("a\nb\nc\n")
    result = await engine.execute(state.id, "read_file", {"path": "result.txt"}, local, 1)
    state = remember_read(store, state, result)
    cached = await engine.execute(state.id, "read_file", {"path": "./result.txt"}, local, 1)
    assert cached.evidence["deduplicated"] and "call:read" in cached.output
    forced = await engine.execute(state.id, "read_file", {"path": "result.txt", "force": True}, local, 1)
    assert forced.output == "a\nb\nc\n"
    p.write_text("changed\n")
    changed = await engine.execute(state.id, "read_file", {"path": "result.txt"}, local, 1)
    assert changed.output == "changed\n" and not changed.evidence.get("deduplicated")
    p.write_text("a\nb\nc\n")
    state.messages = []
    store.commit(state, "compressed_fixture")
    assert (
        await engine.execute(state.id, "read_file", {"path": "result.txt"}, local, 1)
    ).output == "a\nb\nc\n"


async def test_range_read_never_claims_unseen_lines_and_rejects_invalid_ranges(setup):
    store, state, engine, local = setup
    state = focus(store, state)
    (state.root / "result.txt").write_text("a\nb\nc\nd\n")
    result = await engine.execute(
        state.id, "read_file", {"path": "result.txt", "start_line": 2, "end_line": 3}, local, 1
    )
    assert result.output == "b\nc\n"
    state = remember_read(store, state, result)
    assert (
        await engine.execute(
            state.id, "read_file", {"path": "result.txt", "start_line": 3, "end_line": 3}, local, 1
        )
    ).evidence["deduplicated"]
    assert (
        await engine.execute(state.id, "read_file", {"path": "result.txt"}, local, 1)
    ).output == "a\nb\nc\nd\n"
    with pytest.raises(ValueError):
        await engine.execute(state.id, "read_file", {"path": "result.txt", "start_line": True}, local, 1)


async def test_explicit_source_read_dedup_keeps_fresh_tail_and_force_access(setup):
    store, state, engine, local = setup
    state = focus(store, state)
    ref = "archive:" + store.archive(state.id, 1, "original business evidence " * 500)
    first = await engine.execute(state.id, "read_source", {"reference": ref}, local, 1)
    state.messages.append(
        {"role": "developer", "content": first.output, "_recall_ranges": first.evidence["source_ranges"]}
    )
    state = store.commit(state, "source_visible")
    duplicate = await engine.execute(state.id, "read_source", {"reference": ref}, local, 1)
    assert json.loads(duplicate.output)["already_visible"]
    forced = await engine.execute(state.id, "read_source", {"reference": ref, "force": True}, local, 1)
    assert "original business evidence" in forced.output
    state.goal.version = 2
    store.commit(state, "goal_change_fixture")
    assert (
        "original business evidence"
        in (await engine.execute(state.id, "read_source", {"reference": ref}, local, 2)).output
    )


def test_summary_bounds_successful_patch_material_without_changing_raw_history(setup):
    store, state, engine, _ = setup
    state = focus(store, state)
    text = "unique function content\n" * 2000
    messages = [
        *transaction(
            "replace_in_file",
            {"path": "a.py", "old_text": text, "new_text": text + "new"},
            "patch",
            {"ok": True, "output": "done"},
        ),
        *transaction(
            "replace_in_file",
            {"path": "a.py", "old_text": text, "new_text": text},
            "bad",
            {"ok": False, "output": "not unique"},
        ),
    ]
    original = deepcopy(messages)
    material = engine.context.summary_material(state, messages)
    assert messages == original and material[2:] == engine.context.summary_history(original[2:])
    args = json.loads(material[0]["arguments"])
    assert args["old_text_source"]["reference"] == "call:patch" and len(args["old_text"]) < len(text)


async def test_resume_preserves_valid_progress_but_detects_actual_changes(setup):
    store, state, engine, local = setup
    state = focus(store, state)
    p = state.root / "result.txt"
    p.write_text("correct")
    for m in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)
    state = store.get(state.id)
    from goalkeeper.files import inventory

    state.checkpoint_files = inventory(state.root)
    state = store.commit(state, "checkpoint")
    restored = engine.recover(state)
    assert all(m.status == "verified" for m in restored.milestones)
    assert '"changed_since_checkpoint": []' in restored.messages[-1]["content"]
    p.write_text("wrong")
    restored = engine.recover(store.get(state.id))
    assert all(m.status == "pending" for m in restored.milestones)
    assert "result.txt" in restored.messages[-1]["content"]


async def test_auto_delivery_requires_structured_review_and_original_checks(setup):
    store, state, _, local = setup
    state = focus(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "function_call",
                    "name": "finish",
                    "call_id": "review",
                    "arguments": json.dumps({"summary": "checked", "issues": []}),
                }
            ]
        ]
    )
    engine = Engine(store, provider)
    for m in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)
    assert engine.finish_reserve(store.get(state.id)) == 210
    result = await engine.run(state.id)
    assert result.phase == "completed" and result.review_evidence["passed"]
    assert len(provider.requests) == 1 and store.has_event(state.id, "final_checked", {"passed": True})
    (state.root / "result.txt").write_text("changed")
    assert not engine.review_current(result)


async def test_unstructured_review_cannot_pass_required_gate(setup):
    store, state, _, _ = setup
    state = focus(store, state, review=True)
    provider = ScriptedProvider(
        [[{"type": "message", "content": [{"type": "output_text", "text": "looks fine"}]}]]
    )
    engine = Engine(store, provider)
    assert not await engine.ensure_review(state)
    assert not store.get(state.id).review_evidence["passed"]
    assert not await engine.ensure_review(store.get(state.id))
    assert len(provider.requests) == 1


async def test_interrupted_required_review_can_resume_closeout_without_replaying_checks(setup):
    store, state, _, local = setup
    state = focus(store, state, review=True)
    (state.root / "result.txt").write_text("correct")

    class InterruptedOnce(ScriptedProvider):
        interrupted = False

        async def stream(self, request):
            if not self.interrupted:
                self.interrupted = True
                raise ProviderError("connection", "fixture", True)
            async for event in super().stream(request):
                yield event

    provider = InterruptedOnce(
        [
            [
                {
                    "type": "function_call",
                    "name": "finish",
                    "call_id": "review",
                    "arguments": json.dumps({"summary": "checked", "issues": []}),
                }
            ]
        ]
    )
    engine = Engine(store, provider)
    with pytest.raises(ProviderError):
        await engine.closeout(state, local)
    before = store.db.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND kind='check_executed'", (state.id,)
    ).fetchone()[0]
    result = await engine.closeout(store.get(state.id), local)
    after = store.db.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND kind='check_executed'", (state.id,)
    ).fetchone()[0]
    assert result.phase == "completed" and engine.review_current(result)
    assert after - before == len(state.final_checks)


def test_recall_partial_marker_never_claims_removed_prefix_is_still_visible(setup):
    store, state, engine, _ = setup
    state = focus(store, state)
    base = {
        "id": "archive:fixture",
        "blob": "same",
        "goal_version": 1,
        "superseded": False,
        "offset": 0,
        "text": "abcdefghij",
    }
    _, prefix = engine.context.novel_recall(state, base)
    state.messages = [{"role": "developer", "content": "abcdefghij", "_recall_ranges": [prefix]}]
    overlapping = {**base, "offset": 5, "text": "fghijklmno"}
    tail, marker = engine.context.novel_recall(state, overlapping)
    assert marker["segments"] == [(10, 15)]
    state.messages = [{"role": "developer", "content": tail["text"], "_recall_ranges": [marker]}]
    assert engine.context.novel_recall(state, base)[0] == base


def test_automatic_recall_bounds_search_archive_ids_without_crashing(setup):
    store, state, engine, _ = setup
    state = focus(store, state)
    query = "orders/report.py"
    archive_id = store.archive(state.id, 1, query + " important history" * 3000)
    stage = state.milestones[-1]
    stage.implementation_paths = [query]
    restored = engine.restore_sources(state, "stage:query", stage)
    assert archive_id in restored.messages[-1]["content"]
    assert engine.context.count(restored.messages[-1]["content"]) <= 1100
    assert query in store.source(state.id, "archive:" + archive_id)["text"]
    assert restored.phase == state.phase


async def test_auth_transport_is_resource_interruption_not_task_failure():
    class Auth:
        async def access_token(self):
            raise httpx.ConnectError("fixture")

    provider = ResponsesProvider(Auth())
    try:
        with pytest.raises(ProviderError) as caught:
            async for _ in provider.stream(
                ModelRequest(instructions="fixture", input=[], config=RunConfig())
            ):
                pass
        assert caught.value.resource and caught.value.code == "auth_transport_error"
    finally:
        await provider.close()
