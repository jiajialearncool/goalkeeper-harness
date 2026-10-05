"""Efficiency must preserve original evidence, permissions and delivery gates."""

import json
from copy import deepcopy

import pytest

from goalkeeper.engine import Engine
from goalkeeper.files import initialize_repository
from goalkeeper.models import CheckSpec, Goal, Milestone, RunConfig, RunState, ToolResult
from goalkeeper.provider import ScriptedProvider
from goalkeeper.sandbox import PermissionDenied, Scope
from goalkeeper.store import Store
from goalkeeper.tools import LocalTools


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    spec = CheckSpec(name="correct", argv=["/usr/bin/grep", "-q", "correct", "result.txt"])
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="deliver correct", current="deliver correct"),
            config=RunConfig(efficiency_enabled=True),
            final_checks=[spec],
            milestones=[Milestone(description=n, checks=[spec]) for n in ["implement", "validate"]],
        )
    )
    yield store, state, Engine(store, ScriptedProvider([])), LocalTools(Scope(root))
    store.close()


async def test_local_replacement_rejects_ambiguity_stale_hash_and_permissions(setup):
    _, state, _, local = setup
    p = state.root / "result.txt"
    p.write_text("old\nkeep\n")
    read = await local.execute("read_file", {"path": "result.txt"})
    changed = await local.execute(
        "replace_in_file",
        {
            "path": "result.txt",
            "old_text": "old",
            "new_text": "correct",
            "sha256": read.evidence["sha256"],
        },
    )
    assert changed.changed_files == ["result.txt"] and p.read_text() == "correct\nkeep\n"
    with pytest.raises(ValueError, match="过期"):
        await local.execute(
            "replace_in_file",
            {
                "path": "result.txt",
                "old_text": "keep",
                "new_text": "bad",
                "sha256": read.evidence["sha256"],
            },
        )
    p.write_text("repeat repeat")
    with pytest.raises(ValueError, match="唯一"):
        await local.execute(
            "replace_in_file",
            {
                "path": "result.txt",
                "old_text": "repeat",
                "new_text": "bad",
            },
        )
    assert p.read_text() == "repeat repeat"
    for args in [
        {"path": "../outside", "old_text": "x", "new_text": "y"},
        {"path": "result.txt", "old_text": "repeat", "new_text": "y"},
    ]:
        with pytest.raises(PermissionDenied):
            await LocalTools(Scope(state.root, writable=False)).execute("replace_in_file", args)


def transaction(name, args, identifier, result):
    return [
        {
            "type": "function_call",
            "name": name,
            "call_id": identifier,
            "arguments": json.dumps(args),
            "_boundary": True,
        },
        {
            "type": "function_call_output",
            "call_id": identifier,
            "output": ToolResult(**result).model_dump_json(),
        },
    ]


def test_summary_material_shortens_successful_versions_and_preserves_failures(setup):
    _, state, engine, _ = setup
    raw = "important-code " * 3000
    old = [
        *transaction("write_file", {"path": "a.py", "content": raw}, "old", {"ok": True, "output": "saved"}),
        *transaction(
            "write_file", {"path": "a.py", "content": raw + "new"}, "new", {"ok": True, "output": "saved"}
        ),
        *transaction("read_file", {"path": "a.py"}, "r1", {"ok": True, "output": raw}),
        *transaction("read_file", {"path": "a.py"}, "r2", {"ok": True, "output": raw}),
        *transaction("run_command", {"argv": ["check"]}, "failure", {"ok": False, "output": raw}),
        {
            "type": "function_call",
            "call_id": "unfinished",
            "name": "write_file",
            "arguments": json.dumps({"path": "a.py", "content": raw}),
        },
        {"role": "user", "content": "Keep every hard requirement."},
    ]
    before = deepcopy(old)
    material = engine.context.summary_material(state, old)
    assert old == before
    assert material[-4:] == engine.context.summary_history(old)[-4:]
    assert engine.context.count(material) < engine.context.count(old)
    assert json.loads(material[0]["arguments"])["content"]["superseded_by"] == "call:new"
    assert "完全相同" in json.loads(material[7]["output"])["output"]
    assert [m.get("call_id") for m in old] == [m.get("call_id") for m in material]


