import json

import pytest

from goalkeeper.assurance import evidence_current
from goalkeeper.context import ContextManager
from goalkeeper.engine import Engine
from goalkeeper.models import CheckPolicy, CheckSpec, Goal, Milestone, Requirement, RunState
from goalkeeper.provider import ScriptedProvider
from goalkeeper.sandbox import Scope
from goalkeeper.store import Store
from goalkeeper.tools import LocalTools
from goalkeeper.verifier import Verifier


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("yes")
    (root / "b.txt").write_text("yes")
    store = Store(tmp_path / "private")
    spec = CheckSpec(name="a 内容", argv=["/usr/bin/grep", "-q", "yes", "a.txt"])
    goal = Goal(
        original="保持 a",
        current="保持 a",
        requirements=[Requirement(id="A", text="a 包含 yes", checks=[spec])],
    )
    state = store.create(
        RunState(
            workspace=str(root),
            goal=goal,
            final_checks=[spec],
            check_policies=[CheckPolicy(argv=spec.argv, dependencies=["a.txt"], closed=True)],
        )
    )
    yield store, state, Engine(store, ScriptedProvider([])), LocalTools(Scope(root))
    store.close()


async def test_missing_requirement_cannot_be_hidden_by_easy_stage_checks(setup):
    store, state, engine, local = setup
    with pytest.raises(ValueError, match="覆盖全部要求"):
        await engine.execute(
            state.id,
            "plan",
            {
                "milestones": [
                    {"description": name, "checks": [{"name": "容易检查", "argv": ["/usr/bin/true"]}]}
                    for name in ["一", "二"]
                ]
            },
            local,
            1,
        )
    state.milestones = [Milestone(description=name, checks=state.final_checks) for name in ["一", "二"]]
    store.commit(state, "fixture")
    for m in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)
    result = await engine.execute(state.id, "finish", {"summary": "完成"}, local, 1)
    assert not result.ok and not json.loads(result.output)["requirements"][0]["verified"]


async def test_trusted_requirement_check_runs_even_if_agent_only_proposes_true(setup):
    store, state, engine, local = setup
    (state.root / "a.txt").write_text("wrong")
    state.milestones = [
        Milestone(
            description="实现",
            checks=[CheckSpec(name="easy", argv=["/usr/bin/true"])],
            requirement_ids=["A"],
            implementation_paths=["a.txt"],
        )
    ]
    store.commit(state, "fixture")
    result = await engine.execute(state.id, "check_stage", {"milestone_id": state.milestones[0].id}, local, 1)
    assert not result.ok
    assert len(json.loads(result.output)["checks"]) == 2


async def test_unrelated_edit_reuses_check_but_dependency_edit_forces_real_check(setup):
    store, state, engine, _ = setup
    state.milestones = [
        Milestone(
            description="实现",
            checks=state.final_checks,
            requirement_ids=["A"],
            implementation_paths=["a.txt"],
        )
    ]
    state = store.commit(state, "fixture")
    verifier = Verifier(store)
    first = await verifier.check(state.root, state.final_checks, 1, reuse=True)
    state = store.get(state.id)
    state.milestones[0].evidence = [first.model_dump()]
    state.milestones[0].status = "verified"
    state = store.commit(state, "verified")
    (state.root / "b.txt").write_text("changed")
    Engine.invalidate(state, ["b.txt"])
    assert state.milestones[0].status == "verified"
    store.commit(state, "edit")
    second = await verifier.check(state.root, state.final_checks, 1, reuse=True)
    assert second.checks[0]["reused"]
    (state.root / "a.txt").write_text("wrong")
    third = await verifier.check(state.root, state.final_checks, 1, reuse=True)
    assert not third.passed and not third.checks[0]["reused"]
    assert not evidence_current(store.get(state.id), first.model_dump(), state.final_checks)


async def test_unknown_dependencies_new_file_timeout_and_goal_change_invalidate(setup):
    store, state, _, _ = setup
    state.check_policies = []
    store.commit(state, "broad")
    verifier = Verifier(store)
    first = await verifier.check(state.root, state.final_checks, 1, reuse=True)
    (state.root / "new.txt").write_text("new import")
    assert not evidence_current(store.get(state.id), first.model_dump(), state.final_checks)
    second = await verifier.check(state.root, state.final_checks, 1, reuse=True)
    assert not second.checks[0]["reused"]
    state = store.get(state.id)
    state.goal.version += 1
    store.commit(state, "change")
    assert not (await verifier.check(state.root, state.final_checks, 2, reuse=True)).checks[0]["reused"]
    changed_timeout = state.final_checks[0].model_copy(update={"timeout": 10})
    assert not (await verifier.check(state.root, [changed_timeout], 2, reuse=True)).checks[0]["reused"]


def test_recall_returns_middle_match_and_rejects_other_runs(setup):
    store, state, _, _ = setup
    archive = store.archive(state.id, 1, "irrelevant " * 12000 + "decision-778 keep cents" + "tail " * 3000)
    context = ContextManager(store)
    records = context.recall(state, "decision-778")
    assert records[0]["offset"] > 0 and "decision-778 keep cents" in records[0]["text"]
    assert context.count(records[0]) <= 4096
    other = store.create(RunState(workspace=state.workspace, goal=state.goal))
    with pytest.raises(PermissionError):
        context.read_source(other, "archive:" + archive)
    store.supersede(state.id, 2)
    record = context.read_source(state, "archive:" + archive, "decision-778")
    assert record["superseded"] and "decision-778" in record["text"]


