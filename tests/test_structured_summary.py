import json

import pytest

from goalkeeper.context import ContextLimit
from goalkeeper.engine import Engine
from goalkeeper.models import Goal, RunState
from goalkeeper.provider import ScriptedProvider
from goalkeeper.store import Store
from goalkeeper.summary import validate


class SummaryProvider(ScriptedProvider):
    def __init__(self, mutate):
        super().__init__([])
        self.mutate = mutate

    async def stream(self, request):
        data = json.loads(request.input[0]["content"])
        ref = "archive:" + data["source"]
        value = {
            "goal_version": data["goal_version"],
            "decisions": ["保留十进制舍入。"],
            "unresolved": ["报告尚未验证。"],
            "position": "已写文件，尚未验收。",
            "next_steps": ["运行报告检查。"],
            "files": ["money.py", "report.py"],
            "checks": [
                {"scope": "配置检查 13 项通过", "result": "passed", "evidence_refs": [ref]},
                {"scope": "报告检查尚未运行", "result": "not_run", "evidence_refs": [ref]},
            ],
            "source_refs": [ref],
        }
        text = self.mutate(value)
        if isinstance(text, dict):
            text = json.dumps(text, ensure_ascii=False)
        self.responses.append([{"type": "message", "content": [{"type": "output_text", "text": text}]}])
        async for event in super().stream(request):
            yield event


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="keep", current="keep"),
            summary="之前的有效摘要。",
            messages=[
                {"role": "user", "content": "old work " * 12000, "_boundary": True},
                {"role": "user", "content": "recent work", "_boundary": True},
            ],
        )
    )
    store.archive(state.id, 1, "plain searchable fact")  # Not every archive is a JSON message array.
    yield store, state
    store.close()


async def test_structured_summary_preserves_unverified_work_and_reference(history):
    store, state = history
    provider = SummaryProvider(lambda v: v)
    compacted = await Engine(store, provider).compact(state)
    summary = json.loads(compacted.summary)
    assert summary["unresolved"] == ["报告尚未验证。"]
    assert [c["result"] for c in summary["checks"]] == ["passed", "not_run"]
    assert len(provider.requests) == 1
    payload = json.loads(provider.requests[0].input[0]["content"])
    assert payload["previous"] == "之前的有效摘要。"
    assert summary["source_refs"] == ["archive:" + payload["source"]]
    assert "不逐项复述测试名称" in provider.requests[0].instructions
    assert store.has_event(state.id, "summary_validated")


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "wrong_type",
        "wrong_version",
        "foreign_reference",
        "wrong_result",
        "too_long",
        "duplicate_key",
        "plain_text",
    ],
)
async def test_invalid_summary_preserves_history_without_retry_or_truncation(history, invalid):
    store, state = history

    def mutate(value):
        if invalid == "missing":
            value.pop("next_steps")
        elif invalid == "wrong_type":
            value["files"] = "money.py"
        elif invalid == "wrong_version":
            value["goal_version"] += 1
        elif invalid == "foreign_reference":
            other = store.archive("another-task", 1, "foreign fact")
            value["checks"][0]["evidence_refs"] = ["archive:" + other]
        elif invalid == "wrong_result":
            value["checks"][1]["result"] = "probably_passed"
        elif invalid == "too_long":
            value["position"] = " full detail" * 12000
        elif invalid == "duplicate_key":
            return json.dumps(value)[:-1] + ',"position":"replaced"}'
        elif invalid == "plain_text":
            return "已完成，继续即可。"
        return value

    provider = SummaryProvider(mutate)
    with pytest.raises(ContextLimit):
        await Engine(store, provider).compact(state)
    current = store.get(state.id)
    assert current.messages == state.messages and current.summary == state.summary
    assert current.goal == state.goal and len(provider.requests) == 1
    assert store.has_event(state.id, "summary_rejected") and not store.has_event(
        state.id, "context_compacted"
    )
    raw = store.db.execute(
        "SELECT text FROM archives WHERE run_id=? AND text LIKE '[%'", (state.id,)
    ).fetchone()[0]
    assert json.loads(raw) == state.messages[:1]
    assert store.has_event(state.id, "model_finished")  # Complete generated output is saved as a blob.


async def test_concise_target_is_soft_and_complete_valid_summary_is_not_cut(history):
    store, state = history

    def expand(value):
        value["position"] = "important fact " * 2000
        return value

    provider = SummaryProvider(expand)
    engine = Engine(store, provider)
    compacted = await engine.compact(state)
    assert engine.context.count(compacted.summary) > state.config.summary_target_tokens
    assert json.loads(compacted.summary)["position"] == ("important fact " * 2000).strip()
    event = next(e for e in store.events(state.id) if e["kind"] == "summary_validated")
    assert event["payload"]["above_target"] is True and len(provider.requests) == 1


def test_raw_call_reference_is_normalized_only_when_owned():
    value = {
        "goal_version": 1,
        "decisions": [],
        "unresolved": [],
        "position": "等待检查。",
        "next_steps": ["核对结果。"],
        "files": [],
        "checks": [{"scope": "检查已通过", "result": "passed", "evidence_refs": ["call_known"]}],
        "source_refs": ["archive:owned", "call_known"],
    }
    allowed = {"archive:owned", "call:call_known"}
    encoded = validate(json.dumps(value), 1, "owned", allowed, 9600, lambda t: len(t))
    assert json.loads(encoded)["checks"][0]["evidence_refs"] == ["call:call_known"]
    assert json.loads(encoded)["source_refs"][-1] == "call:call_known"
    value["checks"][0]["evidence_refs"] = ["call_unknown"]
    with pytest.raises(ContextLimit, match="来源引用"):
        validate(json.dumps(value), 1, "owned", allowed, 9600, lambda t: len(t))


def test_fixed_call_prefix_is_restored_only_after_exact_owned_id_lookup():
    from goalkeeper.summary import validate
    from goalkeeper.context import ContextLimit
    import json
    import pytest

    payload = {
        "goal_version": 1,
        "decisions": [],
        "unresolved": [],
        "position": "继续",
        "next_steps": ["检查"],
        "files": [],
        "checks": [{"scope": "测试", "result": "passed", "evidence_refs": ["call:known"]}],
        "source_refs": ["archive:a", "call:known"],
    }
    value = json.loads(validate(json.dumps(payload), 1, "a", {"archive:a", "call:call_known"}, 9600, len))
    assert value["source_refs"] == ["archive:a", "call:call_known"]
    payload["checks"][0]["evidence_refs"] = ["call:invented"]
    with pytest.raises(ContextLimit):
        validate(json.dumps(payload), 1, "a", {"archive:a", "call:call_known"}, 9600, len)
