import asyncio
from types import SimpleNamespace

import pytest

from goalkeeper.codex import CodexClient, ProtocolError
from goalkeeper.codex_worker import run_codex
from goalkeeper.models import CheckSpec, Goal, RunConfig, RunState, Verdict
from goalkeeper.store import Store


class FakeNative:
    instances = []
    event_script = []

    def __init__(self, *args):
        self.process = SimpleNamespace(pid=99999999, returncode=None)
        self.thread_id = None
        self.usage_by_thread = {
            "root": {
                "inputTokens": 90,
                "outputTokens": 10,
                "cachedInputTokens": 50,
                "reasoningOutputTokens": 3,
            }
        }
        self.calls = []
        self.goal_status = "paused"
        self.events = self
        self.queue = list(self.event_script)
        self.instances.append(self)

    async def start(self):
        self.calls.append("start")

    async def create_thread(self, *args):
        self.thread_id = "root"
        return "root"

    async def resume(self, *args):
        self.thread_id = args[0]
        self.calls.append("resume")

    async def set_goal(self, objective=None, budget=None, status="active"):
        self.calls.append(("goal", status, objective))
        self.goal_status = status

    async def turn(self, *args):
        self.calls.append("turn")
        assert self.goal_status == "paused", "Avoid set-goal automatic continuation racing with initial turn"
        return {"turn": {"id": "t1"}}

    async def get(self):
        item = self.queue.pop(0)
        self.goal_status = item.pop("test_goal_status", self.goal_status)
        await asyncio.sleep(0)
        return item

    async def request(self, method, params):
        self.calls.append(method)
        return {"goal": {"status": self.goal_status}}

    async def interrupt(self, *args):
        self.calls.append("interrupt")

    async def close(self):
        self.process.returncode = 0


class FakeVerifier:
    def __init__(self, *args):
        pass

    async def check(self, root, checks, version, *args):
        return Verdict(passed=True, goal_version=version, snapshot="proof", checks=[])


def fixture(tmp_path, monkeypatch, events, resume=False):
    from goalkeeper import codex_worker

    FakeNative.instances = []
    FakeNative.event_script = events
    monkeypatch.setattr(codex_worker, "CodexClient", FakeNative)
    monkeypatch.setattr(codex_worker, "Verifier", FakeVerifier)
    monkeypatch.setattr(codex_worker, "collect_tree", lambda *args: None)
    monkeypatch.setattr(codex_worker, "reconcile", lambda *args: None)
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="保持原有功能", current="修复任务"),
            config=RunConfig(engine="codex"),
            final_checks=[CheckSpec(name="proof", argv=["/bin/test", "-f", "done.txt"])],
            native_thread="root" if resume else None,
        )
    )
    return store, state


async def test_native_goal_auto_continues_without_host_synthetic_turn(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "root"}},
            {"method": "turn/completed", "params": {"threadId": "root"}, "test_goal_status": "active"},
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "completed" and result.tokens_used == 100 and result.usage_complete
    calls = FakeNative.instances[-1].calls
    assert calls.count("turn") == 1
    first_goal = next(c for c in calls if isinstance(c, tuple) and c[0] == "goal")
    assert calls.index(first_goal) < calls.index("turn") and first_goal[1] == "paused"
    assert ("goal", "paused", None) in calls
    store.close()


async def test_native_resume_pauses_goal_before_resume_and_retains_old_cost(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "root"}},
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
        resume=True,
    )
    state.native_usage = {"root": {"inputTokens": 150, "outputTokens": 50}}
    store.commit(state, "earlier_usage")
    result = await run_codex(store, state.id)
    assert result.tokens_used == 200
    calls = FakeNative.instances[-1].calls
    assert calls.index(("goal", "paused", None)) < calls.index("resume")
    store.close()


