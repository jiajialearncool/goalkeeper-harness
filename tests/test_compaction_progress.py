import json

import pytest

from goalkeeper.context import ContextLimit
from goalkeeper.engine import Engine
from goalkeeper.models import Features, Goal, ModelEvent, RunConfig, RunState
from goalkeeper.provider import ProviderError, ScriptedProvider
from goalkeeper.store import Store


class FragmentProvider(ScriptedProvider):
    def __init__(self, fail_on=None):
        super().__init__([])
        self.fail_on = fail_on

    async def stream(self, request):
        data = json.loads(request.input[0]["content"])
        if len(self.requests) + 1 == self.fail_on:
            self.requests.append(request)
            yield ModelEvent(type="response.output_text.delta", data={"delta": "partial, not usable"})
            raise ProviderError("transport_error", resource=True)
        summary = "keep cents and continue the pending check"
        if data["summary_format"] == "structured_v1":
            summary = json.dumps(
                {
                    "goal_version": data["goal_version"],
                    "decisions": ["keep cents"],
                    "unresolved": ["pending check"],
                    "position": "written but not verified",
                    "next_steps": ["check"],
                    "files": [],
                    "checks": [],
                    "source_refs": ["archive:" + data["source"]],
                }
            )
        self.responses.append([{"type": "message", "content": [{"type": "output_text", "text": summary}]}])
        async for event in super().stream(request):
            yield event


@pytest.fixture
def saved(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="keep", current="keep"),
            config=RunConfig(context_limit=8000, summary_target_tokens=400),
            summary="earlier facts",
            messages=[
                {"role": "user", "content": "alpha beta constraints " * 4000, "_boundary": True},
                {"role": "user", "content": "recent work", "_boundary": True},
            ],
        )
    )
    yield store, state
    store.close()


@pytest.mark.parametrize("features", [Features(), Features.baseline()])
async def test_interruption_keeps_live_context_and_restart_continues_at_saved_offset(saved, features):
    store, state = saved
    state.config.features = features
    state = store.commit(state, "configuration")
    first = FragmentProvider(fail_on=2)
    with pytest.raises(ProviderError):
        await Engine(store, first).compact(state)
    interrupted = store.get(state.id)
    progress = interrupted.compaction_progress
    assert progress.chunks == 1 and progress.offset > 0
    assert interrupted.messages == state.messages and interrupted.summary == state.summary
    assert interrupted.tokens_used == 125 and not interrupted.usage_complete
    assert "partial, not usable" not in progress.summary
    directory = store.directory
    store.close()
    reopened = Store(directory)
    try:
        second = FragmentProvider()
        result = await Engine(reopened, second).compact(reopened.get(state.id))
        payload = json.loads(second.requests[0].input[0]["content"])
        assert payload["offset"] == progress.offset
        assert payload["previous"] == progress.summary
        assert payload["source"] == progress.archive
        assert result.compaction_progress is None and result.messages == state.messages[-1:]
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM archives WHERE run_id=?", (state.id,)).fetchone()[0]
            == 1
        )
        assert reopened.has_event(state.id, "context_compaction_resumed")
        assert result.tokens_used == 125 * (1 + len(second.requests))
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["history", "goal", "config", "previous_summary", "foreign_archive"])
async def test_changed_sources_or_configuration_cannot_reuse_progress(saved, change):
    store, state = saved
    with pytest.raises(ProviderError):
        await Engine(store, FragmentProvider(fail_on=2)).compact(state)
    changed = store.get(state.id)
    previous_archive = changed.compaction_progress.archive
    if change == "history":
        changed.messages[0]["content"] += " new constraint"
    elif change == "goal":
        changed.goal.version += 1
        changed.goal.current = "different goal"
    elif change == "config":
        changed.config.summary_target_tokens += 1
    elif change == "previous_summary":
        changed.summary = "new earlier facts"
    else:
        changed.compaction_progress.archive = store.archive("other-run", 1, "foreign source")
    changed = store.commit(changed, "changed")
    provider = FragmentProvider()
    result = await Engine(store, provider).compact(changed)
    payload = json.loads(provider.requests[0].input[0]["content"])
    assert payload["offset"] == 0 and payload["source"] != previous_archive
    assert payload["previous"] == changed.summary
    assert result.compaction_progress is None


async def test_invalid_saved_summary_is_not_accepted_or_used_as_live_context(saved):
    store, state = saved
    with pytest.raises(ProviderError):
        await Engine(store, FragmentProvider(fail_on=2)).compact(state)
    broken = store.get(state.id)
    summary = json.loads(broken.compaction_progress.summary)
    summary["source_refs"] = ["archive:invented"]
    broken.compaction_progress.summary = json.dumps(summary)
    broken = store.commit(broken, "corrupt_checkpoint")
    provider = FragmentProvider()
    with pytest.raises(ContextLimit):
        await Engine(store, provider).compact(broken)
    assert not provider.requests
    assert store.get(state.id).messages == state.messages
    assert store.get(state.id).summary == state.summary


async def test_goal_update_clears_pending_compression(saved):
    store, state = saved
    with pytest.raises(ProviderError):
        await Engine(store, FragmentProvider(fail_on=2)).compact(state)
    result = Engine(store, FragmentProvider()).update_goal(state.id, "new goal")
    assert result.compaction_progress is None and result.summary == ""


async def test_run_resumes_summary_before_recall_changes_history(saved):
    store, state = saved
    with pytest.raises(ProviderError):
        await Engine(store, FragmentProvider(fail_on=2)).compact(state)
    interrupted = store.get(state.id)
    checkpoint = interrupted.compaction_progress
    interrupted.recall_refs = ["archive:" + checkpoint.archive]
    store.commit(interrupted, "restore_reference")

    class StopAfterCompression(FragmentProvider):
        async def stream(self, request):
            if request.tools:
                raise ProviderError("stop_after_compression", resource=True)
            async for event in super().stream(request):
                yield event

    provider = StopAfterCompression()
    result = await Engine(store, provider).run(state.id)
    payload = json.loads(provider.requests[0].input[0]["content"])
    assert payload["offset"] == checkpoint.offset and payload["source"] == checkpoint.archive
    assert result.phase == "resource_interrupted" and result.compaction_progress is None
    assert store.has_event(state.id, "sources_restored_before_work")
