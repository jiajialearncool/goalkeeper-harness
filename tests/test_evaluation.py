import json

import pytest

from goalkeeper.benchmark import load_cases, scheduling_order
from goalkeeper.evaluation import NotReady, freeze
from goalkeeper.grader import IndependentGrader
from goalkeeper.pilot import prepare
from goalkeeper.statistics import paired_success_interval, summarize


def test_rotating_schedule_has_all_36_unique_pairs(tmp_path):
    prepare(tmp_path)
    cases = load_cases(tmp_path)
    order = scheduling_order(cases)
    assert len(order) == len(set(order)) == 36
    assert order[:6] == [
        ("A1", "codex"),
        ("A1", "goalkeeper"),
        ("A1", "controlled"),
        ("A2", "goalkeeper"),
        ("A2", "controlled"),
        ("A2", "codex"),
    ]


def test_twelve_ties_cannot_prove_five_point_noninferiority():
    result = paired_success_interval([1] * 12, [1] * 12)
    assert result["interval"][0] < -0.05 and result["non_inferior"] is False
    assert result["leading"] is False


def test_resource_interruption_cost_is_not_discarded():
    rows = [
        {
            "case_id": "A",
            "group": "goalkeeper",
            "status": "resource_interrupted",
            "tokens": 200,
            "active_seconds": 2,
            "usage_complete": False,
        },
        {
            "case_id": "B",
            "group": "goalkeeper",
            "status": "valid",
            "tokens": 300,
            "active_seconds": 3,
            "score": {"passed": True},
            "usage_complete": True,
        },
    ]
    report = summarize(rows)
    assert report["groups"]["goalkeeper"]["all_tokens_per_success"] == 500
    assert report["valid_attempts"] == 1 and not report["can_claim_result"]


def test_human_rescue_is_not_success():
    report = summarize(
        [
            {
                "case_id": "A",
                "group": "goalkeeper",
                "status": "valid",
                "tokens": 100,
                "active_seconds": 1,
                "score": {"passed": True},
                "human_rescue": True,
            }
        ]
    )
    assert report["groups"]["goalkeeper"]["successes"] == 0
    assert report["groups"]["goalkeeper"]["all_tokens_per_success"] is None


def test_diagnostic_success_never_replaces_failed_base_in_comparison():
    rows = [
        {
            "case_id": "A",
            "group": group,
            "status": "valid",
            "tokens": 100,
            "score": {"passed": passed},
            "trial_kind": kind,
        }
        for group, passed, kind in [
            ("goalkeeper", False, "base"),
            ("controlled", True, "base"),
            ("goalkeeper", True, "diagnostic"),
        ]
    ]
    report = summarize(rows)
    assert report["comparisons"]["controlled"]["success"]["difference"] == -1
    assert report["groups"]["goalkeeper"]["tokens"] == 200


def test_interim_grading_and_diagnostic_constraints_are_retained():
    rows = [
        {
            "case_id": "A",
            "group": "goalkeeper",
            "run_id": identifier,
            "status": "valid",
            "tokens": 100,
            "grading_seconds": 2,
            "interim_artifacts": [{"grading_seconds": 3}],
            "score": {
                "passed": passed,
                "tests_expected": ["required"],
                "failed_tests": [] if passed else ["required"],
                "complete_test_collection": True,
            },
            "trial_kind": kind,
        }
        for identifier, passed, kind in [("base", False, "base"), ("retry", True, "diagnostic")]
    ]
    result = summarize(rows)["groups"]["goalkeeper"]
    assert result["grading_seconds"] == 10
    assert result["hard_constraints"] == {"A/required": False, "A/diagnostic/retry/required": True}
    assert result["base_attempts"] == result["diagnostic_attempts"] == 1


def test_mechanism_report_includes_events_beyond_first_page(tmp_path):
    from goalkeeper.models import Goal, RunState
    from goalkeeper.reporting import mechanism_observations
    from goalkeeper.store import Store

    store = Store(tmp_path / "state")
    state = store.create(RunState(goal=Goal(original="test", current="test"), workspace=str(tmp_path)))
    for _ in range(501):
        store.event(state.id, "text_delta", {})
    store.event(state.id, "context_compacted", {})
    store.event(state.id, "recall", {"hits": []})
    store.event(state.id, "recall", {"hits": ["source"]})
    store.event(state.id, "stage_checked", {"passed": False})
    store.event(state.id, "stage_checked", {"passed": True})
    store.event(state.id, "program_closed_for_resume", {"actual_process_exit": True})
    result = mechanism_observations(store, state.id)
    assert result["context_compactions"] == 1
    assert result["recall_requests"] == 2 and result["recall_requests_with_hits"] == 1
    assert result["stage_checks_passed"] == result["stage_checks_failed"] == 1
    assert result["process_resume_drills"] == 1
    store.close()


def test_incomplete_conditions_reject_freeze():
    with pytest.raises(NotReady):
        freeze(4)


