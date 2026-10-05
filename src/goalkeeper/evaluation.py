from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .auth import AuthBroker
from .benchmark import Case, fingerprint, load_cases, scheduling_order
from .engine import INSTRUCTIONS
from .files import initialize_repository, snapshot
from .grader import IndependentGrader, save_report
from .models import EFFORT, MODEL, Features, RunConfig, RunState, uid, utcnow
from .paths import DATA, PROJECT
from .pilot import prepare
from .statistics import summarize
from .reporting import mechanism_observations, write_report
from .store import Store
from .tools import TOOLS

GROUPS = ["codex", "goalkeeper", "controlled"]


class NotReady(RuntimeError):
    pass


def read(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})


def office_attempt_count() -> int:
    manifest = read(DATA / "private/f1-run.json")
    return int(bool(manifest.get("run_id"))) + len(manifest.get("prior_run_ids", []))


def source_manifest() -> dict:
    files = {}
    candidates = [
        PROJECT / "pyproject.toml",
        PROJECT / "uv.lock",
        PROJECT / "frontend/package.json",
        PROJECT / "frontend/package-lock.json",
        PROJECT / "frontend/tsconfig.json",
        PROJECT / "frontend/eslint.config.js",
        PROJECT / "frontend/index.html",
        PROJECT / "README.md",
        PROJECT / "run_goalkeeper.py",
    ]
    for directory in ["src", "frontend/src", "frontend/dist", "tests", "scripts", "docs"]:
        candidates.extend(
            p for p in (PROJECT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
    for file in candidates:
        if file.is_file():
            files[str(file.relative_to(PROJECT))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return files


def conditions() -> dict:
    preflight = read(PROJECT / "outputs/preflight.json")
    pilot = read(PROJECT / "outputs/pilot-report.json")
    checks = read(PROJECT / "outputs/engineering-checks.json")
    grading = read(PROJECT / "outputs/pilot-grader-validation.json")
    office = read(PROJECT / "outputs/f1-validation.json")
    return {
        "own_oauth_and_real_smoke": bool(preflight.get("real_smoke_passed"))
        and preflight.get("codex", {}).get("same_chatgpt_account") is True,
        "engineering_checks": checks.get("passed") is True
        and checks.get("skipped", 0) == 0
        and checks.get("source_files") == source_manifest(),
        "workbench_browser_check": read(PROJECT / "outputs/workbench-browser-checks.json").get("passed")
        is True,
        "codex_permission_isolation": preflight.get("codex", {}).get("formal_permissions_verified") is True,
        "fixed_native_codex_configuration": preflight.get("codex", {}).get("fixed_model_and_native_goal")
        is True
        and bool(preflight.get("codex", {}).get("effective_configuration_fingerprint")),
        "references_and_mutants": grading.get("all_passed") is True
        and grading.get("package_fingerprint") == fingerprint(DATA / "private/pilot-package"),
        "critical_constraint_mutant_coverage": grading.get("critical_constraint_coverage_complete") is True,
        "pilot_complete": pilot.get("base_attempts_valid") == 20,
        "no_unexplained_engineering_faults": pilot.get("no_unexplained_engineering_faults") is True,
        "real_multistage_pass": pilot.get("real_multistage_pass") is True,
        "feedback_and_two_resume_drills": pilot.get("feedback_and_two_resume_drills") is True,
        "real_concurrency_measurements": bool(pilot.get("verified_concurrency")),
        "editable_office_demo": office.get("passed") is True
        and office.get("real_agent_run") is True
        and office.get("visual_review") == "passed",
    }


def freeze(concurrency: int) -> dict:
    ready = conditions()
    missing = [key for key, value in ready.items() if not value]
    pilot = read(PROJECT / "outputs/pilot-report.json")
    if concurrency not in pilot.get("verified_concurrency", []):
        missing.append("selected_concurrency_not_measured")
    if missing:
        raise NotReady("尚不能冻结：" + ", ".join(missing))
    target = DATA / "private/freeze.json"
    if target.exists():
        raise NotReady("该版本已冻结，禁止覆盖。")
    status = subprocess.run(
        ["/usr/bin/git", "status", "--porcelain"], cwd=PROJECT, capture_output=True, text=True, check=True
    )
    if status.stdout.strip():
        raise NotReady("请先保存全部实现和检查结果的 Git 提交。")
    commit = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True, check=True
    ).stdout.strip()
    value = {
        "frozen_at": utcnow(),
        "commit": commit,
        "files": source_manifest(),
        "codex_version": "0.159.0",
        "codex_effective_configuration_fingerprint": read(PROJECT / "outputs/preflight.json")
        .get("codex", {})
        .get("effective_configuration_fingerprint"),
        "model": MODEL,
        "effort": EFFORT,
        "concurrency": concurrency,
        "groups": {
            g: RunConfig(
                engine=g, features=Features.baseline() if g == "controlled" else Features()
            ).model_dump()
            for g in GROUPS
        },
        "instructions": INSTRUCTIONS,
        "tools": TOOLS,
        "pilot_hash": fingerprint(DATA / "private/pilot-package"),
        "attempts": 36,
        "tasks": 12,
        "seconds_per_attempt": 1800,
        "tokens_per_attempt": 1_000_000,
        "non_inferiority_margin": -0.05,
        "formal_network_mode": "offline_no_loopback",
        "source_manifests": ready,
    }
    save_report(target, value)
    save_report(PROJECT / "outputs/freeze-manifest.json", value)
    return value


def verify_frozen() -> dict:
    frozen = read(DATA / "private/freeze.json")
    if not frozen:
        raise NotReady("尚未冻结 Harness，禁止制作或启动正式题。")
    if source_manifest() != frozen["files"]:
        raise NotReady("程序或配置已偏离冻结版本。")
    return frozen


class Scheduler:
    def __init__(
        self,
        package: Path,
        phase: str,
        concurrency: int,
        diagnostic_reason: str | None = None,
        round_id: str = "",
    ):
        import re

        if round_id and not re.fullmatch(r"[a-zA-Z0-9_-]+", round_id):
            raise ValueError("评测轮次名称必须是独立目录名。")
        self.round_id = round_id
        self.package, self.phase, self.concurrency = package, phase, concurrency
        self.store = Store(DATA / "private")
        self.ledger_path = (
            DATA / "private" / round_id / f"{phase}-ledger.json"
            if round_id
            else DATA / f"private/{phase}-ledger.json"
        )
        self.rows = read(self.ledger_path, [])
        self.gate = asyncio.Semaphore(concurrency)
        self.grader = IndependentGrader()
        self.active_workers = 0
        self.max_active_workers = 0
        self.measurement_started = time.monotonic()
        self.new_attempt_ids: set[str] = set()
        self.processes = {}
        self.diagnostic_reason = diagnostic_reason
        if diagnostic_reason and phase != "pilot":
            raise NotReady("正式评测禁止诊断重跑和挑选结果。")

    def persist(self):
        save_report(self.ledger_path, self.rows)

    def prepare_development_resource_resume(self, run_id: str):
        """Explicit recovery of a previously misclassified overload; never reopen formal scores."""
        if self.phase != "development":
            raise NotReady("该恢复入口只适用于开发回归，不能重开正式评测结果。")
        row = next((r for r in self.rows if r["run_id"] == run_id), None)
        if not row or row.get("source") != "development" or row.get("group") != "goalkeeper":
            raise NotReady("只能恢复本开发记录中的 GoalKeeper 尝试。")
        state = self.store.get(run_id)
        if state.evaluation_phase != "development":
            raise NotReady("任务状态不是开发尝试。")
        if state.phase != "failed" or state.failure_origin != "integration" or row["status"] != "valid":
            raise NotReady("该记录不是待核对的历史服务中断。")
        after, last_call, stopped = 0, None, None
        while batch := self.store.events(run_id, after):
            for event in batch:
                if event["kind"] == "model_finished":
                    last_call = event
                if event["kind"] == "checkpoint_stop":
                    stopped = event
            after = batch[-1]["seq"]
        if not last_call or not stopped or last_call["seq"] >= stopped["seq"]:
            raise NotReady("没有完整的模型中断及停止记录。")
        stream = json.loads((self.store.blobs / last_call["payload"]["raw_stream"]).read_text())
        if any(event["type"] == "response.completed" for event in stream):
            raise NotReady("最后一次模型请求已完成，不能按拥堵中断重新执行。")
        errors = [event["data"].get("error", {}) for event in stream if event["type"] == "error"]
        if not errors or errors[-1].get("code") != "server_is_overloaded":
            raise NotReady("原始响应没有确认模型服务拥堵。")
        if self.store.process_records(run_id):
            raise NotReady("仍有登记的执行进程，必须先核对并清理。")
        if not row.get("artifact") or fingerprint(Path(row["artifact"])) != row.get("snapshot"):
            raise NotReady("原交付物快照不存在或已改变。")
        if fingerprint(state.root) != row["snapshot"]:
            raise NotReady("工作文件已改变，不能按原中断状态直接续跑。")
        if (
            state.tokens_used >= state.config.token_budget
            or state.active_seconds >= state.config.seconds_budget
        ):
            raise NotReady("原尝试预算已耗尽，不能续跑。")
        interim = {k: v for k, v in row.items() if k != "interim_artifacts"}
        prior_phase, prior_origin = state.phase, state.failure_origin
        state.phase, state.failure_origin = "resource_interrupted", "resource"
        self.store.commit(
            state,
            "development_overload_reclassified",
            {
                "prior_phase": prior_phase,
                "prior_failure_origin": prior_origin,
                "raw_stream": last_call["payload"]["raw_stream"],
                "interim_result": interim,
            },
        )
        row.setdefault("interim_artifacts", []).append(interim)
        for field in (
            "artifact",
            "snapshot",
            "score",
            "grading_seconds",
            "grading_error",
            "execution_phase",
            "failure_origin",
            "mechanism_observations",
        ):
            row.pop(field, None)
        row.update(
            status="resource_interrupted",
            interrupted_at=datetime.fromisoformat(stopped["timestamp"]).timestamp(),
        )
        self.persist()

    async def attempt(self, case: Case, group: str):
        async with self.gate:
            if self.phase == "formal" and self.round_id:
                from .rounds import verify_round

                frozen = verify_round(self.round_id, self.package)
                if group not in frozen["groups"] or case.id not in {c.id for c in load_cases(self.package)}:
                    raise NotReady("尝试不属于本轮冻结配置。")
            previous = next((r for r in self.rows if r["case_id"] == case.id and r["group"] == group), None)
            if self.diagnostic_reason:
                previous = None
            if previous and previous["status"] == "valid":
                return previous
            if previous and previous.get("artifact"):
                # A scoring error must never launch the Agent again or replace its frozen output.
                return await self.score(case, previous, self.store.get(previous["run_id"]))
            if previous:
                row = previous
                state = self.store.get(row["run_id"])
                waited = time.time() - row.get("interrupted_at", time.time())
                state.quota_wait_seconds += max(0, waited)
                self.store.commit(state, "resource_resumed")
            else:
                if self.phase == "pilot" and len(self.rows) + office_attempt_count() >= 24:
                    raise NotReady("pilot 的最多 24 次尝试预算已经用完。")
                identifier = uid()
                workspace = DATA / "runs" / identifier / "workspace"
                shutil.copytree(self.package / case.initial, workspace)
                initialize_repository(workspace)
                config = RunConfig(
                    engine=group, features=Features.baseline() if group == "controlled" else Features()
                )
                self.store.create(
                    RunState(
                        id=identifier,
                        workspace=str(workspace),
                        goal=case.execution_goal(),
                        config=config,
                        final_checks=case.public_checks,
                        check_policies=case.check_policies,
                        ports=[
                            35100
                            + next(i for i, c in enumerate(load_cases(self.package)) if c.id == case.id) * 3
                            + GROUPS.index(group)
                        ]
                        if case.ports
                        else [],
                        evaluation_phase=self.phase,
                        evaluation_round=self.round_id,
                    )
                )
                row = {
                    "case_id": case.id,
                    "group": group,
                    "run_id": identifier,
                    "status": "running",
                    "human_rescue": False,
                    "source": self.phase,
                    "evidence_mode": "real",
                    "trial_kind": "diagnostic" if self.diagnostic_reason else "base",
                    "diagnostic_reason": self.diagnostic_reason,
                }
                self.new_attempt_ids.add(identifier)
                self.rows.append(row)
                self.persist()
            identifier = row["run_id"]
            control = DATA / "private" / f"control-{identifier}.json"
            save_report(control, {"feedback_sequence": [f.model_dump() for f in case.feedback_steps()]})
            if group == "codex" and not read(PROJECT / "outputs/preflight.json").get("codex", {}).get(
                "restricted_read_verified"
            ):
                raise NotReady("Codex 对照尚未通过实际读取隔离检查。")
            argv = [
                sys.executable,
                "-m",
                "goalkeeper.worker",
                "--run-id",
                identifier,
                "--control",
                str(control),
            ]
            env = {
                **os.environ,
                "PYTHONPATH": str(PROJECT / "src"),
                "GOALKEEPER_DATA": str(DATA),
                "TIKTOKEN_CACHE_DIR": str(PROJECT / "work/tokenizer-cache"),
            }
            for _ in range(len(case.feedback_steps()) + 1):
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=PROJECT,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.processes[identifier] = process
                self.active_workers += 1
                self.max_active_workers = max(self.max_active_workers, self.active_workers)
                try:
                    remaining = max(
                        0,
                        self.store.get(identifier).config.seconds_budget
                        - self.store.get(identifier).active_seconds,
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), remaining + 10)
                    if stderr:
                        self.store.event(
                            identifier,
                            "worker_diagnostic",
                            {"blob": self.store.blob(stderr.decode(errors="replace")[-32000:])},
                        )
                except TimeoutError:
                    import signal

                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    from .processes import reconcile

                    reconcile(self.store, identifier)
                    interrupted = self.store.get(identifier)
                    interrupted.phase, interrupted.error = "budget_exhausted", "执行进程达到总时间上限。"
                    interrupted.active_seconds = interrupted.config.seconds_budget
                    interrupted.usage_complete = False
                    self.store.commit(interrupted, "worker_timeout")
                finally:
                    self.active_workers -= 1
                    if process.returncode is not None:
                        self.processes.pop(identifier, None)
                if process.returncode != 75:
                    break
            state = self.store.get(identifier)
            if state.phase == "resource_interrupted":
                row.update(
                    status="resource_interrupted",
                    interrupted_at=time.time(),
                    error=state.error,
                    tokens=state.tokens_used,
                    usage_complete=state.usage_complete,
                    active_seconds=state.active_seconds,
                    quota_wait_seconds=state.quota_wait_seconds,
                )
                self.persist()
                return row
            if state.phase not in {"completed", "failed", "budget_exhausted", "cancelled"}:
                state.phase, state.error = "failed", "执行进程异常退出。"
                state = self.store.commit(state, "worker_failed")
            destination = DATA / "artifacts" / identifier
            if destination.exists():
                # A historically premature score may be retained as an interim
                # artifact when a confirmed transport interruption resumes.
                destination = DATA / "artifacts" / (identifier + "-" + uid()[:8])
            checksum = snapshot(state.root, destination)
            row.update(
                status="awaiting_grade",
                snapshot=checksum,
                artifact=str(destination),
                tokens=state.tokens_used,
                usage_complete=state.usage_complete,
                active_seconds=state.active_seconds,
                quota_wait_seconds=state.quota_wait_seconds,
                execution_phase=state.phase,
                error=state.error,
                failure_origin=state.failure_origin,
                evidence_index=str(DATA / "private/state.sqlite"),
            )
            self.persist()
            return await self.score(case, row, state)

    async def score(self, case, row, state):
        started = time.monotonic()
        try:
            if self.phase == "formal" and self.round_id:
                from .rounds import verify_round

                frozen = verify_round(self.round_id, self.package)
                if state.config.model_dump() != frozen["groups"][state.config.engine]:
                    raise NotReady("尝试运行配置偏离冻结版本。")
            if self.phase == "formal" and state.failure_origin == "configuration":
                raise NotReady("该尝试出现冻结配置偏离，不能计作有效正式结果；保留证据并按新轮次处理。")
            if fingerprint(Path(row["artifact"])) != row["snapshot"]:
                raise NotReady("交付物快照已改变，拒绝评分。")
            score = await self.grader.grade(case, self.package, Path(row["artifact"]), state.ports)
            if score.get("infrastructure_error"):
                row["grading_failure_evidence"] = score
                raise NotReady("独立评分程序运行异常；保存原快照，修复后只重新评分，禁止重新运行 Agent。")
            steps = case.feedback_steps()
            if steps:
                delivered = self.store.event_count(state.id, "business_feedback_triggered") == len(steps)
                resumes = sum(f.kind != "goal_change" for f in steps)
                restored = (
                    self.store.event_count(
                        state.id, "program_closed_for_resume", {"actual_process_exit": True}
                    )
                    >= resumes
                )
                score["feedback_protocol"] = {
                    "triggered": delivered,
                    "restoration_recorded": restored,
                    "expected_feedback_count": len(steps),
                    "actual_feedback_count": self.store.event_count(state.id, "business_feedback_triggered"),
                }
                score["passed"] = score["passed"] and delivered and restored
            score["execution_delivered"] = state.phase == "completed"
            score["passed"] = score["passed"] and state.phase == "completed"
            row.update(
                status="valid",
                score=score,
                grading_seconds=row.get("grading_seconds", 0) + time.monotonic() - started,
                mechanism_observations=mechanism_observations(self.store, state.id),
            )
            row.pop("grading_error", None)
        except Exception as error:
            row.update(status="grading_error", grading_error=type(error).__name__ + ": " + str(error))
            raise
        finally:
            self.persist()
        return row

    async def stop_workers(self):
        import signal
        from .processes import reconcile

        for run_id, process in list(self.processes.items()):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            reconcile(self.store, run_id)
            state = self.store.get(run_id)
            state.phase, state.error = "resource_interrupted", "调度器退出，保存同一次尝试待恢复。"
            self.store.commit(state, "scheduler_stopped")
            row = next(r for r in self.rows if r["run_id"] == run_id)
            row.update(
                status="resource_interrupted",
                interrupted_at=time.time(),
                tokens=state.tokens_used,
                active_seconds=state.active_seconds,
                usage_complete=False,
            )
        self.persist()

    async def run(self, order: list[tuple[str, str]]):
        by_id = {c.id: c for c in load_cases(self.package)}
        if any(i not in by_id or g not in GROUPS for i, g in order):
            raise NotReady("调度顺序引用了未知题目或组别。")
        if any(g == "codex" and by_id[i].ports for i, g in order):
            raise NotReady("Codex 对照尚不能运行需要本地服务端口的题目。")
        tasks = [asyncio.create_task(self.attempt(by_id[i], g)) for i, g in order]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop_workers()
            raise
        report = summarize(self.rows)
        if self.phase == "pilot":
            valid = [r for r in self.rows if r["status"] == "valid" and r.get("evidence_mode") == "real"]
            report["base_attempts_valid"] = sum(r.get("trial_kind", "base") == "base" for r in valid)
            report["engineering_faults"] = [
                r["run_id"]
                for r in valid
                if r.get("failure_origin") in {"harness", "integration", "configuration"}
                or r.get("error") == "执行进程异常退出。"
            ]
            explanations = read(DATA / "private/pilot-fault-explanations.json")
            report["no_unexplained_engineering_faults"] = all(
                explanations.get(identifier, {}).get("cause")
                and explanations.get(identifier, {}).get("fix_commit")
                and explanations.get(identifier, {}).get("diagnostic_run_id")
                for identifier in report["engineering_faults"]
            )
            passed_gk = [r for r in valid if r["group"] == "goalkeeper" and r["score"]["passed"]]
            report["real_multistage_pass"] = any(
                len(self.store.get(r["run_id"]).milestones) >= 2 for r in passed_gk
            )

            feedback_cases = {
                r["case_id"]
                for r in valid
                if r["group"] == "goalkeeper"
                and self.store.has_event(r["run_id"], "business_feedback_triggered")
            }
            report["feedback_and_two_resume_drills"] = {"D1", "D2", "E1", "E2"}.issubset(
                feedback_cases
            ) and all(
                self.store.has_event(r["run_id"], "program_closed_for_resume", {"actual_process_exit": True})
                for r in valid
                if r["group"] == "goalkeeper" and r["case_id"] in {"E1", "E2"}
            )
            measurements = read(DATA / "private/pilot-concurrency.json", [])
            measurements.append(
                {
                    "requested": self.concurrency,
                    "max_actual": self.max_active_workers,
                    "seconds": time.monotonic() - self.measurement_started,
                    "valid_new_attempts": sum(
                        r["status"] == "valid" and r["run_id"] in self.new_attempt_ids for r in self.rows
                    ),
                    "no_resource_interruptions": all(
                        r["status"] == "valid" for r in self.rows if r["run_id"] in self.new_attempt_ids
                    ),
                    "no_engineering_crashes": all(
                        r.get("error") != "执行进程异常退出。"
                        for r in self.rows
                        if r["run_id"] in self.new_attempt_ids
                    ),
                }
            )
            save_report(DATA / "private/pilot-concurrency.json", measurements)
            report["concurrency_measurements"] = measurements
            report["verified_concurrency"] = sorted(
                {
                    m["requested"]
                    for m in measurements
                    if m["max_actual"] == m["requested"]
                    and m["no_resource_interruptions"]
                    and m.get("no_engineering_crashes")
                }
            )
        write_report(
            self.phase,
            self.rows,
            report,
            directory=PROJECT / "outputs" / self.round_id if self.round_id else None,
        )
        return report


async def cli_action(args):
    package = DATA / "private/pilot-package"
    if args.command == "pilot-prepare":
        if getattr(args, "regenerate", False) and read(DATA / "private/pilot-ledger.json", []):
            raise NotReady("pilot 已有运行记录，禁止重建输入来挽救该轮结果。")
        prepare(package, force=getattr(args, "regenerate", False))
        grader = IndependentGrader()
        results = [await grader.validate(c, package) for c in load_cases(package)]
        report = {
            "results": results,
            "all_passed": all(r["passed"] for r in results),
            "critical_constraint_coverage_complete": all(
                r["critical_constraint_coverage_complete"] for r in results
            ),
            "real_model": False,
            "package_fingerprint": fingerprint(package),
        }
        save_report(PROJECT / "outputs/pilot-grader-validation.json", report)
        print(json.dumps({"all_passed": report["all_passed"], "cases": len(results)}, ensure_ascii=False))
        return
    if args.command == "freeze":
        print(json.dumps(freeze(args.concurrency), ensure_ascii=False, indent=2))
        return
    if args.command == "report":
        phase = getattr(args, "phase", "formal")
        records = read(DATA / f"private/{phase}-ledger.json", [])
        report = {**read(PROJECT / f"outputs/{phase}-report.json"), **summarize(records)}
        write_report(phase, records, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    broker = AuthBroker(DATA / "private")
    try:
        if not broker.status()["connected"]:
            raise NotReady("请先在工作台授权 GoalKeeper 自身的订阅。")
        await broker.access_token()
    finally:
        await broker.close()
    if args.command == "pilot":
        from .doctor import doctor

        preflight = await doctor()
        if (
            not preflight.get("real_smoke_passed")
            or preflight.get("codex", {}).get("same_chatgpt_account") is not True
        ):
            raise NotReady("先完成真实接入检查，并确认三组使用同一 ChatGPT 账号。")
        validation = read(PROJECT / "outputs/pilot-grader-validation.json")
        if (
            not validation.get("all_passed")
            or not validation.get("critical_constraint_coverage_complete")
            or validation.get("package_fingerprint") != fingerprint(package)
        ):
            raise NotReady("独立评分器尚未通过参考解和反例检查。")
        cases = load_cases(package)
        group = getattr(args, "group", "goalkeeper")
        order = [(c.id, group) for c in cases if group == "goalkeeper" or c.id in ["A1", "B2", "D1", "E1"]]
        selected = getattr(args, "cases", None)
        if selected:
            allowed = {c for c, g in order}
            if not set(selected).issubset(allowed):
                raise NotReady("所选题目不属于该组已确认的 pilot 范围。")
            order = [(c, g) for c, g in order if c in selected]
        reason = getattr(args, "diagnostic_reason", None)
        if reason and (not selected or len(selected) != 1):
            raise NotReady("诊断重跑需要选择一道题，并填写具体原因。")
        scheduler = Scheduler(package, "pilot", getattr(args, "concurrency", 1), reason)
    elif args.command == "evaluate":
        frozen = verify_frozen()
        from .codex import inspect_protocol

        probe = AuthBroker(DATA / "private")
        try:
            account = await inspect_protocol(
                PROJECT / "work/codex-schema-experimental", probe.status().get("email")
            )
            if account.get("same_chatgpt_account") is not True:
                raise NotReady("正式评测前账号检查未通过，禁止使用不同账号继续。")
        finally:
            await probe.close()
        package = DATA / "private/formal-package"
        cases = load_cases(package)
        if any(c.ports for c in cases):
            raise NotReady("第一版三组正式对照限定离线任务，不开放本地服务端口。")
        if any(c.source != "formal" for c in cases):
            raise NotReady("禁止把开发题改名后用作正式题。")
        if any(
            not c.environment_ready
            or not c.expected_tests
            or not c.public_checks
            or not c.mutant_expectations
            for c in cases
        ):
            raise NotReady("正式题的环境、验收或关键约束反例尚未准备完整。")
        provenance = read(package / "unseen-provenance.json")
        if provenance.get("created_after_freeze") != frozen["commit"] or not provenance.get(
            "novel_code_or_requirements_verified"
        ):
            raise NotReady("正式新题尚未验证新代码、需求组合或业务数据来源。")
        inputs = fingerprint(package)
        seal = DATA / "private/formal-input-seal.json"
        if seal.exists() and read(seal).get("fingerprint") != inputs:
            raise NotReady("正式输入或评分标准发生变化，禁止继续该轮。")
        if not seal.exists():
            grader = IndependentGrader()
            validations = [await grader.validate(case, package) for case in cases]
            validated = all(
                item["passed"] and item["critical_constraint_coverage_complete"] for item in validations
            )
            save_report(
                DATA / "private/formal-grader-validation.json",
                {"passed": validated, "results": validations, "input_fingerprint": inputs},
            )
            if not validated:
                raise NotReady("正式评分器的参考解或关键约束反例检查未通过，尚未启动任何正式尝试。")
            save_report(
                seal,
                {"fingerprint": inputs, "order": scheduling_order(cases), "frozen_commit": frozen["commit"]},
            )
        order = [tuple(item) for item in read(seal)["order"]]
        scheduler = Scheduler(package, "formal", frozen["concurrency"])
    else:
        raise ValueError("未知命令。")
    try:
        report = await scheduler.run(order)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        scheduler.store.close()
