from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from .codex import CodexClient
from .store import Store

from .auth import AuthBroker
from .codex import CODEX, inspect_protocol
from .grader import save_report
from .paths import DATA, PROJECT
from .sandbox import Scope, Seatbelt, runtime_roots
from .models import EFFORT, MODEL
from .files import initialize_repository


async def doctor() -> dict:
    private = DATA / "private"
    broker = AuthBroker(private)
    probe = DATA / "doctor-workspace"
    probe.mkdir(parents=True, exist_ok=True)
    initialize_repository(probe)
    forbidden = private / "synthetic-grader-secret"
    forbidden.write_text("synthetic-test-only")
    sandbox = Seatbelt(Scope(probe, runtimes=runtime_roots()))
    allowed = await sandbox.run(["/bin/echo", "sandbox-ready"])
    git_check = await sandbox.run(["git", "diff", "--check"])
    denied = await sandbox.run(["/bin/cat", str(forbidden)])
    network = await sandbox.run(
        [sys.executable, "-c", 'import socket;s=socket.socket();s.connect(("127.0.0.1",8765))']
    )
    schema = PROJECT / "work/codex-schema-experimental"
    if not (schema / "v2/TurnStartParams.json").exists():
        subprocess.run(
            [str(CODEX), "app-server", "generate-json-schema", "--experimental", "--out", str(schema)],
            check=True,
            timeout=30,
            capture_output=True,
        )
    try:
        protocol = await inspect_protocol(schema, broker.status().get("email"))
        comparator = CodexClient(probe, runtime_roots())
        try:
            await comparator.start()

            async def command(argv):
                return await comparator.request(
                    "command/exec",
                    {
                        "command": argv,
                        "cwd": str(probe),
                        "permissionProfile": comparator.profile_id,
                        "timeoutMs": 5000,
                    },
                )

            native_allowed = await command(
                [
                    sys.executable,
                    "-c",
                    'from pathlib import Path;Path("native-write-test").write_text("allowed")',
                ]
            )
            git_executable = next(
                (str(p) for p in runtime_roots() if p.name == "git" and p.is_file()), "/usr/bin/git"
            )
            native_git = await command([git_executable, "diff", "--check"])
            denied_files = []
            for name in ["synthetic-grader-secret", "synthetic-other-workspace", "synthetic-auth-store"]:
                file = private / name
                file.write_text("synthetic-test-only")
                denied_files.append(await command(["/bin/cat", str(file)]))
            native_network = []
            for host, port in [("127.0.0.1", 8765), ("1.1.1.1", 443)]:
                native_network.append(
                    await command(
                        [
                            sys.executable,
                            "-c",
                            f"import socket;s=socket.socket();s.settimeout(.5);s.connect(({host!r},{port}))",
                        ]
                    )
                )
            cfg = (await comparator.request("config/read", {"includeLayers": False, "cwd": str(probe)}))[
                "config"
            ]
            protocol["restricted_read_verified"] = native_allowed["exitCode"] == 0 and all(
                r["exitCode"] != 0
                and "synthetic-test-only" not in r["stdout"]
                and "Operation not permitted" in r["stderr"]
                for r in denied_files
            )
            protocol["offline_network_verified"] = all(
                "Operation not permitted" in r["stderr"] for r in native_network
            )
            protocol["connectors_disabled"] = (
                all(v.get("enabled") is False for v in cfg.get("mcp_servers", {}).values())
                and cfg.get("web_search") == "disabled"
            )
            protocol["memory_disabled"] = (
                cfg.get("memories", {}).get("use_memories") is False
                and cfg.get("memories", {}).get("generate_memories") is False
            )
            protocol["fixed_model_and_native_goal"] = (
                cfg.get("model") == MODEL
                and cfg.get("model_reasoning_effort") == EFFORT
                and cfg.get("features", {}).get("goals") is True
            )
            protocol["effective_configuration_fingerprint"] = comparator.configuration_fingerprint
            protocol["assigned_loopback_ports_verified"] = False
            protocol["formal_permissions_verified"] = all(
                protocol[k]
                for k in [
                    "restricted_read_verified",
                    "offline_network_verified",
                    "connectors_disabled",
                    "memory_disabled",
                ]
            )
            protocol["git_runtime_verified"] = native_git["exitCode"] == 0
            protocol["formal_permissions_verified"] &= protocol["git_runtime_verified"]
            protocol["formal_network_mode"] = "offline_no_loopback"
            protocol["note"] = (
                "正式题限定离线执行，不需要本地服务端口。固定 Codex 版本开启回环访问会放行全部回环端口，因此第一版对照禁止全部工具网络。"
            )
        finally:
            await comparator.close()
    except Exception as error:
        protocol = {"handshake": False, "error": str(error), "formal_permissions_verified": False}
    status = broker.status()
    await broker.close()
    store = Store(private)
    smoke = next(
        (
            r
            for r in store.list_runs()
            if r["goal"]["original"] == "订阅接入检查"
            and r["phase"] == "completed"
            and r["evidence_mode"] == "real"
            and r["tool_calls"] >= 2
            and r["config"]["model"] == MODEL
            and r["config"]["effort"] == EFFORT
        ),
        None,
    )
    store.close()
    result = {
        "python": sys.version.split()[0],
        "auth": status,
        "real_smoke_passed": bool(smoke) and status["connected"],
        "smoke_run_id": smoke["id"] if smoke else None,
        "codex": protocol,
        "seatbelt": {
            "executes": allowed["exit_code"] == 0,
            "git_runtime_verified": git_check["exit_code"] == 0,
            "denies_private_read": denied["exit_code"] != 0 and "synthetic-test-only" not in denied["stdout"],
            "denies_control_port": "Operation not permitted" in network["stderr"],
        },
        "runtime_roots": [str(p) for p in runtime_roots()],
        "formal_ready": False,
        "real_model_calls": 0,
        "next_conditions": [
            "自身授权与真实模型闭环",
            "Codex 读取权限隔离实测",
            "完整 pilot 与真实并发检查",
            "关键约束反例覆盖",
            "F1 真实多阶段演示",
            "冻结前版本检查",
        ],
    }
    save_report(PROJECT / "outputs/preflight.json", result)
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(doctor()), ensure_ascii=False, indent=2))
