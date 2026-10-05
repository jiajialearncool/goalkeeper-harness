from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .files import inventory
from .models import CheckPolicy, CheckSpec, Goal, Requirement, StrictModel


class Feedback(StrictModel):
    kind: Literal["goal_change", "same_session", "new_session"]
    probe: CheckSpec
    goal: str = ""
    constraints: list[str] | None = None
    acceptance: list[str] | None = None
    handoff: str = ""
    requirements: list[Requirement] | None = None


class Case(StrictModel):
    id: str
    category: Literal["refactor", "migration", "debug", "feedback", "resume"]
    title: str
    initial: str
    goal: Goal
    public_checks: list[CheckSpec]
    check_policies: list[CheckPolicy] = Field(default_factory=list)
    grader_file: str
    expected_tests: list[str]
    editable: list[str]
    feedback: Feedback | None = None
    feedback_sequence: list[Feedback] = Field(default_factory=list, max_length=3)
    ports: list[int] = Field(default_factory=list)
    reference: str
    mutants: list[str]
    mutant_expectations: dict[str, list[str]] = Field(default_factory=dict)
    environment_ready: bool = False
    source: Literal["pilot", "formal"] = "pilot"

    def feedback_steps(self) -> list[Feedback]:
        if self.feedback and self.feedback_sequence:
            raise ValueError("单条反馈和反馈序列不能同时配置。")
        return self.feedback_sequence or ([self.feedback] if self.feedback else [])

    def execution_goal(self) -> Goal:
        goal = self.goal.model_copy(deep=True)
        goal.constraints.append(
            "可修改文件范围："
            + "、".join(self.editable)
            + "。以 / 结尾表示该目录内文件；其他文件必须保持不变。"
        )
        return goal


def load_cases(package: Path) -> list[Case]:
    manifest = json.loads((package / "manifest.json").read_text())
    cases = [Case.model_validate(c) for c in manifest["cases"]]
    if len(cases) != 12 or len({c.id for c in cases}) != 12:
        raise ValueError("输入包必须包含 12 道不同题目。")
    counts = {
        k: sum(c.category == k for c in cases)
        for k in ["refactor", "migration", "debug", "feedback", "resume"]
    }
    if counts != {"refactor": 3, "migration": 3, "debug": 2, "feedback": 2, "resume": 2}:
        raise ValueError("题目分布与已确认协议不一致。")
    for case in cases:
        for value in [case.initial, case.grader_file, case.reference, *case.mutants]:
            resolved = (package / value).resolve()
            if not resolved.is_relative_to(package.resolve()) or not resolved.exists():
                raise ValueError(f"{case.id} 的输入引用无效。")
    return cases


def fingerprint(directory: Path) -> str:
    return hashlib.sha256(json.dumps(inventory(directory), sort_keys=True).encode()).hexdigest()


def scheduling_order(cases: list[Case]) -> list[tuple[str, str]]:
    groups = ["codex", "goalkeeper", "controlled"]
    return [(case.id, groups[(offset + i) % 3]) for offset, case in enumerate(cases) for i in range(3)]
