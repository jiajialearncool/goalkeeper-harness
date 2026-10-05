import json

import pytest

from goalkeeper.benchmark import Feedback, load_cases
from goalkeeper.evaluation import Scheduler
from goalkeeper.files import digest
from goalkeeper.models import CheckSpec, RunState
from goalkeeper.pilot import prepare


async def test_partial_feedback_and_invented_process_exit_do_not_pass(tmp_path, monkeypatch):
    from goalkeeper import evaluation

    monkeypatch.setattr(evaluation, "DATA", tmp_path / "runtime")
    monkeypatch.setattr(evaluation, "PROJECT", tmp_path)
    package = tmp_path / "package"
    prepare(package)
    case = load_cases(package)[0]
    probe = CheckSpec(name="probe", argv=["/usr/bin/true"])
    case.feedback_sequence = [
        Feedback(kind="goal_change", probe=probe, goal="new"),
        Feedback(kind="same_session", probe=probe),
    ]
    scheduler = Scheduler(package, "development", 1, round_id="new-round")
    assert scheduler.ledger_path == tmp_path / "runtime/private/new-round/development-ledger.json"
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "x.txt").write_text("x")
    state = scheduler.store.create(RunState(workspace=str(root), goal=case.goal, phase="completed"))

    class Grader:
        async def grade(self, *args):
            return {"passed": True}

    scheduler.grader = Grader()
    row = {
        "case_id": case.id,
        "group": "goalkeeper",
        "run_id": state.id,
        "artifact": str(root),
        "snapshot": digest(root),
        "status": "awaiting_grade",
    }
    scheduler.rows = [row]
    scheduler.store.event(state.id, "business_feedback_triggered", {"index": 0, "kind": "goal_change"})
    assert not (await scheduler.score(case, row, state))["score"]["passed"]
    scheduler.store.event(state.id, "business_feedback_triggered", {"index": 1, "kind": "same_session"})
    scheduler.store.event(state.id, "program_closed_for_resume", {"actual_process_exit": False})
    assert not (await scheduler.score(case, row, state))["score"]["passed"]
    scheduler.store.event(state.id, "program_closed_for_resume", {"actual_process_exit": True})
    assert (await scheduler.score(case, row, state))["score"]["passed"]
    assert json.loads(scheduler.ledger_path.read_text())[0]["run_id"] == state.id
    assert not (tmp_path / "runtime/private/development-ledger.json").exists()
    scheduler.store.close()


def test_single_feedback_cannot_be_duplicated_by_sequence(tmp_path):
    package = tmp_path / "package"
    prepare(package)
    case = load_cases(package)[0]
    probe = CheckSpec(name="probe", argv=["/usr/bin/true"])
    case.feedback = Feedback(kind="goal_change", probe=probe, goal="new")
    case.feedback_sequence = [case.feedback]
    with pytest.raises(ValueError):
        case.feedback_steps()


def test_new_round_refuses_source_binary_and_input_drift(tmp_path, monkeypatch):
    from goalkeeper import rounds
    from goalkeeper.benchmark import fingerprint
    from goalkeeper.grader import save_report
    from goalkeeper.evaluation import NotReady

    monkeypatch.setattr(rounds, "DATA", tmp_path / "runtime")
    monkeypatch.setattr(rounds, "source_manifest", lambda: {"engine.py": "frozen"})
    monkeypatch.setattr(rounds, "binary_manifest", lambda: {"codex": "frozen"})
    package = tmp_path / "package"
    package.mkdir()
    (package / "TASK.md").write_text("sealed")
    directory = tmp_path / "runtime/private/test-round"
    save_report(
        directory / "freeze.json",
        {
            "round": "test-round",
            "files": {"engine.py": "frozen"},
            "codex_components": {"codex": "frozen"},
            "commit": "commit",
        },
    )
    save_report(
        directory / "input-seal.json", {"fingerprint": fingerprint(package), "frozen_commit": "commit"}
    )
    assert rounds.verify_round("test-round", package)["commit"] == "commit"
    (package / "TASK.md").write_text("changed")
    with pytest.raises(NotReady):
        rounds.verify_round("test-round", package)
    monkeypatch.setattr(rounds, "binary_manifest", lambda: {"codex": "changed"})
    with pytest.raises(NotReady):
        rounds.verify_round("test-round", package, sealed=False)
    monkeypatch.setattr(rounds, "binary_manifest", lambda: {"codex": "frozen"})
    monkeypatch.setattr(rounds, "source_manifest", lambda: {"engine.py": "changed"})
    with pytest.raises(NotReady):
        rounds.verify_round("test-round", package, sealed=False)
