import json
import sys

import pytest

from goalkeeper import evaluation
from goalkeeper.engine import Engine
from goalkeeper.files import snapshot
from goalkeeper.models import CheckSpec, Features, Goal, ModelEvent, RunConfig, RunState
from goalkeeper.provider import ProviderError


def setup_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "DATA", tmp_path / "runtime")
    scheduler = evaluation.Scheduler(tmp_path / "package", "development", 1)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "done.txt").write_text("keep completed work")
    state = scheduler.store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="task", current="task"),
            phase="failed",
            failure_origin="integration",
            error="overloaded",
            tokens_used=100,
            active_seconds=20,
            usage_complete=False,
        )
    )
    stream = [{"type": "error", "data": {"error": {"code": "server_is_overloaded"}}}]
    raw = scheduler.store.blob(json.dumps(stream))
    scheduler.store.event(state.id, "model_finished", {"raw_stream": raw})
    scheduler.store.event(state.id, "checkpoint_stop", {})
    artifact = tmp_path / "artifact"
    checksum = snapshot(root, artifact)
    row = {
        "case_id": "R3",
        "group": "goalkeeper",
        "run_id": state.id,
        "source": "development",
        "status": "valid",
        "artifact": str(artifact),
        "snapshot": checksum,
        "score": {"passed": False},
        "grading_seconds": 2,
        "tokens": 100,
        "usage_complete": False,
        "active_seconds": 20,
    }
    scheduler.rows.append(row)
    scheduler.persist()
    return scheduler, state, row


def test_reclassify_preserves_work_budget_and_old_score(tmp_path, monkeypatch):
    scheduler, state, row = setup_interrupted(tmp_path, monkeypatch)
    before = state.model_dump()
    scheduler.prepare_development_resource_resume(state.id)
    after = scheduler.store.get(state.id)
    assert after.phase == row["status"] == "resource_interrupted"
    assert after.failure_origin == "resource" and not after.usage_complete
    for key in ("messages", "config", "tokens_used", "active_seconds", "goal", "workspace"):
        assert after.model_dump()[key] == before[key]
    assert row["interim_artifacts"][0]["score"] == {"passed": False}
    assert row["interim_artifacts"][0]["grading_seconds"] == 2
    assert "artifact" not in row and "score" not in row
    assert evaluation.fingerprint(state.root) == row["interim_artifacts"][0]["snapshot"]
    assert (state.root / "done.txt").read_text() == "keep completed work"
    assert row["interrupted_at"] > 0
    with pytest.raises(evaluation.NotReady):
        scheduler.prepare_development_resource_resume(state.id)
    scheduler.store.close()


@pytest.mark.parametrize("invalid", ["formal", "workspace", "artifact", "completed", "process", "budget"])
def test_resource_resume_rejects_unconfirmed_or_unsafe_state(tmp_path, monkeypatch, invalid):
    scheduler, state, row = setup_interrupted(tmp_path, monkeypatch)
    if invalid == "formal":
        scheduler.phase = "formal"
    elif invalid == "workspace":
        (state.root / "done.txt").write_text("changed")
    elif invalid == "artifact":
        path = tmp_path / "artifact/done.txt"
        path.chmod(0o600)
        path.write_text("tampered")
    elif invalid == "completed":
        raw = scheduler.store.blob(json.dumps([{"type": "response.completed", "data": {}}]))
        scheduler.store.event(state.id, "model_finished", {"raw_stream": raw})
        scheduler.store.event(state.id, "checkpoint_stop", {})
    elif invalid == "process":
        scheduler.store.register_process(state.id, 1234567, "synthetic")
    elif invalid == "budget":
        state.active_seconds = state.config.seconds_budget
        scheduler.store.commit(state, "synthetic_budget")
    before = scheduler.store.get(state.id).model_dump()
    with pytest.raises(evaluation.NotReady):
        scheduler.prepare_development_resource_resume(state.id)
    assert scheduler.store.get(state.id).model_dump() == before
    assert row["status"] == "valid" and "interim_artifacts" not in row
    scheduler.store.close()


async def test_engine_saves_and_resumes_overload_without_replaying_partial_write(tmp_path):
    from goalkeeper.store import Store

    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "state")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="task", current="task"),
            config=RunConfig(features=Features.baseline()),
            final_checks=[
                CheckSpec(
                    name="saved-work",
                    argv=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('done.txt').read_text() == 'saved'",
                    ],
                )
            ],
        )
    )

    class Provider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            index = len(self.requests)
            if index == 2:
                yield ModelEvent(
                    type="response.output_item.done",
                    data={
                        "item": {
                            "type": "function_call",
                            "call_id": "partial",
                            "name": "write_file",
                            "arguments": '{"path":"bad.txt","content":"do not execute"}',
                        }
                    },
                )
                yield ModelEvent(type="error", data={"error": {"code": "server_is_overloaded"}})
                raise ProviderError("server_is_overloaded", "overloaded", True)
            call = {
                "type": "function_call",
                "call_id": f"complete-{index}",
                "name": "write_file" if index == 1 else "finish",
                "arguments": json.dumps(
                    {"path": "done.txt", "content": "saved"} if index == 1 else {"summary": "done"}
                ),
            }
            yield ModelEvent(
                type="response.completed",
                data={
                    "response": {
                        "output": [call],
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                    }
                },
            )

    provider = Provider()
    first = await Engine(store, provider).run(state.id)
    assert first.phase == "resource_interrupted" and first.failure_origin == "resource"
    assert first.tokens_used == 12 and not first.usage_complete
    assert (root / "done.txt").read_text() == "saved" and not (root / "bad.txt").exists()
    second = await Engine(store, provider).run(state.id)
    assert second.phase == "completed" and second.tokens_used == 24
    assert second.active_seconds >= first.active_seconds and not second.usage_complete
    assert second.tool_calls == 2 and not (root / "bad.txt").exists()
    assert any(m.get("call_id") == "complete-1" for m in provider.requests[-1].input)
    assert not any(m.get("call_id") == "partial" for m in provider.requests[-1].input)
    store.close()
