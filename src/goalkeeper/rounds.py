"""Independent optimization round, preserving the first formal experiment."""

from __future__ import annotations
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from .benchmark import fingerprint, load_cases, scheduling_order
from .codex import CODEX, PINNED_VERSION
from .evaluation import NotReady, read, source_manifest, Scheduler, GROUPS
from .grader import IndependentGrader, save_report
from .models import MODEL, EFFORT, RunConfig, Features, utcnow
from .paths import DATA, PROJECT
from .reporting import write_report
from .statistics import summarize

ROUND = "optimization-formal-v2"


def binary_manifest():
    return {
        n: hashlib.sha256((CODEX.parent / n).read_bytes()).hexdigest()
        for n in ["codex", "codex-code-mode-host", "rg"]
    }


def calibration_rows():
    base = read(PROJECT / "outputs/optimization-round/calibration-v2-report.json")
    corrected = read(PROJECT / "outputs/optimization-round/calibration-installation-resume.json")
    merged = {r["run_id"]: r for r in base.get("attempts", [])}
    merged.update({r["run_id"]: r for r in corrected.get("attempts", [])})
    repaired = read(PROJECT / "outputs/optimization-round/calibration-program-resume.json")
    merged.update({r["run_id"]: r for r in repaired.get("attempts", [])})
    return base, list(merged.values())


def freeze_round():
    directory = DATA / "private" / ROUND
    target = directory / "freeze.json"
    if target.exists():
        raise NotReady("该轮已冻结，禁止覆盖。")
    checks = read(PROJECT / "outputs/engineering-checks.json")
    if (
        not checks.get("passed")
        or checks.get("skipped") != 0
        or checks.get("source_files") != source_manifest()
    ):
        raise NotReady("当前源码尚未通过完整工程检查。")
    regress = read(PROJECT / "outputs/optimization-round/regression-final.json")
    if not regress.get("all_valid") or regress.get("passes") != 8:
        raise NotReady("旧题回归尚未完整验收。")
    base, rows = calibration_rows()
    scaled = read(PROJECT / "outputs/optimization-round/scaled-calibration-report.json")
    combined = rows + scaled.get("attempts", [])
    if (
        len(rows) != 6
        or len(scaled.get("attempts", [])) != 2
        or any(r["status"] != "valid" for r in combined)
    ):
        raise NotReady("开发规模校准仍有未解决的资源中断。")
    if any(r.get("failure_origin") in ["harness", "integration", "configuration"] for r in combined):
        raise NotReady("开发规模校准有未解决的工程故障。")
    if base.get("concurrency_measured") != 4:
        raise NotReady("四并发没有实跑依据。")
    if any(
        not read(PROJECT / ("outputs/optimization-round/" + n)).get("all_passed")
        for n in ["calibration-v2-grader-validation.json", "scaled-grader-validation.json"]
    ):
        raise NotReady("开发评分程序未通过检查。")
    preflight = read(PROJECT / "outputs/preflight.json")
    if not preflight.get("real_smoke_passed") or not all(
        preflight.get("codex", {}).get(k)
        for k in ["formal_permissions_verified", "same_chatgpt_account", "fixed_model_and_native_goal"]
    ):
        raise NotReady("接入和权限条件不满足。")
    status = subprocess.run(
        ["/usr/bin/git", "status", "--porcelain"], cwd=PROJECT, capture_output=True, text=True, check=True
    )
    if status.stdout.strip():
        raise NotReady("必须先保存所有程序和文档的 Git 提交。")
    commit = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True, check=True
    ).stdout.strip()
    value = {
        "round": ROUND,
        "frozen_at": utcnow(),
        "commit": commit,
        "files": source_manifest(),
        "codex_version": PINNED_VERSION,
        "codex_components": binary_manifest(),
        "codex_effective_configuration_fingerprint": preflight["codex"][
            "effective_configuration_fingerprint"
        ],
        "model": MODEL,
        "effort": EFFORT,
        "concurrency": 4,
        "attempts": 36,
        "tasks": 12,
        "seconds_per_attempt": 1800,
        "tokens_per_attempt": 1_000_000,
        "non_inferiority_margin": -0.05,
        "formal_network_mode": "offline_no_loopback",
        "groups": {
            g: RunConfig(
                engine=g, features=Features.baseline() if g == "controlled" else Features()
            ).model_dump()
            for g in GROUPS
        },
        "calibration_attempt_ids": [r["run_id"] for r in combined],
        "scope": "first three optimizations; child compaction, extra summary fallback, background memory and recovery overhaul deferred",
        "task_protocol": "8 expanded integrated multi-stage projects and 4 ordinary projects; unchanged 3/3/2/2/2 categories; no forced context reduction or log padding; empirical compression coverage reported",
        "development_templates_authored_before_freeze": True,
        "formal_instances_not_used_for_harness_debug": True,
        "first_formal_preserved": True,
    }
    save_report(target, value)
    save_report(PROJECT / "outputs/optimization-round/freeze-manifest.json", value)
    return value


def verify_round(round_id=ROUND, package: Path | None = None, sealed=True):
    frozen = read(DATA / "private" / round_id / "freeze.json")
    if not frozen or frozen["round"] != round_id:
        raise NotReady("该轮没有有效冻结版本。")
    if source_manifest() != frozen["files"] or binary_manifest() != frozen["codex_components"]:
        raise NotReady("程序或 Codex 安装组件偏离冻结版本。")
    if sealed:
        seal = read(DATA / "private" / round_id / "input-seal.json")
        if (
            not seal
            or seal["frozen_commit"] != frozen["commit"]
            or fingerprint(package or DATA / "private" / round_id / "formal-package") != seal["fingerprint"]
        ):
            raise NotReady("正式输入和评分规则未封存，或已经发生变化。")
    return frozen


