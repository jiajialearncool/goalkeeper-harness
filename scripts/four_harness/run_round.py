"""Eight attempts only. Immutable protocol, separate ledger, no automatic harder round."""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from goalkeeper.benchmark import Case, fingerprint
from goalkeeper.models import RunState, RunConfig, Goal, CheckSpec, utcnow
from goalkeeper.store import Store
from goalkeeper.files import EXCLUDED, initialize_repository, snapshot
from goalkeeper.grader import IndependentGrader, save_report
from goalkeeper.paths import DATA, PROJECT
from goalkeeper.processes import reconcile
from goalkeeper.codex import CodexClient
from goalkeeper.sandbox import runtime_roots, Seatbelt, Scope
from goalkeeper.evaluation import source_manifest

ROUND = "four-harness-20261003-v2"
PRIVATE = DATA / "private" / ROUND
OUTPUT = PROJECT / "outputs/four-harness-round"
PACKAGE = PRIVATE / "package"
GROUPS = ["goalkeeper", "codex", "opencode", "pi"]


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def copy_initial_case(source, destination):
    # Validation creates .tmp scoring scripts and .home state inside private references.
    # None of these reserved files belong to an agent's initial task input.
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*EXCLUDED))


class Runner:
    def __init__(self, smoke=False, efficiency=False, focused=False, global_efficiency=False):
        self.store = Store(DATA / "private")
        self.smoke = smoke
        self.efficiency = efficiency
        self.focused = focused
        self.global_efficiency = global_efficiency
        self.ledger = PRIVATE / ("smoke-ledger.json" if smoke else "ledger.json")
        self.rows = read(self.ledger, [])
        self.processes = {}
        self.gate = asyncio.Semaphore(2)

    def persist(self):
        save_report(self.ledger, self.rows)
        save_report(
            OUTPUT / ("smoke-results.json" if self.smoke else "results.json"),
            {"attempts": self.rows, "updated_at": utcnow()},
        )

    async def attempt(self, case, group):
        async with self.gate:
            row = next((r for r in self.rows if r["case_id"] == case.id and r["group"] == group), None)
            if row and row["status"] in {"valid", "invalid_configuration", "invalid_scoring"}:
                return row
            if row:
                state = self.store.get(row["run_id"])
                if state.phase != "resource_interrupted":
                    raise RuntimeError("Existing attempt cannot be rerun")
                state.quota_wait_seconds += max(0, time.time() - row.get("interrupted_at", time.time()))
                self.store.commit(state, "four_group_resource_resumed")
            else:
                config = RunConfig(
                    engine=group,
                    seconds_budget=180 if self.smoke else 1800,
                    token_budget=200000 if self.smoke else 1000000,
                    efficiency_enabled=self.efficiency,
                    focused_context_enabled=self.focused,
                    final_review_required=self.focused,
                    global_efficiency_enabled=self.global_efficiency,
                )
                state = RunState(
                    workspace="pending",
                    goal=case.execution_goal(),
                    config=config,
                    final_checks=case.public_checks,
                    check_policies=case.check_policies,
                    evaluation_phase="development",
                    evaluation_round=ROUND,
                )
                root = DATA / "runs" / state.id / "workspace"
                copy_initial_case(PACKAGE / case.initial, root)
                initialize_repository(root)
                state.workspace = str(root)
                self.store.create(state)
                row = {
                    "case_id": case.id,
                    "group": group,
                    "run_id": state.id,
                    "status": "running",
                    "model": config.model,
                    "effort": config.effort,
                    "source": "efficiency_development_regression"
                    if self.efficiency
                    else "integration_smoke"
                    if self.smoke
                    else "held_out_two_task_comparison",
                    "human_rescue": False,
                    "started_at": utcnow(),
                }
                self.rows.append(row)
                self.persist()
            control = PRIVATE / ("control-" + state.id + ".json")
            save_report(control, {"feedback_sequence": [f.model_dump() for f in case.feedback_steps()]})
            module = "goalkeeper.external_worker" if group in {"opencode", "pi"} else "goalkeeper.worker"
            env = {
                **os.environ,
                "PYTHONPATH": str(PROJECT / "src"),
                "GOALKEEPER_DATA": str(DATA),
                "TIKTOKEN_CACHE_DIR": str(PROJECT / "work/tokenizer-cache"),
            }
            for turn in range(len(case.feedback_steps()) + 1):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    module,
                    "--run-id",
                    state.id,
                    "--control",
                    str(control),
                    cwd=PROJECT,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.processes[state.id] = process
                remain = max(0, state.config.seconds_budget - self.store.get(state.id).active_seconds)
                try:
                    _, stderr = await asyncio.wait_for(process.communicate(), remain + 10)
                    if stderr:
                        self.store.event(
                            state.id,
                            "four_group_worker_diagnostic",
                            {"blob": self.store.blob(stderr.decode(errors="replace")[-32000:])},
                        )
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    reconcile(self.store, state.id)
                    state = self.store.get(state.id)
                    state.phase = "budget_exhausted"
                    state.active_seconds = state.config.seconds_budget
                    state.usage_complete = False
                    state.failure_origin = "budget"
                    state.error = "30 minute deadline reached"
                    self.store.commit(state, "four_group_timeout")
                finally:
                    self.processes.pop(state.id, None)
                if process.returncode != 75:
                    break
            state = self.store.get(state.id)
            if state.phase not in {"completed", "failed", "budget_exhausted", "resource_interrupted"}:
                state.phase = "failed"
                state.failure_origin = "integration"
                state.error = "Controller exited without a terminal attempt state"
                self.store.commit(state, "four_group_nonterminal_exit")
            row.update(
                execution_phase=state.phase,
                active_seconds=state.active_seconds,
                tokens=state.tokens_used,
                usage_complete=state.usage_complete,
                quota_wait_seconds=state.quota_wait_seconds,
                error=state.error,
                failure_origin=state.failure_origin,
            )
            if state.phase == "resource_interrupted":
                row.update(status="resource_interrupted", interrupted_at=time.time())
                self.persist()
                return row
            if state.failure_origin in {"configuration", "integration"}:
                row["status"] = "invalid_configuration"
                self.persist()
                return row
            artifact = DATA / "artifacts" / state.id
            row["snapshot"] = snapshot(state.root, artifact)
            row["artifact"] = str(artifact)
            score = await IndependentGrader().grade(case, PACKAGE, artifact)
            score["artifact_passed"] = score["passed"]
            score["execution_delivered"] = state.phase == "completed"
            score["expected_feedback_count"] = len(case.feedback_steps())
            score["actual_feedback_count"] = self.store.event_count(state.id, "business_feedback_triggered")
            resume = sum(f.kind != "goal_change" for f in case.feedback_steps())
            score["restoration_recorded"] = (
                self.store.event_count(state.id, "program_closed_for_resume", {"actual_process_exit": True})
                >= resume
            )
            score["passed"] = (
                score["passed"]
                and score["execution_delivered"]
                and score["expected_feedback_count"] == score["actual_feedback_count"]
                and score["restoration_recorded"]
            )
            row.update(
                score=score,
                status="invalid_scoring" if score.get("infrastructure_error") else "valid",
                finished_at=utcnow(),
            )
            self.persist()
            print(
                json.dumps(
                    {
                        "case": case.id,
                        "group": group,
                        "phase": state.phase,
                        "passed": score["passed"],
                        "artifact_passed": score["artifact_passed"],
                        "seconds": state.active_seconds,
                        "tokens": state.tokens_used,
                        "failed_tests": score["failed_tests"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return row

    async def close(self):
        for rid, p in list(self.processes.items()):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await p.wait()
            reconcile(self.store, rid)
        self.store.close()


async def preflight():
    root = DATA / "validation-work" / ROUND / "codex-config"
    root.mkdir(parents=True, exist_ok=True)
    initialize_repository(root)
    client = CodexClient(root, runtime_roots())
    try:
        await client.start()
        finger = client.configuration_fingerprint
        save_report(
            PRIVATE / "freeze.json",
            {
                "round": ROUND,
                "codex_effective_configuration_fingerprint": finger,
                "model": "gpt-6.1-sol",
                "effort": "xhigh",
                "scope": "preflight only; not scored inputs freeze",
            },
        )
        save_report(
            OUTPUT / "codex-configuration.json",
            {
                "configuration_fingerprint": finger,
                "model": "gpt-6.1-sol",
                "effort": "xhigh",
                "fixed_version": "0.159.0",
            },
        )
    finally:
        await client.close()
    # Actual cross-workspace/private/network denials for external process profile.
    test = DATA / "validation-work" / ROUND / "external-permissions"
    test.mkdir(parents=True, exist_ok=True)
    outside = PRIVATE / "deny-canary"
    outside.write_text("private evaluation canary")
    sb = Seatbelt(
        Scope(
            test,
            runtimes=runtime_roots(),
            extra_read=[PROJECT / "work/four-harness-tools", Path("/private/var/db/timezone")],
            ports=[39127],
        )
    )
    checks = {}
    for name, code in [
        ("private_read", f"from pathlib import Path;Path({str(outside)!r}).read_text()"),
        (
            "outside_write",
            f"from pathlib import Path;Path({str(PRIVATE / 'must-not-write')!r}).write_text('x')",
        ),
        ("public_network", "import socket;socket.create_connection(('1.1.1.1',443),timeout=1)"),
    ]:
        r = await sb.run([sys.executable, "-c", code], 5)
        checks[name] = r["exit_code"] != 0 and not r["timed_out"]
    save_report(
        OUTPUT / "permission-checks.json",
        {"passed": all(checks.values()), "checks": checks, "real_model_calls": 0},
    )
    if not all(checks.values()):
        raise RuntimeError("External permission checks failed")
    root = PACKAGE / "SMOKE/initial"
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("Create result.txt containing exactly READY. Do not change README.md.")
    hidden = PACKAGE / "SMOKE/test_independent.py"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(
        "from pathlib import Path\ndef test_marker():\n assert (Path.cwd()/'result.txt').read_text().strip()=='READY'\n"
    )
    return Case(
        id="SMOKE",
        category="debug",
        title="工具闭环接入检查",
        initial="SMOKE/initial",
        goal=Goal(
            original="创建 result.txt，内容恰为 READY；运行检查后交付。",
            current="创建 result.txt，内容恰为 READY；运行检查后交付。",
            acceptance=["文件正确且已经交付。"],
        ),
        public_checks=[
            CheckSpec(
                name="marker",
                argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path;assert Path('result.txt').read_text().strip()=='READY'",
                ],
            )
        ],
        grader_file="SMOKE/test_independent.py",
        expected_tests=["test_marker"],
        editable=["result.txt"],
        reference="SMOKE/initial",
        mutants=[],
        source="formal",
        environment_ready=True,
    )


async def main(mode):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if mode == "preflight":
        smoke = await preflight()
        r = Runner(True)
        try:
            rows = await asyncio.gather(*(r.attempt(smoke, g) for g in GROUPS))
            print(
                json.dumps(
                    {"preflight_passed": all(x.get("score", {}).get("passed") for x in rows)},
                    ensure_ascii=False,
                )
            )
        finally:
            await r.close()
        return
    protocol = read(PRIVATE / "protocol-seal.json")
    if not protocol:
        raise RuntimeError("Protocol has not been sealed")
    if source_manifest() != protocol["source_files"] or fingerprint(PACKAGE) != protocol["package_hash"]:
        raise RuntimeError("Protocol/source drift")
    cases = [Case.model_validate(c) for c in read(PACKAGE / "manifest.json")["cases"]]
    lookup = {c.id: c for c in cases}
    r = Runner()
    try:
        started = time.monotonic()
        rows = await asyncio.gather(*(r.attempt(lookup[c], g) for c, g in protocol["order"]))
        # At most two transport-only resumptions in same run/budget; no task failure reruns.
        for retry in range(2):
            pending = [x for x in rows if x["status"] == "resource_interrupted"]
            if not pending:
                break
            await asyncio.sleep(10)
            await asyncio.gather(*(r.attempt(lookup[x["case_id"]], x["group"]) for x in pending))
            rows = r.rows
        summary = {
            g: {
                "valid": sum(x["status"] == "valid" for x in rows if x["group"] == g),
                "success": sum(x.get("score", {}).get("passed", False) for x in rows if x["group"] == g),
                "tokens": sum(x.get("tokens", 0) for x in rows if x["group"] == g),
                "active_seconds": sum(x.get("active_seconds", 0) for x in rows if x["group"] == g),
                "usage_complete": all(x.get("usage_complete", False) for x in rows if x["group"] == g),
            }
            for g in GROUPS
        }
        save_report(
            OUTPUT / "report.json",
            {
                "model": protocol["model"],
                "effort": protocol["effort"],
                "attempts": rows,
                "summary": summary,
                "elapsed_seconds": time.monotonic() - started,
                "complete": len(rows) == 8 and all(x["status"] == "valid" for x in rows),
                "scope": "Two held-out multi-stage cases, one run per group; not statistical proof of general superiority",
            },
        )
        print(
            json.dumps(
                {"summary": summary, "complete": all(x["status"] == "valid" for x in rows)},
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        await r.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["preflight", "evaluate"])
    asyncio.run(main(p.parse_args().mode))