async def test_native_quota_is_resource_interruption(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {"method": "error", "params": {"threadId": "root", "error": {"message": "usage limit reached"}}},
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "resource_interrupted" and result.tokens_used == 100
    assert not result.usage_complete
    store.close()


async def test_native_advisory_reconnect_is_not_a_task_failure(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {
                "method": "error",
                "params": {
                    "threadId": "root",
                    "willRetry": True,
                    "error": {
                        "message": "Reconnecting... 2/5",
                        "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                    },
                },
            },
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "root"}},
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "completed" and result.tokens_used == 100
    store.close()


async def test_native_exhausted_transport_retry_remains_resumable(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {
                "method": "error",
                "params": {
                    "threadId": "root",
                    "willRetry": False,
                    "error": {
                        "message": "request timed out",
                        "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                    },
                },
            }
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "resource_interrupted" and result.failure_origin == "resource"
    assert not result.usage_complete
    store.close()


def test_native_config_ignores_other_project_registration_but_detects_task_policy_changes(tmp_path):
    from goalkeeper.codex import normalized_configuration

    workspace = tmp_path / "task"
    base = {"model": "gpt-6-astra", "projects": {str(workspace): {"trust_level": "trusted"}}}
    other = {**base, "projects": {**base["projects"], "/other/task": {"trust_level": "trusted"}}}
    assert normalized_configuration(base, workspace) == normalized_configuration(other, workspace)
    changed = {**base, "projects": {str(workspace): {"trust_level": "untrusted"}}}
    assert normalized_configuration(base, workspace) != normalized_configuration(changed, workspace)


async def test_native_goal_long_objective_is_rejected_before_inference():
    client = CodexClient()
    with pytest.raises(ProtocolError):
        await client.set_goal("x" * 4001, 1000)


async def test_native_create_resume_and_turn_use_the_approved_model(tmp_path):
    client = CodexClient()
    calls = []

    async def request(method, params):
        calls.append((method, params))
        return {"thread": {"id": "root"}, "turn": {"id": "turn"}}

    client.request = request
    await client.create_thread(tmp_path)
    await client.resume("root", tmp_path)
    await client.turn("complete the task", tmp_path, [])
    assert [method for method, _ in calls] == ["thread/start", "thread/resume", "turn/start"]
    assert all(params["model"] == "gpt-6.1-sol" for _, params in calls)
    assert calls[0][1]["config"]["model_reasoning_effort"] == calls[2][1]["effort"] == "xhigh"


def test_native_resume_budget_does_not_double_charge_previous_goal_usage(tmp_path):
    from goalkeeper.codex_worker import native_budget, native_objective

    state = RunState(
        workspace=str(tmp_path), goal=Goal(original="修复文件并保留接口", current="修复文件并保留接口")
    )
    state.tokens_used = 300
    old = {"objective": native_objective(state.goal), "tokensUsed": 250, "status": "paused"}
    assert native_budget(state, old) == 999950
    old["status"] = "complete"
    assert native_budget(state, old) == 999700


async def test_native_child_model_drift_stops_and_records_configuration_violation(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {
                "method": "item/started",
                "params": {
                    "threadId": "root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "receiverThreadIds": ["child"],
                        "model": "other-model",
                        "reasoningEffort": "low",
                    },
                },
            }
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "failed" and result.failure_origin == "configuration"
    assert result.native_children == ["child"] and not result.usage_complete
    store.close()


async def test_native_children_without_observed_usage_are_marked_incomplete(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "root"}},
            {
                "method": "item/completed",
                "params": {
                    "threadId": "root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "receiverThreadIds": ["child"],
                    },
                },
            },
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "completed" and result.children_used == 1 and result.tokens_used == 100
    assert not result.usage_complete
    store.close()


async def test_native_receives_both_business_goal_changes(tmp_path, monkeypatch):
    from goalkeeper.benchmark import Feedback

    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {"method": "item/completed", "params": {"threadId": "root"}},
            {"method": "item/completed", "params": {"threadId": "root"}},
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
    )

    async def steer(self, text, turn):
        self.calls.append(("steer", text, turn))

    monkeypatch.setattr(FakeNative, "steer", steer, raising=False)
    probe = CheckSpec(name="业务进度", argv=["/usr/bin/true"])
    steps = [Feedback(kind="goal_change", probe=probe, goal=text) for text in ["第二版要求", "第三版要求"]]
    result = await run_codex(store, state.id, steps)
    assert result.goal.version == 3 and result.goal.current == "第三版要求"
    assert store.event_count(state.id, "business_feedback_triggered") == 2
    assert len([c for c in FakeNative.instances[-1].calls if isinstance(c, tuple) and c[0] == "steer"]) == 2
    store.close()


async def test_native_spawn_placeholder_does_not_override_effective_child_configuration(
    tmp_path, monkeypatch
):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {
                "method": "item/started",
                "params": {
                    "threadId": "root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "model": "",
                        "reasoningEffort": "medium",
                    },
                },
            },
            {
                "method": "thread/started",
                "params": {"thread": {"id": "child", "model": "gpt-6.1-sol", "reasoningEffort": "xhigh"}},
            },
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "root"}},
            {"method": "thread/goal/updated", "params": {"threadId": "root"}, "test_goal_status": "complete"},
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "completed" and result.native_children == ["child"]
    store.close()


async def test_native_actual_child_effort_drift_is_still_rejected(tmp_path, monkeypatch):
    store, state = fixture(
        tmp_path,
        monkeypatch,
        [
            {
                "method": "thread/started",
                "params": {"thread": {"id": "child", "model": "gpt-6.1-sol", "reasoningEffort": "medium"}},
            },
        ],
    )
    result = await run_codex(store, state.id)
    assert result.phase == "failed" and result.failure_origin == "configuration"
    store.close()