async def seal_round():
    frozen = verify_round(sealed=False)
    directory = DATA / "private" / ROUND
    package = directory / "formal-package"
    if (directory / "input-seal.json").exists():
        verify_round(package=package)
        return read(directory / "input-seal.json")
    cases = load_cases(package)
    if any(c.source != "formal" or not c.environment_ready or c.ports for c in cases):
        raise NotReady("正式任务配置不符合协议。")
    if read(package / "provenance.json").get("frozen_commit") != frozen["commit"]:
        raise NotReady("正式新题来源不匹配冻结版本。")
    grader = IndependentGrader()
    rows = []
    from .verifier import Verifier

    for case in cases:
        result = await grader.validate(case, package)
        public = await Verifier().check(package / case.reference, case.public_checks, 1)
        result["public_reference_passed"] = public.passed
        # Check intermediate feedback probes independently before any formal model call.
        for step in case.feedback_steps():
            if step.kind != "goal_change":
                continue
            argv = step.probe.argv
            if "-c" in argv:
                # V2 probe is intentionally false for final V3 reference; verify its
                # rule by a isolated V2 reference copy, never by editing final inputs.
                import shutil

                root = DATA / "validation-work" / ROUND / case.id
                if root.exists():
                    shutil.rmtree(root)
                shutil.copytree(package / case.reference, root)
                engine = root / "app/engine.py"
                text = engine.read_text()
                old = 'if row["tenant"] == tenant:'
                new = 'if row["tenant"] == tenant and (start is None or row["at"] >= start) and (end is None or row["at"] < end):'
                if old not in text:
                    raise NotReady("中间反馈的参考实现边界无法核对。")
                result["intermediate_probe_expected_false_on_final"] = not (
                    await Verifier().check(root, [step.probe], 3)
                ).passed
                engine.write_text(text.replace(old, new))
                result["intermediate_probe_passed_on_v2"] = (
                    await Verifier().check(root, [step.probe], 2)
                ).passed
        rows.append(result)
    result = {
        "passed": all(
            r["passed"]
            and r["critical_constraint_coverage_complete"]
            and r["public_reference_passed"]
            and r.get("intermediate_probe_expected_false_on_final", True)
            and r.get("intermediate_probe_passed_on_v2", True)
            for r in rows
        ),
        "results": rows,
        "input_fingerprint": fingerprint(package),
        "real_model_calls": 0,
    }
    save_report(PROJECT / "outputs/optimization-round/formal-grader-validation.json", result)
    if not result["passed"]:
        raise NotReady("新题参考解或关键要求反例验证未通过，尚未启动模型。")
    value = {
        "fingerprint": fingerprint(package),
        "order": scheduling_order(cases),
        "frozen_commit": frozen["commit"],
        "sealed_at": utcnow(),
        "validation_hash": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
    }
    save_report(directory / "input-seal.json", value)
    save_report(PROJECT / "outputs/optimization-round/input-seal.json", value)
    return value


async def evaluate_round():
    directory = DATA / "private" / ROUND
    package = directory / "formal-package"
    frozen = verify_round(package=package)
    from .auth import AuthBroker
    from .codex import inspect_protocol

    auth = AuthBroker(DATA / "private")
    try:
        await auth.access_token()
        account = await inspect_protocol(
            PROJECT / "work/codex-schema-experimental", auth.status().get("email")
        )
        if not account.get("same_chatgpt_account"):
            raise NotReady("三组账号不一致。")
    finally:
        await auth.close()
    scheduler = Scheduler(package, "formal", frozen["concurrency"], round_id=ROUND)
    started = utcnow()
    try:
        order = read(directory / "input-seal.json")["order"]
        by_id = {c.id: c for c in load_cases(package)}
        rows = await asyncio.gather(*(scheduler.attempt(by_id[i], g) for i, g in order))
        verify_round(package=package)
        report = {
            **summarize(rows),
            "round": ROUND,
            "started_at": started,
            "reported_at": utcnow(),
            "frozen_commit": frozen["commit"],
            "attempts": rows,
            "max_parallel_workers": scheduler.max_active_workers,
            "coverage": {
                "natural_compactions": sum(
                    r.get("mechanism_observations", {}).get("context_compactions", 0) for r in rows
                ),
                "goal_changes": sum(r.get("mechanism_observations", {}).get("goal_changes", 0) for r in rows),
                "actual_resume_drills": sum(
                    r.get("mechanism_observations", {}).get("process_resume_drills", 0) for r in rows
                ),
            },
            "task_family_limit": "Private synthetic event-processing projects share a framework. No public benchmark or proof across all long tasks. Per-group single runs cannot measure repeat variance.",
        }
        output = PROJECT / "outputs/optimization-round/formal"
        write_report("formal", rows, report, directory=output)
        print(
            json.dumps(
                {
                    "complete": report["complete"],
                    "valid": report["valid_attempts"],
                    "groups": report["groups"],
                },
                ensure_ascii=False,
            )
        )
        return report
    finally:
        await scheduler.stop_workers()
        scheduler.store.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["freeze", "seal", "evaluate", "verify"])
    args = parser.parse_args()
    if args.action == "freeze":
        print(json.dumps(freeze_round(), ensure_ascii=False))
    elif args.action == "verify":
        print(json.dumps(verify_round(), ensure_ascii=False))
    else:
        asyncio.run(seal_round() if args.action == "seal" else evaluate_round())