def test_stage_recall_happens_before_work_once_and_restore_uses_owned_calls(setup):
    store, state, engine, _ = setup
    archive = store.archive(
        state.id,
        1,
        json.dumps(
            [
                {"type": "function_call", "call_id": "decision", "arguments": "a.txt should preserve cents"},
                {"type": "function_call_output", "call_id": "decision", "output": "confirmed a.txt"},
            ]
        ),
    )
    state.goal.requirements[0].source_refs = ["call:decision"]
    stage = Milestone(
        description="实现", checks=state.final_checks, requirement_ids=["A"], implementation_paths=["a.txt"]
    )
    state.milestones = [stage]
    state = store.commit(state, "fixture")
    restored = engine.restore_sources(state, "stage:1:" + stage.id, stage)
    assert "confirmed a.txt" in restored.messages[-1]["content"]
    assert engine.restore_sources(restored, "stage:1:" + stage.id, stage).version == restored.version
    record = engine.context.read_source(state, "archive:" + archive, "a.txt")
    assert record["query_found"]
    restored.config.features.layered_context = False
    assert engine.restore_sources(restored, "new", stage).messages == restored.messages


async def test_existing_path_and_valid_evidence_allow_delivery(setup):
    store, state, engine, local = setup
    state.milestones = [
        Milestone(
            description=n, checks=state.final_checks, requirement_ids=["A"], implementation_paths=["a.txt"]
        )
        for n in ["实现", "交付"]
    ]
    store.commit(state, "fixture")
    for m in state.milestones:
        assert (await engine.execute(state.id, "check_stage", {"milestone_id": m.id}, local, 1)).ok
    assert (await engine.execute(state.id, "finish", {"summary": "完成"}, local, 1)).ok
    assert store.get(state.id).phase == "completed"


def test_new_session_restores_preserved_reference_without_old_conversation(setup):
    store, state, engine, _ = setup
    store.archive(
        state.id,
        1,
        json.dumps(
            [
                {"type": "function_call", "call_id": "call_old-decision", "arguments": "a.txt"},
                {
                    "type": "function_call_output",
                    "call_id": "call_old-decision",
                    "output": "Do not round until final result",
                },
            ]
        ),
    )
    state.summary = ""
    state.messages = [{"role": "user", "content": "继续任务"}]
    state.recall_refs = ["call:call_old-decision"]
    stage = Milestone(description="交付", checks=state.final_checks, implementation_paths=["a.txt"])
    state.milestones = [stage]
    state = store.commit(state, "new_session")
    restored = engine.restore_sources(state, "resume:1:epoch", stage)
    assert "Do not round until final result" in restored.messages[-1]["content"]
    assert restored.messages[0]["content"] == "继续任务"


def test_new_transitive_import_is_included_and_dynamic_import_falls_back(setup):
    from goalkeeper.assurance import dependency_key

    store, state, _, _ = setup
    folder = state.root / "app"
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    (folder / "a.py").write_text("from .b import value\n")
    (folder / "b.py").write_text("value=1\n")
    (folder / "unrelated.py").write_text("value=9\n")
    spec = CheckSpec(name="closed", argv=["/usr/bin/grep", "-q", "value", "app/a.py"])
    state.check_policies = [CheckPolicy(argv=spec.argv, dependencies=["app/a.py"], closed=True)]
    first = dependency_key(state.root, spec, state)
    (folder / "unrelated.py").write_text("value=10\n")
    assert dependency_key(state.root, spec, state) == first
    (folder / "b.py").write_text("value=2\n")
    assert dependency_key(state.root, spec, state) != first
    (folder / "a.py").write_text('import importlib\nvalue=importlib.import_module("app.b").value\n')
    broad = dependency_key(state.root, spec, state)
    (folder / "unrelated.py").write_text("value=11\n")
    assert dependency_key(state.root, spec, state) != broad


def test_dynamic_file_read_uses_all_files_for_freshness(tmp_path):
    from goalkeeper.assurance import dependency_key
    from goalkeeper.models import CheckPolicy, CheckSpec, RunState

    (tmp_path / "app").mkdir()
    (tmp_path / "app/parser.py").write_text(
        "from pathlib import Path\ndef result():return Path('rules.json').read_text()\n"
    )
    (tmp_path / "rules.json").write_text("old")
    spec = CheckSpec(name="parser", argv=["/usr/bin/true"])
    state = RunState(
        workspace=str(tmp_path),
        goal=Goal(original="检查动态读取", current="检查动态读取"),
        check_policies=[CheckPolicy(argv=spec.argv, dependencies=["app/parser.py"], closed=True)],
    )
    before = dependency_key(tmp_path, spec, state)
    (tmp_path / "rules.json").write_text("new")
    assert dependency_key(tmp_path, spec, state) != before
