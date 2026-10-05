from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path


class PermissionDenied(RuntimeError):
    pass


def normalized(path: Path) -> Path:
    return path.expanduser().resolve()


@dataclass
class Scope:
    workspace: Path
    writable: bool = True
    write_paths: list[str] | None = None
    runtimes: list[Path] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    extra_read: list[Path] = field(default_factory=list)
    protected_files: list[Path] = field(default_factory=list)

    def path(self, relative: str, write: bool = False) -> Path:
        root = normalized(self.workspace)
        target = normalized(root / relative)
        if not target.is_relative_to(root) or any(
            p in {".git", ".home", ".tmp"} for p in Path(relative).parts
        ):
            raise PermissionDenied("路径不在允许的任务文件范围。")
        if write:
            if not self.writable:
                raise PermissionDenied("该角色只有读取权限。")
            if self.write_paths is not None and not any(
                target == normalized(root / p)
                or (p.endswith("/") and target.is_relative_to(normalized(root / p)))
                for p in self.write_paths
            ):
                raise PermissionDenied("Worker 不能修改分配范围以外的文件。")
        return target


class Seatbelt:
    def __init__(self, scope: Scope):
        self.scope = scope
        self.root = normalized(scope.workspace)
        self.temp = self.root / ".tmp"
        self.home = self.root / ".home"
        self.temp.mkdir(exist_ok=True)
        self.home.mkdir(exist_ok=True)
        self.on_spawn = None
        self.on_exit = None
        self.processes: set[asyncio.subprocess.Process] = set()

    def profile(self) -> str:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise PermissionDenied("第一版要求 macOS Seatbelt；禁止无沙箱回退。")

        def quoted(p):
            return json.dumps(str(normalized(Path(p))), ensure_ascii=False)

        reads = [
            self.root,
            Path("/System"),
            Path("/private/var/db/dyld"),
            Path("/usr/lib"),
            Path("/usr/share"),
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
            Path("/Library/Frameworks/Python.framework"),
            *self.scope.runtimes,
            *self.scope.extra_read,
        ]
        read_rules = " ".join(f"(subpath {quoted(p)})" for p in reads)
        writes = [
            f"(subpath {quoted(self.temp)})",
            f"(subpath {quoted(self.home)})",
            '(literal "/dev/null")',
            '(literal "/dev/tty")',
        ]
        if self.scope.writable:
            if self.scope.write_paths is None:
                writes.append(f"(subpath {quoted(self.root)})")
            else:
                for relative in self.scope.write_paths:
                    path = self.scope.path(relative, write=True)
                    writes.append(f"({'subpath' if relative.endswith('/') else 'literal'} {quoted(path)})")
        network = "\n".join(
            f'(allow network-outbound (remote tcp "localhost:{p}"))\n'
            f'(allow network-bind (local tcp "localhost:{p}"))\n'
            f'(allow network-inbound (local tcp "localhost:{p}"))'
            for p in self.scope.ports
        )
        protected = "\n".join(f"(deny file-write* (literal {quoted(p)}))" for p in self.scope.protected_files)
        return f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow process-info* (target self))
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read* {read_rules} (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))
(allow file-write* {" ".join(writes)})
(deny file-write* (subpath {quoted(self.root / ".git")}))
{protected}
(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.cfprefsd.agent") (global-name "com.apple.logd"))
{network}
"""

    def environment(self) -> dict[str, str]:
        runtime_bins = [str(path / "bin") for path in self.scope.runtimes if (path / "bin").is_dir()]
        # macOS /usr/bin/git is an Xcode launcher. Use the real, explicitly readable
        # executable so the task does not need access to the complete Xcode bundle.
        runtime_bins[:0] = [
            str(path.parent) for path in self.scope.runtimes if path.name == "git" and path.is_file()
        ]
        result = {
            "PATH": ":".join([*runtime_bins, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            "HOME": str(self.home),
            "TMPDIR": str(self.temp) + "/",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "MPLCONFIGDIR": str(self.temp / "matplotlib"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if self.scope.ports:
            result["GK_TASK_PORT"] = str(self.scope.ports[0])
        return result

    async def run(self, argv: list[str], timeout: int = 120) -> dict:
        if not argv or not all(isinstance(part, str) and "\x00" not in part for part in argv):
            raise ValueError("命令必须是非空 argv 数组。")
        profile = self.profile()
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            *argv,
            cwd=self.root,
            env=self.environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.processes.add(process)
        if self.on_spawn:
            self.on_spawn(process)
        timed_out = False
        try:

            async def drain(stream):
                chunks, size, truncated = [], 0, False
                while chunk := await stream.read(8192):
                    if size < 100_000:
                        chunks.append(chunk[: 100_000 - size])
                    size += len(chunk)
                    truncated = truncated or size > 100_000
                return b"".join(chunks) + (b"\n[output truncated]" if truncated else b"")

            readers = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
            await asyncio.wait_for(process.wait(), timeout)
            self.kill(process)
            stdout, stderr = await asyncio.gather(*readers)
        except (TimeoutError, asyncio.CancelledError) as error:
            self.kill(process)
            await process.wait()
            stdout, stderr = await asyncio.gather(*readers)
            if isinstance(error, asyncio.CancelledError):
                raise
            timed_out = True
        finally:
            self.kill(process)  # Also remove surviving background descendants in the process group.
            self.processes.discard(process)
            if self.on_exit:
                self.on_exit(process)
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "timed_out": timed_out,
        }

    @staticmethod
    def kill(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def stop(self) -> None:
        for process in list(self.processes):
            self.kill(process)
        await asyncio.gather(*(p.wait() for p in self.processes), return_exceptions=True)
        self.processes.clear()


def runtime_roots() -> list[Path]:
    roots = [Path(sys.prefix), Path(sys.base_prefix)]
    # Restrict executable search to actual installations, never the whole user's home.
    import shutil

    for name in ("node", "npm", "git"):
        if executable := shutil.which(name):
            path = Path(executable).resolve()
            if str(path).startswith(("/Applications/", "/opt/", "/usr/local/")):
                roots.append(path.parent.parent)
    if sys.platform == "darwin":
        import subprocess

        selection = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=5)
        if selection.returncode == 0:
            developer = Path(selection.stdout.strip())
            for relative in ("usr/bin/git", "usr/libexec/git-core", "usr/share/git-core"):
                path = developer / relative
                if path.exists():
                    roots.append(path)
    library = Path(__file__).resolve().parents[2] / "frontend/node_modules"
    if library.exists():
        roots.append(library)
    if executable := shutil.which("python3.12"):
        roots.append(Path(executable).resolve().parent.parent)
    office_runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    if office_runtime.exists():
        roots.append(office_runtime)
    return list(dict.fromkeys(roots))
