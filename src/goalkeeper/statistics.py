from __future__ import annotations

import numpy as np
from scipy.stats import beta


def clopper_pearson(successes: int, n: int, alpha: float = 0.05):
    if n == 0:
        return [None, None]
    return [
        0.0 if successes == 0 else float(beta.ppf(alpha / 2, successes, n - successes + 1)),
        1.0 if successes == n else float(beta.ppf(1 - alpha / 2, successes + 1, n - successes)),
    ]


def paired_success_interval(a: list[int], b: list[int], alpha=0.05):
    """Conservative simultaneous exact intervals for the two discordant-pair probabilities.

    Bonferroni guarantees >=95% coverage for p(a wins)-p(b wins), including zero discordances.
    A bootstrap-only all-tied interval [0,0] would incorrectly claim non-inferiority on 12 tasks.
    """
    if len(a) != len(b) or not a:
        raise ValueError("需要完整配对数据。")
    wins = sum(x == 1 and y == 0 for x, y in zip(a, b))
    losses = sum(x == 0 and y == 1 for x, y in zip(a, b))
    wa = clopper_pearson(wins, len(a), alpha / 2)
    la = clopper_pearson(losses, len(a), alpha / 2)
    return {
        "difference": (wins - losses) / len(a),
        "interval": [wa[0] - la[1], wa[1] - la[0]],
        "wins": wins,
        "losses": losses,
        "method": "paired_discordance_exact_bonferroni",
        "leading": wa[0] - la[1] > 0,
        "non_inferior": wa[0] - la[1] >= -0.05,
    }


def paired_cost_interval(a: list[float], b: list[float], samples=10_000, seed=20261001):
    if len(a) != len(b) or not a:
        raise ValueError("需要完整配对数据。")
    diffs = np.asarray(a) - np.asarray(b)
    indices = np.random.default_rng(seed).integers(0, len(a), size=(samples, len(a)))
    values = diffs[indices].mean(axis=1)
    return {
        "difference": float(diffs.mean()),
        "interval": np.quantile(values, [0.025, 0.975]).tolist(),
        "method": "task_paired_bootstrap",
        "samples": samples,
        "seed": seed,
    }


def summarize(records: list[dict]) -> dict:
    groups = ["codex", "goalkeeper", "controlled"]
    output = {
        "groups": {},
        "comparisons": {},
        "valid_attempts": sum(r["status"] == "valid" for r in records),
        "limitations": [
            "每题每组仅一次，无法估计同题重复运行的波动。",
            "仅报告可观测用量，不能据此声称总体 token 消耗更低。",
            "第一版正式任务均为离线任务，不含本地服务端口或公网工具。",
        ],
    }
    for group in groups:
        rows = [r for r in records if r["group"] == group and r["status"] == "valid"]
        successes = sum(r["score"]["passed"] and not r.get("human_rescue", False) for r in rows)
        all_rows = [r for r in records if r["group"] == group]
        tokens = sum(r.get("tokens", 0) for r in all_rows)
        constraints = {}
        for row in rows:
            score = row["score"]
            for name in score.get("tests_expected", score.get("tests_seen", [])):
                case_key = row["case_id"]
                if row.get("trial_kind") == "diagnostic":
                    case_key += "/diagnostic/" + row.get("run_id", "unknown")
                constraints[case_key + "/" + name] = name not in score.get("failed_tests", []) and score.get(
                    "complete_test_collection", False
                )
        output["groups"][group] = {
            "attempts": len(rows),
            "successes": successes,
            "success_rate": successes / len(rows) if rows else None,
            "tokens": tokens,
            "all_tokens_per_success": tokens / successes if successes else None,
            "execution_seconds": sum(r.get("active_seconds", 0) for r in all_rows),
            "quota_wait_seconds": sum(r.get("quota_wait_seconds", 0) for r in all_rows),
            "grading_seconds": sum(
                r.get("grading_seconds", 0)
                + sum(a.get("grading_seconds", 0) for a in r.get("interim_artifacts", []))
                for r in all_rows
            ),
            "base_attempts": sum(r.get("trial_kind", "base") == "base" for r in rows),
            "diagnostic_attempts": sum(r.get("trial_kind") == "diagnostic" for r in rows),
            "hard_constraints": constraints,
            "hard_constraint_pass_rate": sum(constraints.values()) / len(constraints)
            if constraints
            else None,
            "usage_complete": all(r.get("usage_complete", False) for r in all_rows) if all_rows else None,
        }
    for comparator in ["codex", "controlled"]:
        a = {
            r["case_id"]: r
            for r in records
            if r["group"] == "goalkeeper" and r["status"] == "valid" and r.get("trial_kind", "base") == "base"
        }
        b = {
            r["case_id"]: r
            for r in records
            if r["group"] == comparator and r["status"] == "valid" and r.get("trial_kind", "base") == "base"
        }
        keys = sorted(a.keys() & b.keys())
        if keys:
            output["comparisons"][comparator] = {
                "paired_tasks": len(keys),
                "success": paired_success_interval(
                    [int(a[k]["score"]["passed"] and not a[k].get("human_rescue")) for k in keys],
                    [int(b[k]["score"]["passed"] and not b[k].get("human_rescue")) for k in keys],
                ),
                "observed_tokens": paired_cost_interval(
                    [a[k]["tokens"] for k in keys], [b[k]["tokens"] for k in keys]
                ),
                "execution_seconds": paired_cost_interval(
                    [a[k].get("active_seconds", 0) for k in keys],
                    [b[k].get("active_seconds", 0) for k in keys],
                ),
            }
    case_sets = [
        {r["case_id"] for r in records if r["group"] == g and r["status"] == "valid"} for g in groups
    ]
    output["complete"] = all(output["groups"][g]["attempts"] == 12 for g in groups) and all(
        len(keys) == 12 and keys == case_sets[0] for keys in case_sets
    )
    output["can_claim_result"] = output["complete"] and all(
        r.get("evidence_mode") == "real" and r.get("source") == "formal" for r in records
    )
    return output
