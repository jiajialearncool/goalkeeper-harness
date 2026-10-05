"""End-to-end scheduling changes must preserve evidence and review failures."""
import json
import time
from copy import deepcopy

import pytest

from goalkeeper.engine import Engine, WorkWindowEnded
from goalkeeper.files import digest, inventory, initialize_repository
from goalkeeper.models import ModelRequest
from goalkeeper.provider import ProviderError, ScriptedProvider
from test_efficiency import setup as setup, transaction


def enable(store, state, review=False):
    state.config.focused_context_enabled = True
    state.config.global_efficiency_enabled = True
    state.config.final_review_required = review
    return store.commit(state, "global_fixture")


def tool(name, args, call):
    return {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": call}


def test_closed_reasoning_view_keeps_visible_decisions_and_failed_turns(setup):
    store, state, engine, _ = setup
    state = enable(store, state)
    state.messages = [
        {"type": "reasoning", "encrypted_content": "opaque" * 4000,
         "summary": [{"type": "summary_text", "text": "Preserve old interface"}], "_boundary": True},
        *transaction("write_file", {"path": "a.py", "content": "value=1"}, "ok", {"ok": True, "output": "saved"}),
        {"type": "reasoning", "encrypted_content": "failed reasoning", "_boundary": True},
        *transaction("run_command", {"argv": ["false"]}, "fail", {"ok": False, "output": "actual failure"}),
        {"role": "user", "content": "next", "_boundary": True},
    ]
    # A Responses turn boundary is on the first response item, not every call.
    state.messages[1].pop("_boundary")
    state.messages[4].pop("_boundary")
    original = deepcopy(state.messages)
    view = engine.context.work_messages(state)
    assert state.messages == original
    assert "Preserve old interface" in json.dumps(view)
    assert "opaque" not in json.dumps(view)
    assert "failed reasoning" not in json.dumps(view) and "actual failure" in json.dumps(view)


async def test_reviewer_uses_shared_view_and_dedup_preserves_raw_sources(setup):
    store, state, _, _ = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct\n" * 200)
    initialize_repository(state.root)
    provider = ScriptedProvider([
        [tool("read_file", {"path": "result.txt"}, "read1")],
        [tool("read_file", {"path": "result.txt"}, "read2")],
        [tool("finish", {"summary": "checked", "issues": []}, "done")],
    ])
    engine = Engine(store, provider)
    result = await engine.delegate(state, {"role": "Reviewer", "task": "review result", "paths": ["result.txt"]})
    assert result.ok and engine.review_current(store.get(state.id))
    assert store.has_event(state.id, "file_read_deduplicated", {"path": "result.txt"})
    assert "correct" in store.source(state.id, "call:read1")["text"]
    assert store.get(state.id).review_progress == {}
    assert len([e for e in store.events(state.id) if e["kind"] == "child_context_prepared"]) == 3


async def test_interrupted_review_resumes_same_role_and_keeps_previous_read(setup):
    store, state, _, _ = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    initialize_repository(state.root)

    class BrokenSecond(ScriptedProvider):
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 2:
                raise ProviderError("connection", "fixture", True)
            async for event in super().stream(request):
                yield event

    provider = BrokenSecond([
        [tool("read_file", {"path": "result.txt"}, "read1")],
        [tool("finish", {"summary": "checked", "issues": []}, "done")],
    ])
    engine = Engine(store, provider)
    with pytest.raises(ProviderError):
        await engine.ensure_review(state)
    paused = store.get(state.id)
    assert paused.children_used == 1 and "read1" in json.dumps(paused.review_progress)
    assert await engine.ensure_review(paused)
    assert store.get(state.id).children_used == 1
    assert "correct" in json.dumps(provider.requests[-1].input)


async def test_closing_repair_skips_soft_summary_then_checks_and_delivers(setup, monkeypatch):
    store, state, _, _ = setup
    state = enable(store, state)
    state.closing_mode = True
    state = store.commit(state, "closing")
    (state.root / "result.txt").write_text("wrong")
    provider = ScriptedProvider([[tool("write_file", {"path": "result.txt", "content": "correct"}, "fix"),
        *[tool("check_stage", {"milestone_id": m.id}, m.id) for m in state.milestones]]])
    engine = Engine(store, provider)
    monkeypatch.setattr(engine.context, "should_compact", lambda *a: True)

    async def forbidden(*a):
        raise AssertionError("Nonessential summary used the repair window")

    monkeypatch.setattr(engine, "compact", forbidden)
    result = await engine.run(state.id)
    assert result.phase == "completed" and len(provider.requests) == 1


async def test_confirmed_issue_blocks_delivery_until_fixed_and_rereviewed(setup):
    store, state, _, local = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct but defect")
    initialize_repository(state.root)
    provider = ScriptedProvider([
        [tool("finish", {"summary": "defect", "issues": ["result.txt:1 confirmed defect"]}, "bad")],
        [tool("finish", {"summary": "fixed", "issues": []}, "good")],
    ])
    engine = Engine(store, provider)
    for m in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)
    before = await engine.closeout(store.get(state.id), local, "verified_stages")
    assert before.phase != "completed" and not before.review_evidence["passed"]
    assert not await engine.ensure_review(before)
    assert len(provider.requests) == 1
    (state.root / "result.txt").write_text("correct")
    after = await engine.closeout(store.get(state.id), local, "verified_stages")
    assert after.phase == "completed" and engine.review_current(after)
    assert "confirmed defect" in json.dumps(provider.requests[-1].input)


