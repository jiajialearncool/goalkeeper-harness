from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .auth import AuthBroker
from .benchmark import Feedback
from .engine import Engine
from .models import RunState
from .paths import DATA
from .provider import ResponsesProvider
from .store import Store


async def run(run_id: str, control_file: Path | None = None):
    store = Store(DATA / "private")
    auth = AuthBroker(DATA / "private")
    provider = ResponsesProvider(auth)
    engine = Engine(store, provider)
    control = json.loads(control_file.read_text()) if control_file and control_file.exists() else {}
    steps = [Feedback.model_validate(f) for f in control.get("feedback_sequence", [])]
    if not steps and control.get("feedback"):
        steps = [Feedback.model_validate(control["feedback"])]

    async def hook(identifier):
        index = store.event_count(identifier, "business_feedback_triggered")
        if index >= len(steps):
            return
        feedback = steps[index]
        state = store.get(identifier)
        verdict = await engine.verifier.check(state.root, [feedback.probe], state.goal.version, state.ports)
        if not verdict.passed:
            return
        store.event(
            identifier,
            "business_feedback_triggered",
            {
                "kind": feedback.kind,
                "index": index,
                "probe": verdict.model_dump(),
                "handoff": feedback.handoff,
            },
        )
        if feedback.kind == "goal_change":
            engine.update_goal(
                identifier,
                feedback.goal,
                feedback.constraints,
                feedback.acceptance,
                requirements=feedback.requirements,
            )
        else:
            state = store.get(identifier)
            state.phase = "paused"
            if feedback.kind == "new_session":
                archive = store.archive(
                    identifier, state.goal.version, json.dumps(state.messages, ensure_ascii=False)
                )
                state.recall_refs = [*state.recall_refs, "archive:" + archive][-10:]
                state.messages = [
                    {"role": "user", "content": state.goal.model_dump_json()},
                    {"role": "user", "content": feedback.handoff},
                ]
                state.summary = ""
            state.active_seconds = engine.elapsed(state)
            store.commit(
                state, "program_closed_for_resume", {"kind": feedback.kind, "actual_process_exit": True}
            )
            # A real program exit at a checkpoint, not an invented overnight delay.
            os._exit(75)

    if store.get(run_id).config.engine == "codex":
        from .codex_worker import run_codex

        try:
            await run_codex(store, run_id, steps)
        finally:
            await provider.close()
            await auth.close()
            store.close()
        return
    engine.hook = hook
    try:
        result: RunState = await engine.run(run_id)
        print(
            json.dumps(
                {"id": result.id, "phase": result.phase, "tokens": result.tokens_used}, ensure_ascii=False
            ),
            flush=True,
        )
    finally:
        await provider.close()
        await auth.close()
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--control", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.run_id, args.control))


if __name__ == "__main__":
    main()