def test_recall_dedup_keeps_uncovered_tail_and_is_reset_after_compression_or_goal_change(setup):
    store, state, engine, _ = setup
    original = {
        "id": "source",
        "blob": "immutable",
        "goal_version": 1,
        "superseded": 0,
        "offset": 10,
        "text": "abcdefghij",
        "next_offset": 20,
    }
    _, span = engine.context.novel_recall(state, original)
    state.messages = [{"role": "developer", "content": "abcdefghij", "_recall_ranges": [span]}]
    duplicate, _ = engine.context.novel_recall(state, original)
    assert duplicate is None
    overlapping = {**original, "offset": 15, "text": "fghijklmno", "next_offset": 25}
    fresh, _ = engine.context.novel_recall(state, overlapping)
    assert "klmno" in fresh["text"] and "fghij" not in fresh["text"]
    state.goal.version = 2
    assert engine.context.novel_recall(state, original)[0] == original
    state.goal.version = 1
    state.messages = []
    assert engine.context.novel_recall(state, original)[0] == original
    archive = store.archive(state.id, 1, "source facts " * 1000)
    state.recall_refs = ["archive:" + archive]
    state = store.commit(state, "reference")
    first = engine.restore_sources(state, "stage:first")
    again = engine.restore_sources(first, "stage:second")
    assert len(again.messages) == len(first.messages)
    event = [e for e in store.events(state.id) if e["kind"] == "sources_restored_before_work"][-1]
    assert event["payload"]["tokens"] == 0 and event["payload"]["deduplicated_tokens"] > 0


async def test_auto_activation_then_verified_delivery_without_model_request(setup):
    store, state, engine, local = setup
    (state.root / "result.txt").write_text("correct")
    activated = await engine.advance(state, local)
    assert activated.active_stage == state.milestones[0].id and activated.phase != "completed"
    for milestone in activated.milestones:
        assert (await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)).ok
    result = await engine.run(state.id)
    assert result.phase == "completed" and not engine.provider.requests
    assert store.has_event(state.id, "delivery_ready")
    assert store.has_event(state.id, "final_checked", {"passed": True})


async def test_goal_feedback_before_auto_delivery_invalidates_previous_evidence(setup):
    store, state, engine, local = setup
    (state.root / "result.txt").write_text("correct")
    for milestone in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)

    async def feedback(identifier):
        engine.update_goal(identifier, "new goal with additional constraints")

    engine.hook = feedback
    result = await engine.advance(store.get(state.id), local)
    assert result.goal.version == 2 and result.phase != "completed"
    assert not store.has_event(state.id, "delivery_ready")


async def test_changed_file_and_failed_final_check_cannot_auto_deliver(setup):
    store, state, engine, local = setup
    (state.root / "result.txt").write_text("correct")
    for milestone in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)
    (state.root / "result.txt").write_text("wrong")
    result = await engine.advance(store.get(state.id), local)
    assert result.phase != "completed" and not store.has_event(state.id, "delivery_ready")


async def test_optimized_compaction_keeps_full_archive_and_transaction_pairs(setup, summary_provider):
    store, state, _, _ = setup
    raw = "visible code lines " * 6000
    state.messages = [
        *transaction(
            "write_file", {"path": "a.py", "content": raw}, "write", {"ok": True, "output": "saved"}
        ),
        {"role": "user", "content": "recent", "_boundary": True},
    ]
    original = deepcopy(state.messages)
    state = store.commit(state, "history")
    engine = Engine(store, summary_provider("a.py written; check it using the original source."))
    result = await engine.compact(state)
    row = store.db.execute("SELECT text FROM archives WHERE run_id=?", (state.id,)).fetchone()
    assert json.loads(row[0]) == original[:-1]
    assert result.messages == original[-1:]
    payload = json.loads(engine.provider.requests[0].input[0]["content"])
    assert len(payload["fragment"]) < len(json.dumps(original[:-1]))
    recalled = engine.context.read_source(result, "call:write")
    assert raw[:100] in recalled["text"]


@pytest.mark.parametrize("reported", [[], ["result.txt:1 contains a confirmed defect"]])
async def test_targeted_reviewer_receives_actual_changes_and_preserves_issues(setup, reported):
    store, state, _, _ = setup
    (state.root / "result.txt").write_text("original")
    initialize_repository(state.root)
    (state.root / "result.txt").write_text("correct")
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "function_call",
                    "name": "finish",
                    "call_id": "review",
                    "arguments": json.dumps({"summary": "reviewed result.txt", "issues": reported}),
                }
            ]
        ]
    )
    result = await Engine(store, provider).delegate(
        state,
        {
            "role": "Reviewer",
            "task": "review one risk",
            "paths": ["result.txt"],
        },
    )
    assert result.ok and json.loads(result.output)["issues"] == reported
    assert len(provider.requests) == 1
    assert "correct" in provider.requests[0].input[1]["content"]
    assert (
        "issues"
        in next(t for t in provider.requests[0].tools if t["name"] == "finish")["parameters"]["required"]
    )


async def test_text_claim_is_not_a_completed_optimized_review(setup):
    store, state, _, _ = setup
    provider = ScriptedProvider(
        [[{"type": "message", "content": [{"type": "output_text", "text": "no problems"}]}]]
    )
    result = await Engine(store, provider).delegate(
        state,
        {
            "role": "Reviewer",
            "task": "review",
            "paths": [],
        },
    )
    assert not result.ok and "未完成" in json.loads(result.output)["issues"][0]
