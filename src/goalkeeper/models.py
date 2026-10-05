from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL = "gpt-6.1-sol"
EFFORT = "xhigh"


def uid() -> str:
    return uuid4().hex


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Features(StrictModel):
    goal_anchor: bool = True
    layered_context: bool = True
    progress_skeleton: bool = True
    stage_gates: bool = True

    @classmethod
    def baseline(cls) -> Features:
        return cls(**{k: False for k in cls.model_fields})


class RunConfig(StrictModel):
    # Keep the previous identifier readable in archived development runs.
    # New runs and all execution paths must pass require_current_model().
    model: Literal["gpt-6-astra", "gpt-6.1-sol"] = MODEL
    effort: Literal["xhigh"] = EFFORT
    engine: Literal["goalkeeper", "controlled", "codex", "opencode", "pi"] = "goalkeeper"
    features: Features = Field(default_factory=Features)
    context_limit: int = Field(default=64_000, ge=2048, le=922_000)
    compact_at: float = Field(default=0.8, gt=0, lt=1)
    compact_target: float = Field(default=0.55, gt=0, lt=1)
    summary_fraction: float = Field(default=0.15, gt=0, lt=1)
    summary_target_tokens: int = Field(default=2500, ge=128)
    recent_fraction: float = Field(default=0.2, gt=0, lt=1)
    recall_top_k: int = Field(default=5, ge=1, le=10)
    recall_tokens: int = Field(default=4096, ge=128)
    token_budget: int = Field(default=1_000_000, ge=1000)
    seconds_budget: int = Field(default=1800, ge=1)
    max_children: int = Field(default=2, ge=0, le=2)
    repair_rounds: int = Field(default=2, ge=0, le=2)
    memory_enabled: bool = True
    tool_timeout: int = Field(default=120, ge=1, le=300)
    finish_reserve_seconds: int = Field(default=120, ge=0, le=300)
    finish_reserve_tokens: int = Field(default=50_000, ge=0)
    # Opt in per attempt while the new scheduling/material policy is validated.
    efficiency_enabled: bool = False
    focused_context_enabled: bool = False
    final_review_required: bool = False
    review_reserve_seconds: int = Field(default=90, ge=0, le=300)
    global_efficiency_enabled: bool = False
    review_window_seconds: int = Field(default=180, ge=10, le=300)

    def require_current_model(self) -> None:
        if self.model != MODEL or self.effort != EFFORT:
            raise ValueError(f"当前评测统一使用 {MODEL} / {EFFORT}；旧模型记录只供查看。")

    @model_validator(mode="after")
    def context_fractions(self):
        if not self.summary_fraction + self.recent_fraction < self.compact_target < self.compact_at:
            raise ValueError("压缩后必须留出工具和目标空间，且小于压缩触发阈值。")
        return self


class CheckSpec(StrictModel):
    name: str
    argv: list[str] = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=300)


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    checks: list[CheckSpec] = Field(min_length=1)
    source_refs: list[str] = Field(default_factory=list)


class CheckPolicy(StrictModel):
    """Trusted input supplied by the task owner, never by an execution tool."""

    argv: list[str] = Field(min_length=1)
    dependencies: list[str] = Field(min_length=1)
    closed: bool = False


class Goal(StrictModel):
    version: int = 1
    original: str = Field(min_length=1)
    current: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    changed_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def unique_requirements(self):
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("要求 ID 不能重复。")
        return self


class Milestone(StrictModel):
    id: str = Field(default_factory=uid)
    description: str
    checks: list[CheckSpec] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    implementation_paths: list[str] = Field(default_factory=list)
    test_coverage: list[str] = Field(default_factory=list)
    status: Literal["pending", "active", "verified"] = "pending"
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    goal_version: int = 1


class OperationStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"


class Usage(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    known: bool = True

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class CompactionProgress(StrictModel):
    """Incomplete compression is durable, but never used as the live summary."""

    identity: str
    archive: str
    source_digest: str
    goal_version: int
    offset: int = Field(default=0, ge=0)
    chunks: int = Field(default=0, ge=0)
    summary: str = ""


class RunState(StrictModel):
    id: str = Field(default_factory=uid)
    version: int = 0
    goal: Goal
    config: RunConfig = Field(default_factory=RunConfig)
    workspace: str
    phase: Literal[
        "ready",
        "running",
        "paused",
        "resource_interrupted",
        "budget_exhausted",
        "completed",
        "failed",
        "cancelled",
        "recovering",
    ] = "ready"
    milestones: list[Milestone] = Field(default_factory=list)
    plan_goal_version: int = 0
    final_checks: list[CheckSpec] = Field(default_factory=list)
    check_policies: list[CheckPolicy] = Field(default_factory=list)
    check_cache: dict[str, dict[str, Any]] = Field(default_factory=dict)
    recall_triggers: list[str] = Field(default_factory=list)
    recall_refs: list[str] = Field(default_factory=list)
    active_stage: str | None = None
    evaluation_round: str = ""
    ports: list[int] = Field(default_factory=list)
    children_used: int = 0
    tool_calls: int = 0
    evidence_mode: Literal["real", "offline"] = "real"
    evaluation_phase: Literal["development", "pilot", "formal", "demo"] = "development"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    compaction_progress: CompactionProgress | None = None
    tokens_used: int = 0
    usage_complete: bool = True
    active_seconds: float = 0
    quota_wait_seconds: float = 0
    repairs: dict[str, int] = Field(default_factory=dict)
    memory_enabled: bool = True
    answer: str = ""
    native_thread: str | None = None
    native_children: list[str] = Field(default_factory=list)
    native_usage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    native_handoff: str = ""
    next_action: str = ""
    review_evidence: dict[str, Any] = Field(default_factory=dict)
    review_progress: dict[str, Any] = Field(default_factory=dict)
    closing_mode: bool = False
    checkpoint_files: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)
    error: str | None = None
    failure_origin: str | None = None

    @property
    def root(self) -> Path:
        return Path(self.workspace)


class ModelRequest(StrictModel):
    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    config: RunConfig


class ModelEvent(StrictModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolResult(StrictModel):
    ok: bool
    output: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    exit_code: int | None = None


class Deliverable(StrictModel):
    role: Literal["Explorer", "Planner", "Worker", "Reviewer"]
    summary: str
    patch: str = ""
    files: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class Verdict(StrictModel):
    passed: bool
    goal_version: int
    snapshot: str
    checks: list[dict[str, Any]]