async def test_expired_review_window_sends_no_request_and_cannot_pass(setup):
    store, state, engine, _ = setup
    state = enable(store, state, review=True)
    with pytest.raises(WorkWindowEnded):
        await engine.call(state.id, ModelRequest(instructions="check", input=[], config=state.config),
                          "Reviewer", deadline=time.monotonic() - 1)
    assert not engine.provider.requests and not engine.review_current(store.get(state.id))


async def test_goal_change_discards_review_progress_and_old_delivery_mode(setup):
    store, state, engine, _ = setup
    state = enable(store, state, review=True)
    state.closing_mode = True
    state.review_progress = {"snapshot": digest(state.root), "goal_version": 1, "messages": []}
    state.review_evidence = {"passed": True, "snapshot": digest(state.root), "goal_version": 1, "files": inventory(state.root)}
    store.commit(state, "old_progress")
    changed = engine.update_goal(state.id, "new goal")
    assert not changed.closing_mode and not changed.review_progress and not engine.review_current(changed)


def test_goal_is_present_once_without_dropping_other_user_messages(setup):
    store, state, engine, _ = setup
    state = enable(store, state)
    state.messages = [{"role": "user", "content": state.goal.model_dump_json()},
                      {"role": "user", "content": "additional decision"}]
    view = engine.context.build(state, "rules", [])
    assert sum(state.goal.model_dump_json() in str(m.get("content", "")) for m in view) == 1
    assert "additional decision" in json.dumps(view)
    state.config.features.goal_anchor = False
    assert state.goal.model_dump_json() in json.dumps(engine.context.build(state, "rules", []), ensure_ascii=False).replace('\\"', '"')


async def test_folded_child_reference_resolves_only_exact_call_pair(setup):
    store, state, _, _ = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    initialize_repository(state.root)
    provider = ScriptedProvider([
        [tool("run_command", {"argv": ["/usr/bin/true"]}, "command1")],
        [tool("read_file", {"path": "result.txt"}, "read1")],
        [tool("finish", {"summary": "done", "issues": []}, "done")],
    ])
    await Engine(store, provider).delegate(state, {"role": "Reviewer", "task": "review", "paths": ["result.txt"]})
    third = json.dumps(provider.requests[2].input)
    assert "call:command1" in third
    pair = json.loads(store.source(state.id, "call:command1")["text"])
    assert len(pair) == 2 and all(m["call_id"] == "command1" for m in pair)


async def test_explicit_child_resource_error_interrupts_main_and_keeps_progress(setup):
    store, state, _, _ = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    initialize_repository(state.root)

    class BrokenChild(ScriptedProvider):
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 3:
                raise ProviderError("transport_error", "RemoteProtocolError", True)
            async for event in super().stream(request):
                yield event

    provider = BrokenChild([
        [tool("delegate", {"role": "Reviewer", "task": "review", "paths": ["result.txt"]}, "delegate")],
        [tool("read_file", {"path": "result.txt"}, "read1")],
    ])
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "resource_interrupted" and result.failure_origin == "resource"
    assert result.review_progress and "read1" in json.dumps(result.review_progress)
    assert len(provider.requests) == 2


async def test_exhausted_failed_review_stops_instead_of_spending_more_model_calls(setup):
    store, state, engine, local = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    for m in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)
    state = store.get(state.id)
    state.children_used = state.config.max_children
    state.review_evidence = {"snapshot": digest(state.root), "goal_version": 1, "passed": False,
                             "issues": ["confirmed defect"], "files": inventory(state.root)}
    store.commit(state, "failed_review")
    result = await engine.run(state.id)
    assert result.phase == "failed" and result.failure_origin == "verification"
    assert not engine.provider.requests


async def test_review_segment_timeout_is_resumable_not_quality_failure(setup):
    store, state, _, _ = setup
    state = enable(store, state, review=True)
    (state.root / "result.txt").write_text("correct")
    initialize_repository(state.root)

    class WindowEnded(ScriptedProvider):
        once = False

        async def stream(self, request):
            if not self.once:
                self.once = True
                raise WorkWindowEnded("segment fixture")
            async for event in super().stream(request):
                yield event

    provider = WindowEnded([[tool("finish", {"summary": "done", "issues": []}, "done")]])
    engine = Engine(store, provider)
    with pytest.raises(ProviderError) as caught:
        await engine.ensure_review(state)
    assert caught.value.resource and caught.value.code == "review_window"
    assert store.get(state.id).review_progress
    assert await engine.ensure_review(store.get(state.id))
    assert store.get(state.id).children_used == 1
