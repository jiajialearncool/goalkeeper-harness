from __future__ import annotations

from .grader import save_report
from .paths import PROJECT
from .statistics import summarize

NAMES = {"codex": "Codex", "goalkeeper": "完整 GoalKeeper", "controlled": "受控基线"}


def mechanism_observations(store, run_id):
    result = {
        "scope": "native_adapter_events_only"
        if store.get(run_id).config.engine == "codex"
        else "goalkeeper_events",
        "context_compactions": 0,
        "recall_requests": 0,
        "recall_requests_with_hits": 0,
        "stage_checks_passed": 0,
        "stage_checks_failed": 0,
        "goal_changes": 0,
        "process_resume_drills": 0,
        "proactive_restorations": 0,
        "source_reads": 0,
        "checks_reused": 0,
        "checks_executed": 0,
    }
    after = 0
    while batch := store.events(run_id, after):
        for event in batch:
            kind, payload = event["kind"], event["payload"]
            if kind == "sources_restored_before_work" and payload.get("sources"):
                result["proactive_restorations"] += 1
            elif kind == "source_read":
                result["source_reads"] += 1
            elif kind == "check_reused":
                result["checks_reused"] += 1
            elif kind == "check_executed":
                result["checks_executed"] += 1
            elif kind == "context_compacted":
                result["context_compactions"] += 1
            elif kind == "recall":
                result["recall_requests"] += 1
                result["recall_requests_with_hits"] += bool(payload.get("hits"))
            elif kind == "stage_checked":
                result["stage_checks_passed" if payload.get("passed") else "stage_checks_failed"] += 1
            elif kind == "goal_changed":
                result["goal_changes"] += 1
            elif kind == "program_closed_for_resume" and payload.get("actual_process_exit"):
                result["process_resume_drills"] += 1
        after = batch[-1]["seq"]
    return result


def write_report(phase, rows, report=None, directory=None):
    report = report or summarize(rows)
    lines = [
        f"# {'开发 pilot' if phase == 'pilot' else '正式对照评测'}报告",
        "",
        f"已取得 {report['valid_attempts']} 次有效结果。开发数据不能计入正式成绩。"
        if phase == "pilot"
        else f"已取得 {report['valid_attempts']}/36 次有效结果。",
        "",
        "| 组别 | 有效尝试 | 成功 | 成功率 | 可观测 token | 全部消耗÷成功数 | 执行分钟 | 额度等待分钟 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for g in NAMES:
        s = report["groups"][g]
        rate = "—" if s["success_rate"] is None else f"{s['success_rate']:.1%}"
        cost = "—" if s["all_tokens_per_success"] is None else f"{s['all_tokens_per_success']:,.0f}"
        lines.append(
            f"| {NAMES[g]} | {s['attempts']} | {s['successes']} | {rate} | {s['tokens']:,} | {cost} | {s['execution_seconds'] / 60:.1f} | {s['quota_wait_seconds'] / 60:.1f} |"
        )
    lines.extend(
        [
            "",
            "全部尝试消耗包括失败和资源中断已经发生的消耗。缓存输入和推理输出属于输入、输出的子集，不能再次相加。",
            "",
        ]
    )
    if phase == "pilot":
        diagnostic = sum(r.get("trial_kind") == "diagnostic" for r in rows)
        lines.append(
            f"以上汇总包含全部开发消耗，其中 {diagnostic} 次为诊断尝试。诊断结果不覆盖基础尝试，配对比较只使用基础尝试。pilot 中的实现和评分修复均保留记录，不能据此声称已在冻结条件下领先。"
        )
    lines.append(
        "独立评分耗时："
        + "；".join(f"{NAMES[g]} {report['groups'][g]['grading_seconds']:.1f} 秒" for g in NAMES)
        + "。资源等待列包含连接中断与额度中断的恢复等待；开发阶段也包括接入修复期间的等待。"
    )
    for against, comparison in report["comparisons"].items():
        success = comparison["success"]
        lo, hi = success["interval"]
        label = "证据支持领先" if success["leading"] else "未能证明领先"
        observed = "观察到更高" if success["difference"] > 0 else "没有观察到更高"
        lines.append(
            f"与 {NAMES[against]} 配对 {comparison['paired_tasks']} 题：成功率差 {success['difference']:+.1%}，95% 区间 [{lo:+.1%}, {hi:+.1%}]；{observed}，{label}。非劣界限为负五个百分点，当前区间{'支持' if success['non_inferior'] else '未支持'}不劣。"
        )
        for metric, name, divisor, unit in [
            ("observed_tokens", "每题可观测 token 差", 1, "token"),
            ("execution_seconds", "每题执行时间差", 60, "分钟"),
        ]:
            if metric in comparison:
                cost = comparison[metric]
                low, high = cost["interval"]
                lines.append(
                    f"{name} {cost['difference'] / divisor:+,.1f} {unit}，"
                    f"按任务配对重采样的 95% 区间 [{low / divisor:+,.1f}, {high / divisor:+,.1f}]。"
                )
        if phase != "formal" or not report["complete"]:
            lines.append("以上为开发结果或未完成数据，不能作为正式领先结论。")
    lines.extend(
        [
            "",
            "| 题目 | 组别 | 运行类型 | 状态 | 独立验收 | 执行结果 | 消耗 | 失败原因或证据 |",
            "|---|---|---|---|---|---|---:|---|",
        ]
    )
    for row in rows:
        passed = row.get("score", {}).get("passed")
        answer = "通过" if passed else ("未通过" if row["status"] == "valid" else "待恢复")
        error = (row.get("error") or row.get("artifact") or "—").replace("|", "／").replace("\n", " ")
        lines.append(
            f"| {row['case_id']} | {NAMES[row['group']]} | {'诊断' if row.get('trial_kind') == 'diagnostic' else '基础'} | {row['status']} | {answer} | {row.get('execution_phase', '—')} | {row.get('tokens', 0):,} | {error} |"
        )
    observed = [r for r in rows if r.get("mechanism_observations")]
    if observed:
        lines.extend(
            [
                "",
                "实际机制记录：",
                "",
                "| 题目与组别 | 压缩 | 回溯请求／有命中 | 阶段检查通过／未通过 | 目标变更 | 实际关闭后恢复 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in observed:
            m = row["mechanism_observations"]
            lines.append(
                f"| {row['case_id']} · {NAMES[row['group']]} | {m['context_compactions']} | "
                f"{m['recall_requests']}／{m['recall_requests_with_hits']} | "
                f"{m['stage_checks_passed']}／{m['stage_checks_failed']} | "
                f"{m['goal_changes']} | {m['process_resume_drills']} |"
            )
        lines.append(
            "回溯命中表示程序已找到并返回归档原文，不能单独证明模型正确使用了它。"
            "Codex 这一表格只覆盖本机接入程序可见的事件，不能据此认定其内部没有相应机制。"
        )
    lines.extend(
        [
            "",
            *report["limitations"],
            "",
            "完整逐项约束、原始事件、冻结版本和评分证据保存在对应 JSON 报告与运行资料中。",
        ]
    )
    directory = directory or PROJECT / "outputs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{phase}-report.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
    save_report(directory / f"{phase}-report.json", report)
    return report
