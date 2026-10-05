import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from goalkeeper.context import ContextManager
from goalkeeper.engine import Engine
from goalkeeper.models import Features, Goal, Milestone, RunConfig, RunState
from goalkeeper.provider import ScriptedProvider
from goalkeeper.store import StaleState, Store


@pytest.fixture
def record(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(RunState(workspace=str(root), goal=Goal(original="keep", current="keep")))
    yield store, state
    store.close()


def test_concurrent_cas_only_one_writer(record):
    store, state = record

    def writer(_):
        try:
            store.commit(state, "change")
            return True
        except StaleState:
            return False

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(writer, range(8))) == 1
    assert store.get(state.id).version == 1


def test_atomic_failed_create_does_not_add_goal(record):
    store, state = record
    state.goal.version = 9
    with pytest.raises(Exception):
        store.create(state)
    assert store.db.execute("SELECT count(*) FROM goals WHERE version=9").fetchone()[0] == 0


def test_goal_change_invalidates_old_sources_and_evidence(record):
    store, state = record
    state.milestones = [Milestone(description="x", status="verified", evidence=[{"passed": True}])]
    store.commit(state, "planned")
    old = store.archive(state.id, 1, "old target obsolete keyword")
    engine = Engine(store, ScriptedProvider([]))
    state = engine.update_goal(state.id, "new", expected_version=1)
    assert state.goal.original == "keep" and state.goal.version == 2
    assert state.milestones[0].status == "pending" and not state.milestones[0].evidence
    assert store.search(state.id, "keyword") == []
    assert store.search(state.id, "keyword", include_superseded=True)[0]["id"] == old
    with pytest.raises(StaleState):
        engine.update_goal(state.id, "lost update", expected_version=1)


def test_archive_blob_and_recall_are_separate_from_model_use(record):
    store, state = record
    key = store.archive(state.id, 1, "invoice original 278.61")
    context = ContextManager(store)
    found = context.recall(state, "invoice")
    assert found[0]["id"] == key and "278.61" in found[0]["text"]
    assert (store.blobs / found[0]["blob"]).read_text() == found[0]["text"]
    state.config.features = Features.baseline()
    assert context.recall(state, "invoice") == []
    assert store.search(state.id, "invoice")


def test_cross_attempt_memory_and_archive_are_isolated(record):
    store, state = record
    store.archive("other", 1, "shared confidential keyword")
    store.add_memory("other", "project", "secret fact", "file:a.py", 1)
    assert store.search(state.id, "keyword") == []
    assert store.memories(state.id) == []


@pytest.mark.parametrize("text", ["access_token=xyz", "Bearer abcdefghijklmnop", "api_key=abcdefghijklmnop"])
def test_credential_candidates_are_rejected(record, text):
    store, state = record
    with pytest.raises(ValueError):
        store.add_memory(state.id, "project", text, "file:x", 1)


def test_missing_memory_file_is_not_injected(record):
    store, state = record
    store.add_memory(state.id, "project", "missing fact", "file:deleted.py", 1)
    assert "missing fact" not in ContextManager(store).memory_text(state)


def test_internal_transaction_marker_not_sent_to_api(record):
    store, state = record
    state.messages = [{"role": "user", "content": "test", "_boundary": True}]
    inputs = ContextManager(store).build(state, "instructions", [])
    assert "_boundary" not in json.dumps(inputs)


def test_baseline_flags_do_not_disable_final_check_or_change_budget():
    full = RunConfig()
    baseline = RunConfig(engine="controlled", features=Features.baseline())
    a, b = full.model_dump(), baseline.model_dump()
    for key in ["engine", "features"]:
        a.pop(key)
        b.pop(key)
    assert a == b


async def test_old_model_history_remains_readable_but_cannot_restart(record):
    store, state = record
    state.config = RunConfig(model="gpt-6-astra")
    store.commit(state, "historical_development_configuration")
    assert store.get(state.id).config.model == "gpt-6-astra"
    provider = ScriptedProvider([])
    with pytest.raises(ValueError, match="旧模型记录只供查看"):
        await Engine(store, provider).run(state.id)
    assert not provider.requests and not store.has_event(state.id, "model_started")
    assert store.get(state.id).config.model == "gpt-6-astra"


def test_usage_is_deduplicated_and_cached_reasoning_are_subsets(record):
    store, state = record
    usage = {"input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 12, "reasoning_output_tokens": 5}
    assert store.usage(state.id, "one", "main", usage)
    assert not store.usage(state.id, "one", "main", usage)
    assert store.db.execute("SELECT count(*) FROM usage").fetchone()[0] == 1


def test_baseline_memory_cannot_restore_goal_anchor(tmp_path):
    from goalkeeper.models import Goal
    from goalkeeper.context import fact_allowed

    goal = Goal(
        original="保留所有旧接口并完成订单迁移。验收标准：类型检查通过。",
        current="保留所有旧接口并完成订单迁移。",
    )
    assert not fact_allowed(goal, goal.current, "file:notes.md")
    assert not fact_allowed(goal, "完成订单迁移", "file:TASK.md")
    assert not fact_allowed(goal, "待办：迁移订单接口", "file:notes.md")
    assert fact_allowed(goal, 'TABLE_NAME = "order_rows"', "file:storage.py")


def test_event_evidence_is_found_after_first_500_events(tmp_path):
    from goalkeeper.store import Store

    store = Store(tmp_path / "private")
    for _ in range(550):
        store.event("run", "log", {})
    store.event("run", "program_closed_for_resume", {"actual_process_exit": True})
    assert store.has_event("run", "program_closed_for_resume", {"actual_process_exit": True})
    assert not store.has_event("run", "program_closed_for_resume", {"actual_process_exit": False})
    store.close()
