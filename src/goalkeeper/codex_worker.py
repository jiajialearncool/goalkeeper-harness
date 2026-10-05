"""Product comparator controller. It does not supply GoalKeeper's four mechanisms."""

from __future__ import annotations

import asyncio
import json
import os
import time

from .benchmark import Feedback
from .codex import CodexClient, ProtocolError, ConfigurationError
from .engine import Engine
from .provider import ScriptedProvider
from .processes import RunLease, reconcile, collect_tree, track
from .sandbox import runtime_roots
from .store import Store
from .verifier import Verifier


def native_objective(goal):
    return json.dumps(
        {"目标": goal.current, "硬约束": goal.constraints, "验收标准": goal.acceptance}, ensure_ascii=False
    )


def native_budget(state, existing_goal):
    remaining = max(1, state.config.token_budget - state.tokens_used)
    if (
        existing_goal
        and existing_goal.get("objective") == native_objective(state.goal)
        and existing_goal.get("status") != "complete"
    ):
        return remaining + existing_goal.get("tokensUsed", 0)
    return remaining


async def run_codex(store: Store, run_id: str, feedback: Feedback | list[Feedback] | None = None):
    state = store.get(run_id)
    state.config.require_current_model()
    if state.ports:
        raise ProtocolError("固定版本尚未通过指定回环端口隔离检查，拒绝调用模型。")
    lease = RunLease(store.directory, run_id)
    reconcile(store, run_id)
    client = track(store, run_id, CodexClient(state.root, runtime_roots()))
    verifier = Verifier(store)
    base, started = state.active_seconds, time.monotonic()
    active_turn = None
    usage_seen = False
    child_threads = set(state.native_children)

    def save(kind, payload=None):
        nonlocal state
        state = store.get(run_id)
        if client.process and client.process.returncode is None:
            collect_tree(store, run_id, client.process.pid)
        state.active_seconds = base + time.monotonic() - started
        for key, value in client.usage_by_thread.items():
            old = state.native_usage.get(key, {})
            old_total = old.get("inputTokens", 0) + old.get("outputTokens", 0)
            new_total = value.get("inputTokens", 0) + value.get("outputTokens", 0)
            if new_total >= old_total:
                state.native_usage[key] = value
        state.tokens_used = sum(
            v.get("inputTokens", 0) + v.get("outputTokens", 0) for v in state.native_usage.values()
        )
        state.native_children = sorted(child_threads)
        state = store.commit(state, kind, payload or {})
        return state

    async def start_turn(text):
        nonlocal active_turn
        result = await client.turn(text, state.root, runtime_roots())
        active_turn = result["turn"]["id"]
        store.event(run_id, "native_turn_started", {"thread": client.thread_id, "turn": active_turn})

    steps = feedback if isinstance(feedback, list) else ([feedback] if feedback else [])

    async def business_feedback():
        nonlocal active_turn, state
        index = store.event_count(run_id, "business_feedback_triggered")
        if index >= len(steps):
            return False
        feedback = steps[index]
        verdict = await verifier.check(state.root, [feedback.probe], state.goal.version)
        if not verdict.passed:
            return False
        store.event(
            run_id,
            "business_feedback_triggered",
            {
                "kind": feedback.kind,
                "index": index,
                "probe": verdict.model_dump(),
                "handoff": feedback.handoff,
            },
        )
        if feedback.kind == "goal_change":
            state = Engine(store, ScriptedProvider([])).update_goal(
                run_id,
                feedback.goal,
                feedback.constraints,
                feedback.acceptance,
                requirements=feedback.requirements,
            )
            await client.set_goal(
                native_objective(state.goal), max(1, state.config.token_budget - state.tokens_used)
            )
            if active_turn:
                await client.steer("用户变更目标：" + state.goal.model_dump_json(), active_turn)
        else:
            await client.set_goal(status="paused")
            if active_turn:
                await client.interrupt(active_turn)
            save("checkpoint_before_native_close")
            await client.close()
            reconcile(store, run_id)
            state = store.get(run_id)
            state.phase = "paused"
            if feedback.kind == "new_session":
                state.native_thread = None
                state.native_handoff = feedback.handoff
            store.commit(
                state,
                "program_closed_for_resume",
                {"kind": feedback.kind, "actual_process_exit": True, "native_process_exit": True},
            )
            os._exit(75)
        return True

    try:
        await client.start()
        if getattr(client, "configuration_fingerprint", None):
            from .evaluation import read
            from .paths import DATA, PROJECT

            frozen = (
                read(DATA / "private" / state.evaluation_round / "freeze.json")
                if state.evaluation_round
                else read(DATA / "private/freeze.json")
            )
            expected = frozen.get("codex_effective_configuration_fingerprint") or read(
                PROJECT / "outputs/preflight.json"
            ).get("codex", {}).get("effective_configuration_fingerprint")
            if not expected or client.configuration_fingerprint != expected:
                raise ConfigurationError("Codex 的有效配置与检查或冻结版本不一致，尚未调用模型。")
        if state.native_thread:
            # Pause the persisted Goal before resume, which can otherwise immediately auto-continue.
            client.thread_id = state.native_thread
            await client.set_goal(status="paused")
            await client.resume(state.native_thread, state.root)
            store.event(run_id, "native_session_resumed", {"thread": state.native_thread})
        else:
            state.native_thread = await client.create_thread(state.root)
            state = store.commit(state, "native_session_created", {"thread": state.native_thread})
        state.phase, state.error = "running", None
        state.failure_origin = None
        state = store.commit(state, "running")
        existing_goal = (await client.request("thread/goal/get", {"threadId": client.thread_id})).get("goal")
        await client.set_goal(
            native_objective(state.goal),
            native_budget(state, existing_goal),
            status="paused",
        )
        checks = [c.model_dump() for c in state.final_checks]
        await start_turn(
            "请完成以下任务，在任务目录内修改文件并运行检查。依赖已经准备好，工具不能访问公网。每次尝试最多两个原生子 Agent，所有调用共用预算。\n"
            + state.goal.model_dump_json()
            + "\n内部最终检查："
            + json.dumps(checks, ensure_ascii=False)
            + ("\n共同交接包：" + state.native_handoff if state.native_handoff else "")
        )
        await client.set_goal(status="active")
        while True:
            state = save("native_checkpoint")
            remaining = state.config.seconds_budget - state.active_seconds
            if remaining <= 0 or state.tokens_used >= state.config.token_budget:
                state.phase, state.error = "budget_exhausted", "尝试预算已耗尽。"
                break
            if state.phase in {"paused", "cancelled"}:
                break
            try:
                event = await asyncio.wait_for(client.events.get(), min(remaining, 15))
            except TimeoutError:
                if client.process.returncode is not None:
                    raise ProtocolError("Codex 进程退出。")
                continue
            method, params = event.get("method"), event.get("params", {})
            # Every raw event is retained separately, with credentials removed by Store.
            store.event(
                run_id,
                "native_event",
                {"method": method, "blob": store.blob(json.dumps(event, ensure_ascii=False))},
            )
            if method == "thread/tokenUsage/updated":
                usage_seen = True
            if method == "thread/started" and params.get("thread", {}).get("id") != client.thread_id:
                child_threads.add(params["thread"]["id"])
            item = params.get("item", {})
            if item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
                child_threads.update(item.get("receiverThreadIds", []))
                # An empty model plus medium effort on item/started is a CLI placeholder
                # when spawn_agent omitted both arguments; it is not effective child configuration.
                # Check explicit requests here and actual configuration on thread/started below.
                if item.get("model") not in {None, "", state.config.model} or (
                    item.get("model") == state.config.model
                    and item.get("reasoningEffort") not in {None, state.config.effort}
                ):
                    state.phase, state.error, state.failure_origin = (
                        "failed",
                        "原生子 Agent 请求了冻结配置之外的模型或推理档位。",
                        "configuration",
                    )
                    break
            if method == "thread/started":
                configured = params.get("thread", {})
                if configured.get("model") not in {None, state.config.model} or configured.get(
                    "reasoningEffort"
                ) not in {None, state.config.effort}:
                    state.phase, state.error, state.failure_origin = (
                        "failed",
                        "原生会话配置偏离固定模型。",
                        "configuration",
                    )
                    break
            if len(child_threads) > 2:
                state.phase, state.error = "failed", "原生子 Agent 数量超过冻结上限。"
                state.failure_origin = "configuration"
                break
            if params.get("threadId") not in {None, client.thread_id}:
                continue
            if method == "turn/started":
                active_turn = params.get("turn", {}).get("id", active_turn)
            if method == "item/completed":
                await business_feedback()
            if method == "error":
                error = params.get("error", {})
                if params.get("willRetry") is True:
                    # Advisory notification: let native retry continue in the same budget.
                    continue
                info = str(error).lower()
                state.phase = (
                    "resource_interrupted"
                    if any(
                        s in info
                        for s in [
                            "usage limit",
                            "usagelimit",
                            "rate limit",
                            "ratelimit",
                            "quota",
                            "overloaded",
                            "503",
                            "429",
                            "unauthorized",
                            "not signed in",
                            "responsestreamdisconnected",
                            "request timed out",
                            "connection reset",
                        ]
                    )
                    else "failed"
                )
                state.error = str(error)[:4000]
                state.failure_origin = "resource" if state.phase == "resource_interrupted" else "task"
                break
            if method in {"turn/completed", "thread/goal/updated"}:
                # Queued notifications can describe our earlier pause; use the current native state.
                goal = (await client.request("thread/goal/get", {"threadId": client.thread_id})).get("goal")
                status = (goal or {}).get("status")
                if status == "complete":
                    if active_turn and method != "turn/completed":
                        await client.interrupt(active_turn)
                    verdict = await verifier.check(state.root, state.final_checks, state.goal.version)
                    store.event(run_id, "native_final_checked", verdict.model_dump())
                    if verdict.passed:
                        state.phase = "completed"
                        break
                    repairs = state.repairs.get("final", 0) + 1
                    state.repairs["final"] = repairs
                    store.commit(state, "native_repair", {"round": repairs})
                    if repairs > state.config.repair_rounds:
                        state.phase, state.error = "failed", "最终内部检查未通过。"
                        break
                    await client.set_goal(
                        native_objective(state.goal),
                        max(1, state.config.token_budget - state.tokens_used),
                        status="paused",
                    )
                    await start_turn("最终内部检查失败，请在原预算内修复：" + verdict.model_dump_json())
                    await client.set_goal(status="active")
                elif status in {"blocked", "budgetLimited", "usageLimited", "paused"}:
                    state.phase = (
                        "resource_interrupted"
                        if status == "usageLimited"
                        else ("budget_exhausted" if status == "budgetLimited" else "failed")
                    )
                    state.error = "原生 Goal 状态：" + status
                    break
                elif method == "turn/completed":
                    active_turn = None
                    # Native active Goal drives its own continuation. Do not inject a GoalKeeper plan.
    except asyncio.CancelledError:
        state = store.get(run_id)
        state.phase = "paused" if state.phase != "cancelled" else state.phase
        state.error = "执行已中断。"
    except ProtocolError as error:
        state = store.get(run_id)
        state.phase, state.error = "failed", str(error)
        state.failure_origin = "configuration" if isinstance(error, ConfigurationError) else "integration"
    finally:
        if client.thread_id and client.process and client.process.returncode is None:
            try:
                await client.set_goal(status="paused")
                await client.request("thread/backgroundTerminals/clean", {"threadId": client.thread_id})
            except (ProtocolError, TimeoutError, OSError):
                pass
        if active_turn and client.process and client.process.returncode is None:
            try:
                await client.interrupt(active_turn)
            except (ProtocolError, TimeoutError, OSError):
                pass
        await client.close()
        reconcile(store, run_id)
        lease.close()
        phase, error, origin = state.phase, state.error, state.failure_origin
        state = save("native_process_stopped")
        state.phase, state.error = phase, error
        state.failure_origin = origin
        if state.phase == "completed" and (
            state.tokens_used > state.config.token_budget
            or state.active_seconds > state.config.seconds_budget
        ):
            state.phase, state.error, state.failure_origin = (
                "budget_exhausted",
                "终止时观测到累计预算已超限。",
                "budget",
            )
        state.usage_complete = (
            state.usage_complete and usage_seen and child_threads.issubset(state.native_usage)
        )
        state.children_used = len(child_threads)
        state.native_children = sorted(child_threads)
        state = store.commit(state, "checkpoint_stop")
    return state
