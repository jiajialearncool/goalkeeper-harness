import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from goalkeeper.engine import Engine
from goalkeeper.models import CheckSpec, Features, Goal, Milestone, RunConfig, RunState
from goalkeeper.processes import RunLease, birth, reconcile
from goalkeeper.provider import ScriptedProvider
from goalkeeper.sandbox import Scope
from goalkeeper.store import Store
from goalkeeper.tools import LocalTools
from test_engine_sandbox import tool


def make(tmp_path, features=None):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="保留原始要求：结果必须正确", current="结果必须正确"),
            config=RunConfig(features=features or Features()),
            final_checks=[
                CheckSpec(
                    name="实际文件",
                    argv=[
                        sys.executable,
                        "-c",
                        'from pathlib import Path;assert Path("a.txt").read_text()=="correct"',
                    ],
                )
            ],
        )
    )
    return store, state


async def test_two_compressions_preserve_goal_and_raw_recall(tmp_path, summary_provider):
    store, state = make(tmp_path)
    raw = "早期资料中的特征代号 blue-heron-778，必须保留引用。" + " long-record-xyz" * 6000
    state.messages = [
        {"role": "user", "content": raw, "_boundary": True},
        {"role": "user", "content": "近期实际工作", "_boundary": True},
    ]
    state = store.commit(state, "history")
    provider = summary_provider("已归档早期资料；后续使用 recall 检索 blue-heron-778 原文。")
    engine = Engine(store, provider)
    engine.clocks[state.id] = (0, __import__("time").monotonic())
    state = await engine.compact(state)
    recalled = engine.context.recall(state, "blue-heron-778")
    assert recalled and "blue-heron-778" in recalled[0]["text"]
    assert (store.blobs / recalled[0]["blob"]).read_text() == json.dumps(
        [{"role": "user", "content": raw, "_boundary": True}], ensure_ascii=False
    )
    state.messages.append({"role": "user", "content": " another-record" * 6000, "_boundary": True})
    state = store.commit(state, "more_history")
    state = await engine.compact(state)
    assembled = engine.context.build(state, "", [])
    assert any("保留原始要求" in m.get("content", "") for m in assembled)
    assert engine.context.recall(state, "blue-heron-778")
    assert len(provider.requests) == 2
    store.close()


async def test_evidence_invalidates_on_edit_and_finish_cannot_skip_stages(tmp_path):
    store, state = make(tmp_path)
    (state.root / "a.txt").write_text("correct")
    state.milestones = [
        Milestone(description="准备", checks=state.final_checks),
        Milestone(description="交付", checks=state.final_checks),
    ]
    state = store.commit(state, "plan")
    engine = Engine(store, ScriptedProvider([]))
    local = LocalTools(Scope(state.root))
    for stage in state.milestones:
        assert (await engine.execute(state.id, "check_stage", {"milestone_id": stage.id}, local, 1)).ok
    current = store.get(state.id)
    assert all(m.status == "verified" for m in current.milestones)
    Engine.invalidate(current)
    store.commit(current, "file_changed")
    result = await engine.execute(state.id, "finish", {"summary": "claim"}, local, 1)
    assert not result.ok and store.get(state.id).phase != "completed"
    assert "final" not in store.get(state.id).repairs
    assert len(json.loads(result.output)["pending_stages"]) == 2
    store.close()


async def test_stage_method_revision_preserves_goal_progress_and_repair_budget(tmp_path):
    store, state = make(tmp_path)
    (state.root / "a.txt").write_text("correct")
    state.milestones = [
        Milestone(description="准备", checks=state.final_checks),
        Milestone(description="交付", checks=state.final_checks),
    ]
    state.repairs[state.milestones[0].id] = 1
    state = store.commit(state, "planned")
    engine = Engine(store, ScriptedProvider([]))
    local = LocalTools(Scope(state.root))
    for stage in state.milestones:
        assert (await engine.execute(state.id, "check_stage", {"milestone_id": stage.id}, local, 1)).ok
    before = store.get(state.id)
    result = await engine.execute(
        state.id,
        "revise_stage",
        {
            "milestone_id": state.milestones[0].id,
            "reason": "换用等价检查工具",
            "checks": [{"name": "文件存在", "argv": ["/bin/test", "-f", "a.txt"]}],
        },
        local,
        1,
    )
    after = store.get(state.id)
    assert result.ok and after.goal == before.goal and after.final_checks == before.final_checks
    assert [m.id for m in after.milestones] == [m.id for m in before.milestones]
    assert after.milestones[0].status == "pending" and not after.milestones[0].evidence
    assert after.milestones[1].status == "verified" and after.repairs == before.repairs
    after.config = RunConfig(engine="controlled", features=Features.baseline())
    assert "revise_stage" not in {t["name"] for t in engine.tool_specs(after)}
    store.close()


