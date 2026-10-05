"""Evidence freshness and requirement coverage, without model inference."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import sys
from pathlib import Path

from .files import inventory
from .models import CheckSpec, RunState


def expand_imports(root: Path, files: dict, selected: dict) -> tuple[dict, bool]:
    selected = dict(selected)
    queue = list(selected)
    visited = set()
    while queue:
        name = queue.pop()
        if name in visited or not name.endswith(".py"):
            continue
        visited.add(name)
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()):
            return files, False
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError, UnicodeError):
            return files, False
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name.split(".") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = list(Path(name).parent.parts)
                if node.level:
                    base = base[: len(base) - node.level + 1]
                else:
                    base = []
                parts = base + (node.module.split(".") if node.module else [])
                modules.append(parts)
                modules.extend(parts + alias.name.split(".") for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Call):
                function = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if function in {
                    "exec",
                    "eval",
                    "__import__",
                    "import_module",
                    "spec_from_file_location",
                    "open",
                    "read_text",
                    "read_bytes",
                    "glob",
                    "rglob",
                    "listdir",
                    "scandir",
                }:
                    return files, False
        for parts in modules:
            candidates = ["/".join(parts) + ".py"]
            candidates.extend("/".join(parts[:i]) + "/__init__.py" for i in range(1, len(parts) + 1))
            for candidate in candidates:
                if candidate in files and candidate not in selected:
                    selected[candidate] = files[candidate]
                    queue.append(candidate)
    return selected, True


def dependency_key(root: Path, spec: CheckSpec, state: RunState | None, ports=None) -> str:
    files = inventory(root)
    policy = (
        next((p for p in state.check_policies if p.argv == spec.argv and p.closed), None) if state else None
    )
    scope = (
        [*policy.dependencies, "*.py", "pyproject.toml", "pytest.ini", "setup.cfg", ".env*"]
        if policy
        else ["**"]
    )
    # Missing files, additions, deletions and test edits change the key. Unknown scopes
    # use all files. Agent-proposed check methods cannot supply dependency policies.
    selected = {
        name: value
        for name, value in files.items()
        if any(
            pattern == "**"
            or fnmatch.fnmatchcase(name, pattern)
            and ("/" in pattern or "/" not in name)
            or pattern.endswith("/")
            and name.startswith(pattern)
            for pattern in scope
        )
    }
    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in scope):
        scope, selected = ["**"], files
    if policy:
        selected, closed_imports = expand_imports(root, files, selected)
        if not closed_imports:
            scope, selected = ["**"], files
    executable = Path(spec.argv[0])
    stat = executable.stat() if executable.is_absolute() and executable.is_file() else None
    from .sandbox import Scope, Seatbelt, runtime_roots

    runner = Seatbelt(Scope(root, writable=False, runtimes=runtime_roots(), ports=ports or []))
    # Local runtimes are fixed during an attempt. Include installation identities,
    # interpreter identity and the exact sandbox/environment; no user environment leaks.
    runtimes = [(str(p), p.stat().st_mtime_ns) for p in runner.scope.runtimes if p.exists()]
    payload = {
        "files": selected,
        "scope": scope,
        "argv": spec.argv,
        "timeout": spec.timeout,
        "version": state.goal.version if state else None,
        "ports": ports or [],
        "environment": runner.environment(),
        "profile": runner.profile(),
        "runtime": runtimes,
        "python": sys.version,
        "executable": (stat.st_mtime_ns, stat.st_size) if stat else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def evidence_current(state: RunState, evidence: dict, specs: list[CheckSpec]) -> bool:
    if not evidence.get("passed") or evidence.get("goal_version") != state.goal.version:
        return False
    results = evidence.get("checks", [])
    return bool(specs) and all(
        any(
            r.get("argv") == spec.argv
            and r.get("passed")
            and r.get("dependency_key") == dependency_key(state.root, spec, state, state.ports)
            for r in results
        )
        for spec in specs
    )


def stage_specs(state: RunState, milestone) -> list[CheckSpec]:
    specs = list(milestone.checks)
    for requirement in state.goal.requirements:
        if requirement.id in milestone.requirement_ids:
            specs.extend(requirement.checks)
    unique = {}
    for spec in specs:
        unique[(tuple(spec.argv), spec.timeout)] = spec
    return list(unique.values())


def requirements_status(state: RunState) -> list[dict]:
    results = []
    for requirement in state.goal.requirements:
        stages = [m for m in state.milestones if requirement.id in m.requirement_ids]
        paths = sorted({p for m in stages for p in m.implementation_paths})
        valid_paths = [
            p
            for p in paths
            if not Path(p).is_absolute()
            and ".." not in Path(p).parts
            and (state.root / p).exists()
            and (state.root / p).resolve().is_relative_to(state.root.resolve())
        ]
        verified = any(
            m.status == "verified"
            and m.evidence
            and evidence_current(state, m.evidence[-1], requirement.checks)
            for m in stages
        )
        results.append(
            {
                "id": requirement.id,
                "text": requirement.text,
                "implementation_paths": valid_paths,
                "verified": bool(valid_paths) and verified,
                "stage_ids": [m.id for m in stages],
                "goal_version": state.goal.version,
            }
        )
    return results