async def test_grader_refuses_skipped_or_missing_tests(tmp_path):
    prepare(tmp_path)
    case = load_cases(tmp_path)[0]
    grader = IndependentGrader()
    original = tmp_path / case.grader_file
    original.write_text("import pytest\n@pytest.mark.skip\ndef test_behavior():pass\n")
    result = await grader.grade(case, tmp_path, tmp_path / case.reference)
    assert not result["passed"] and not result["complete_test_collection"]


async def test_grader_collects_private_tests_without_exposing_private_package(tmp_path):
    package = tmp_path / "private/package"
    prepare(package)
    case = load_cases(package)[0]
    from goalkeeper.files import snapshot

    artifact = tmp_path / "artifacts/run"
    artifact.parent.mkdir()
    snapshot(package / case.reference, artifact)
    forbidden = package.parent / "other-secret"
    forbidden.write_text("synthetic-private-only")
    path = package / case.grader_file
    path.write_text(
        path.read_text()
        + f"""\ndef test_isolation():
    import pytest
    with pytest.raises(PermissionError):
        Path({str(forbidden)!r}).read_text()
    with pytest.raises(PermissionError):
        Path(__file__).write_text("tampered")
"""
    )
    case.expected_tests.append("test_isolation")
    result = await IndependentGrader().grade(case, package, artifact)
    assert result["passed"] and not result["infrastructure_error"], result


def test_modifiable_scope_is_visible_and_does_not_modify_case_goal(tmp_path):
    prepare(tmp_path)
    case = load_cases(tmp_path)[0]
    goal = case.execution_goal()
    assert goal.constraints[-1].startswith("可修改文件范围：log_config.py")
    assert not case.goal.constraints


async def test_logging_grader_accepts_valid_module_loggers(tmp_path):
    prepare(tmp_path)
    case = load_cases(tmp_path)[0]
    root = tmp_path / case.reference
    for i in range(12):
        file = root / f"unit{i}.py"
        file.write_text(file.read_text().replace('getLogger("sales")', f'getLogger("unit{i}")'))
    (root / "log_config.py").write_text(
        "import logging\ndef configure(level):\n    logging.getLogger().setLevel(level)\n"
    )
    result = await IndependentGrader().grade(case, tmp_path, root)
    assert result["passed"], result


async def test_logging_grader_accepts_debug_logs_with_level_control(tmp_path):
    prepare(tmp_path)
    case = load_cases(tmp_path)[0]
    root = tmp_path / case.reference
    for i in range(12):
        file = root / f"unit{i}.py"
        file.write_text(file.read_text().replace("logger.info(", "logger.debug("))
    result = await IndependentGrader().grade(case, tmp_path, root)
    assert result["passed"], result


async def test_logging_grader_accepts_multiple_leveled_records_per_call(tmp_path):
    prepare(tmp_path)
    case = load_cases(tmp_path)[0]
    root = tmp_path / case.reference
    for i in range(12):
        file = root / f"unit{i}.py"
        file.write_text(
            file.read_text().replace("    return round", '    logger.debug("completed")\n    return round')
        )
    result = await IndependentGrader().grade(case, tmp_path, root)
    assert result["passed"], result


async def test_execution_agent_cannot_modify_independent_tests(tmp_path):
    from goalkeeper.sandbox import Scope, Seatbelt, runtime_roots

    root = tmp_path / "workspace"
    root.mkdir()
    score = tmp_path / "test_hidden.py"
    score.write_text("original")
    result = await Seatbelt(Scope(root, runtimes=runtime_roots())).run(
        ["/bin/sh", "-c", f"echo fake > {score}"]
    )
    assert result["exit_code"] != 0 and score.read_text() == "original"


def test_formal_distribution_cannot_be_changed_silently(tmp_path):
    prepare(tmp_path)
    file = tmp_path / "manifest.json"
    manifest = json.loads(file.read_text())
    manifest["cases"][0]["category"] = "debug"
    file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_cases(tmp_path)


async def test_scoring_failure_resumes_same_snapshot_without_agent(tmp_path, monkeypatch):
    from goalkeeper import evaluation
    from goalkeeper.files import snapshot
    from goalkeeper.models import Goal, RunState

    monkeypatch.setattr(evaluation, "DATA", tmp_path / "runtime")
    prepare(tmp_path / "package")
    case = load_cases(tmp_path / "package")[0]
    scheduler = evaluation.Scheduler(tmp_path / "package", "pilot", 1)
    state = scheduler.store.create(
        RunState(
            workspace=str(tmp_path / "package" / case.reference),
            goal=Goal(original="task", current="task"),
            phase="completed",
        )
    )
    target = tmp_path / "artifact"
    checksum = snapshot(state.root, target)
    row = {
        "case_id": case.id,
        "group": "goalkeeper",
        "run_id": state.id,
        "status": "grading_error",
        "artifact": str(target),
        "snapshot": checksum,
    }
    scheduler.rows.append(row)

    class Scorer:
        async def grade(self, *args):
            return {"passed": True}

    scheduler.grader = Scorer()
    result = await scheduler.attempt(case, "goalkeeper")
    assert result["status"] == "valid" and result["run_id"] == state.id
    assert scheduler.max_active_workers == 0
    scheduler.store.close()
