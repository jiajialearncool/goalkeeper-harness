import asyncio
import json
import time

import pytest

from goalkeeper.engine import BudgetExceeded, Engine, WorkWindowEnded
from goalkeeper.models import CheckSpec, Goal, Milestone, ModelRequest, Requirement, RunConfig, RunState
from goalkeeper.provider import ScriptedProvider
from goalkeeper.sandbox import Scope
from goalkeeper.store import Store
from goalkeeper.tools import LocalTools


@pytest.fixture
def ready(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "result.txt").write_text("correct")
    spec = CheckSpec(name="result", argv=["/usr/bin/grep", "-q", "correct", "result.txt"])
    goal = Goal(
        original="deliver",
        current="deliver",
        requirements=[
            Requirement(id="result", text="correct result", checks=[spec]),
        ],
    )
    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=goal,
            active_seconds=1690,
            final_checks=[spec],
            milestones=[
                Milestone(
                    description="implementation",
                    checks=[spec],
                    requirement_ids=["result"],
                    implementation_paths=["result.txt"],
                ),
                Milestone(description="compatibility", checks=[spec]),
            ],
        )
    )
    yield store, state
    store.close()


async def test_near_deadline_checks_and_delivers_without_another_model_call(ready):
    store, state = ready
    provider = ScriptedProvider([])
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "completed", result.error
    assert all(m.status == "verified" for m in result.milestones)
    assert result.compaction_progress is None
    assert not provider.requests and result.tokens_used == 0
    assert store.has_event(state.id, "final_checked", {"passed": True})
    assert store.has_event(state.id, "budget_closeout_result", {"passed": True})


@pytest.mark.parametrize("invalid", ["bad_file", "missing_implementation", "missing_requirement", "no_plan"])
async def test_closeout_does_not_relax_existing_acceptance_rules(ready, invalid):
    store, state = ready
    if invalid == "bad_file":
        (state.root / "result.txt").write_text("wrong")
    elif invalid == "missing_implementation":
        state.milestones[0].implementation_paths = ["missing.txt"]
    elif invalid == "missing_requirement":
        state.milestones[0].requirement_ids = []
    else:
        state.milestones = []
    state = store.commit(state, "invalid")
    engine = Engine(store, ScriptedProvider([]))
    result = await engine.closeout(state, LocalTools(Scope(state.root)))
    assert result.phase != "completed"
    assert not store.has_event(state.id, "final_checked", {"passed": True})
    # An unchanged failure is not checked over and over on every loop.
    first_events = store.events(state.id)
    await engine.closeout(result, LocalTools(Scope(state.root)))
    assert store.events(state.id) == first_events


async def test_file_fix_allows_new_closeout_after_a_failed_check(ready):
    store, state = ready
    engine = Engine(store, ScriptedProvider([]))
    (state.root / "result.txt").write_text("wrong")
    failed = await engine.closeout(state, LocalTools(Scope(state.root)))
    assert failed.phase != "completed"
    (state.root / "result.txt").write_text("correct")
    fixed = await engine.closeout(store.get(state.id), LocalTools(Scope(state.root)))
    assert fixed.phase == "completed"


async def test_actual_file_change_is_rechecked_instead_of_reusing_verified_stage(ready):
    store, state = ready
    engine = Engine(store, ScriptedProvider([]))
    local = LocalTools(Scope(state.root))
    for milestone in state.milestones:
        assert (await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)).ok
    (state.root / "result.txt").write_text("wrong")
    result = await engine.closeout(store.get(state.id), local)
    assert result.phase != "completed" and result.milestones[0].status == "active"


