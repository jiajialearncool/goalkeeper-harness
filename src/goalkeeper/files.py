from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

EXCLUDED = {".git", ".home", ".tmp", "node_modules", ".venv", "__pycache__", ".pytest_cache"}


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [
            f for f in sorted(folders) if f not in EXCLUDED and not (Path(directory) / f).is_symlink()
        ]
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                result[str(path.relative_to(root))] = "symlink:" + os.readlink(path)
            elif path.is_file():
                result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def digest(root: Path) -> str:
    import json

    return hashlib.sha256(json.dumps(inventory(root), sort_keys=True).encode()).hexdigest()


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-c", "user.name=GoalKeeper", "-c", "user.email=local@goalkeeper.invalid", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if result.returncode:
        raise RuntimeError(result.stderr[:2000])
    return result.stdout


def initialize_repository(root: Path) -> None:
    if not (root / ".git").exists():
        git(root, "init", "-b", "main")
    git(root, "add", "--all", "--", ":!.home", ":!.tmp")
    git(root, "commit", "--allow-empty", "-m", "Initial task state")


def snapshot(source: Path, destination: Path) -> str:
    if destination.exists():
        raise FileExistsError("快照已存在，禁止覆盖评测证据。")
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(*EXCLUDED))
    value = digest(destination)
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o400)
    return value


def import_repository(source: Path, destination: Path) -> None:
    from .secrets import contains_secret

    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError("请选择实际存在的本地 Git 项目目录。")
    tracked = git(source, "ls-files", "-z").split("\0")
    if not any(tracked):
        raise ValueError("所选项目没有 Git 已跟踪文件，请先保存需要导入的源码。")
    pending = []
    for name in filter(None, tracked):
        file = source / name
        parts = Path(name).parts
        if any(p in EXCLUDED or p == ".codex" or p.startswith(".env") for p in parts):
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(source):
            raise ValueError("项目含符号链接或越界文件，请先移除后导入。")
        if not file.is_file():
            continue
        if file.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            raise ValueError("项目含凭据文件，已拒绝导入。")
        content = file.read_bytes()
        if len(content) > 20_000_000:
            raise ValueError("单个导入文件超过 20 MB。")
        try:
            secret = contains_secret(content.decode())
        except UnicodeDecodeError:
            secret = False
        if secret:
            raise ValueError("项目源码含疑似凭据，请移除后再导入。")
        pending.append((name, content))
    for name, content in pending:
        file = destination / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    initialize_repository(destination)