async def test_old_goal_write_is_cancelled_before_tool(tmp_path):
    store, state = make(tmp_path, Features.baseline())
    provider = ScriptedProvider(
        [
            [tool("write_file", {"path": "a.txt", "content": "obsolete"})],
            [tool("write_file", {"path": "a.txt", "content": "correct"}, "new")],
            [tool("finish", {"summary": "done"}, "done")],
        ]
    )
    engine = Engine(store, provider)
    changed = False

    async def hook(run):
        nonlocal changed
        if not changed:
            changed = True
            engine.update_goal(run, "用户已变更要求，结果仍必须正确")

    engine.hook = hook
    result = await engine.run(state.id)
    assert result.phase == "completed" and result.goal.version == 2
    assert result.goal.original.startswith("保留原始要求")
    assert result.root.joinpath("a.txt").read_text() == "correct"
    outputs = [json.loads(m["output"]) for m in result.messages if m.get("type") == "function_call_output"]
    assert "旧目标" in outputs[0]["output"]
    store.close()


def test_goal_change_during_pending_call_does_not_split_call_from_result(tmp_path):
    store, state = make(tmp_path)
    state.messages = [
        {"role": "user", "content": "earlier history " * 20000, "_boundary": True},
        {**tool("run_command", {"argv": ["/bin/echo", "ok"]}, "pending"), "_boundary": True},
    ]
    store.commit(state, "response_saved")
    engine = Engine(store, ScriptedProvider([]))
    engine.update_goal(state.id, "用户已变更要求")
    current = store.get(state.id)
    current.messages.append(
        {"type": "function_call_output", "call_id": "pending", "output": "旧目标操作取消"}
    )
    _, recent = engine.context.split(current)
    assert any(m.get("type") == "function_call" and m.get("call_id") == "pending" for m in recent)
    assert any(m.get("type") == "function_call_output" and m.get("call_id") == "pending" for m in recent)
    store.close()


async def test_worker_patch_is_merged_from_own_worktree(tmp_path):
    store, state = make(tmp_path, Features.baseline())
    (state.root / "a.txt").write_text("wrong")
    provider = ScriptedProvider(
        [
            [tool("write_file", {"path": "a.txt", "content": "correct"})],
            [tool("finish", {"summary": "修改 a.txt；内容已核对。"}, "end")],
        ]
    )
    engine = Engine(store, provider)
    engine.clocks[state.id] = (0, __import__("time").monotonic())
    result = await engine.delegate(state, {"role": "Worker", "task": "修复 a.txt", "paths": ["a.txt"]})
    assert result.ok and result.changed_files == ["a.txt"] and (state.root / "a.txt").read_text() == "correct"
    assert not list(state.root.parent.glob("child-*"))
    assert all(t["name"] != "delegate" for req in provider.requests for t in req.tools)
    store.close()


async def test_readonly_child_cannot_write_or_spawn_child(tmp_path):
    store, state = make(tmp_path, Features.baseline())
    (state.root / "a.txt").write_text("original")
    provider = ScriptedProvider(
        [
            [tool("write_file", {"path": "a.txt", "content": "changed"})],
            [tool("finish", {"summary": "写入被拒绝。"}, "end")],
        ]
    )
    engine = Engine(store, provider)
    engine.clocks[state.id] = (0, __import__("time").monotonic())
    await engine.delegate(state, {"role": "Reviewer", "task": "只读复核", "paths": []})
    assert (state.root / "a.txt").read_text() == "original"
    assert "write_file" not in [t["name"] for t in provider.requests[0].tools]
    state = store.get(state.id)
    state.children_used = 2
    store.commit(state, "children_used")
    assert "delegate" not in [t["name"] for t in engine.tool_specs(store.get(state.id))]
    store.close()


def test_cross_process_lease_rejects_double_execution(tmp_path):
    lease = RunLease(tmp_path, "run")
    try:
        with pytest.raises(RuntimeError):
            RunLease(tmp_path, "run")
    finally:
        lease.close()
    RunLease(tmp_path, "run").close()


async def test_actual_process_exit_leaves_owned_child_for_reconciliation(tmp_path):
    store, state = make(tmp_path)
    program = """import asyncio,os,sys
from pathlib import Path
from goalkeeper.store import Store
from goalkeeper.sandbox import Scope,Seatbelt,runtime_roots
from goalkeeper.processes import track
async def main():
 store=Store(Path(sys.argv[1]));runner=track(store,sys.argv[2],Seatbelt(Scope(Path(sys.argv[3]),runtimes=runtime_roots())))
 original=runner.on_spawn
 def on_spawn(p):
  original(p);os._exit(75)
 runner.on_spawn=on_spawn
 await runner.run([sys.executable,'-c','import time;time.sleep(120)'])
asyncio.run(main())"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        str(store.directory),
        state.id,
        str(state.root),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    await child.wait()
    assert child.returncode == 75
    records = store.process_records(state.id)
    assert len(records) == 1
    pid = records[0]["pid"]
    assert birth(pid) is not None
    result = reconcile(store, state.id)
    assert result[0]["killed"]
    assert not store.process_records(state.id)
    store.close()


def test_pid_reuse_does_not_kill_unrelated_process(tmp_path):
    store, state = make(tmp_path)
    store.register_process(state.id, os.getpid(), "different process birth")
    result = reconcile(store, state.id)
    assert not result[0]["killed"] and not store.process_records(state.id)
    store.close()