async def test_short_work_window_timeout_retains_time_for_checks(ready):
    store, state = ready
    state.config = RunConfig(seconds_budget=2, finish_reserve_seconds=1)
    state.active_seconds = 1.75  # 0.25 seconds remain; reserve is capped at 0.2 seconds.
    state = store.commit(state, "short_budget")

    class SlowProvider:
        async def stream(self, request):
            await asyncio.sleep(1)
            if False:
                yield

    engine = Engine(store, SlowProvider())
    engine.clocks[state.id] = (state.active_seconds, time.monotonic())
    request = ModelRequest(instructions="work", input=[], config=state.config)
    with pytest.raises(WorkWindowEnded):
        await engine.call(state.id, request)
    assert 0 < engine.budget(store.get(state.id)) <= 0.2
    assert store.has_event(state.id, "model_finished")


async def test_total_budget_still_stops_requests_and_closeout(ready):
    store, state = ready
    state.active_seconds = state.config.seconds_budget
    state = store.commit(state, "exhausted")
    engine = Engine(store, ScriptedProvider([]))
    with pytest.raises(BudgetExceeded):
        await engine.closeout(state, LocalTools(Scope(state.root)))
    result = await engine.run(state.id)
    assert result.phase == "budget_exhausted" and not store.has_event(state.id, "final_checked")


async def test_budget_prompt_and_new_child_rejection_preserve_child_allowance(ready):
    store, state = ready
    engine = Engine(store, ScriptedProvider([]))
    assert "剩余预算" in engine.task_instructions(state) and "现在优先必要检查" in engine.task_instructions(
        state
    )
    result = await engine.execute(
        state.id,
        "delegate",
        {
            "role": "Reviewer",
            "task": "optional review",
            "paths": [],
        },
        LocalTools(Scope(state.root)),
        1,
    )
    assert not result.ok and store.get(state.id).children_used == 0
    assert not store.has_event(state.id, "child_started")


async def test_zero_reserves_keep_original_model_driven_finish(ready):
    store, state = ready
    state.config.finish_reserve_seconds = 0
    state.config.finish_reserve_tokens = 0
    state.active_seconds = 0
    state = store.commit(state, "disabled_reserve")
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "function_call",
                    "call_id": "finish",
                    "name": "finish",
                    "arguments": json.dumps({"summary": "done"}),
                }
            ]
        ]
    )
    engine = Engine(store, provider)
    local = LocalTools(Scope(state.root))
    for milestone in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)
    result = await engine.run(state.id)
    assert result.phase == "completed" and len(provider.requests) == 1
    assert not store.has_event(state.id, "budget_closeout_started")


async def test_development_request_timeout_switches_to_real_checks_and_finishes(ready):
    store, state = ready
    state.config.seconds_budget = 10
    state.active_seconds = 8.85
    state = store.commit(state, "almost_at_reserve")

    class SlowProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            await asyncio.sleep(2)
            if False:
                yield

    provider = SlowProvider()
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "completed", result.error
    assert result.active_seconds < 10 and provider.calls == 1
    assert store.has_event(state.id, "work_window_ended")
    assert store.has_event(state.id, "final_checked", {"passed": True})


async def test_closeout_keeps_feedback_hook_and_rejects_old_goal_operations(ready):
    store, state = ready
    engine = Engine(store, ScriptedProvider([]))
    calls = []

    async def change_goal(run_id):
        calls.append(run_id)
        engine.update_goal(run_id, "new requirement during closeout")

    engine.hook = change_goal
    result = await engine.closeout(state, LocalTools(Scope(state.root)))
    assert calls == [state.id] and result.goal.version == 2
    assert result.phase != "completed" and not store.has_event(state.id, "stage_checked")


async def test_feedback_is_checked_again_before_final_delivery(ready):
    store, state = ready
    engine = Engine(store, ScriptedProvider([]))
    local = LocalTools(Scope(state.root))
    for milestone in state.milestones:
        await engine.execute(state.id, "check_stage", {"milestone_id": milestone.id}, local, 1)

    async def change_goal(run_id):
        engine.update_goal(run_id, "changed after stage checks")

    engine.hook = change_goal
    result = await engine.closeout(store.get(state.id), local)
    assert result.goal.version == 2 and result.phase != "completed"
    assert not store.has_event(state.id, "final_checked")
