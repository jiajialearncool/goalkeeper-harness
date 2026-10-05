"""Codex is a comparator only. Experimental runtime never imports this adapter."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import re
import hashlib
import urllib.request
from pathlib import Path

from .models import EFFORT, MODEL
from .secrets import redact

CODEX = Path(__file__).resolve().parents[2] / "work/pinned-codex-0.159.0/codex"
PINNED_VERSION = "0.159.0"


class ProtocolError(RuntimeError):
    pass


class ConfigurationError(ProtocolError):
    pass


def normalized_configuration(config: dict, workspace: Path) -> str:
    value = dict(redact(config))
    # thread/start registers task directories globally. Unrelated project entries
    # are not effective settings for this run; its own trust policy is fixed below.
    value["projects"] = {
        str(workspace.resolve()): value.get("projects", {}).get(str(workspace.resolve()), {})
    }
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False)
        .replace(str(workspace.parent / "native-agent-role.toml"), "<ROLE_CONFIGURATION>")
        .replace(str(workspace.resolve()), "<WORKSPACE>")
    )


class CodexClient:
    def __init__(self, workspace: Path | None = None, read_roots: list[Path] | None = None):
        self.workspace = workspace
        self.read_roots = read_roots or []
        self.profile_id = "goalkeeper-evaluation"
        self.process = None
        self.reader = None
        self.stderr_task = None
        self.diagnostics = b""
        self.counter = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.usage_by_thread: dict[str, dict] = {}
        self.thread_id = None
        self.on_spawn = None
        self.on_exit = None
        self.configuration_fingerprint = None

    async def start(self):
        for name in ["codex", "codex-code-mode-host", "rg"]:
            if not (CODEX.parent / name).is_file():
                raise ConfigurationError("固定 Codex 安装缺少执行组件：" + name)
        version = subprocess.run([str(CODEX), "--version"], capture_output=True, text=True, timeout=10)
        if version.stdout.strip() != f"codex-cli {PINNED_VERSION}":
            raise ConfigurationError("Codex 版本不符合冻结要求。")
        overrides = []
        if self.workspace:
            rules = {":root": "deny", ":minimal": "read", str(self.workspace.resolve()): "write"}
            rules.update({str(p.resolve()): "read" for p in self.read_roots})
            rules[str(self.workspace.resolve() / ".git")] = "read"
            profile = (
                "{filesystem={"
                + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in rules.items())
                + "},network={enabled=false}}"
            )
            settings = {
                "default_permissions": json.dumps(self.profile_id),
                "permissions." + self.profile_id: profile,
                "approval_policy": '"never"',
                "web_search": '"disabled"',
                "features.multi_agent": "true",
                "features.goals": "true",
                "model": json.dumps(MODEL),
                "model_reasoning_effort": json.dumps(EFFORT),
                "features.memory_tool": "false",
                "features.apps": "false",
                "features.browser": "false",
                "features.computer_use": "false",
                "memories.use_memories": "false",
                "memories.generate_memories": "false",
                "project_doc_max_bytes": "0",
                "agents.max_threads": "3",
                "agents.max_depth": "1",
                "shell_environment_policy.inherit": '"none"',
                "allow_login_shell": "false",
                "projects": "{" + json.dumps(str(self.workspace.resolve())) + '={trust_level="trusted"}}',
            }
            # Enumerate connector names without exposing credentials or editing the user's configuration.
            inspector = CodexClient()
            try:
                await inspector.start()
                cfg = (await inspector.request("config/read", {"includeLayers": False})).get("config", {})
                for name in cfg.get("mcp_servers", {}):
                    settings["mcp_servers." + name + ".enabled"] = "false"
                for name in cfg.get("plugins", {}):
                    settings["plugins." + name + ".enabled"] = "false"
                role_file = self.workspace.parent / "native-agent-role.toml"
                role_file.write_text(
                    f"model = {json.dumps(MODEL)}\nmodel_reasoning_effort = {json.dumps(EFFORT)}\n"
                )
                role_file.chmod(0o600)
                roles = {"default", "explorer", "worker"} | {
                    k for k, v in (cfg.get("agents") or {}).items() if isinstance(v, dict)
                }
                for name in roles:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
                        raise ProtocolError("自定义子 Agent 角色名无法安全固定配置。")
                    settings["agents." + name + ".config_file"] = json.dumps(str(role_file))
            finally:
                await inspector.close()
            from .sandbox import Scope, Seatbelt

            sandbox_env = Seatbelt(Scope(self.workspace, runtimes=self.read_roots)).environment()
            settings["shell_environment_policy.set"] = (
                "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in sandbox_env.items()) + "}"
            )
            for key, value in settings.items():
                overrides.extend(["-c", key + "=" + value])
        transport_env = dict(os.environ)
        # Rust clients read proxy environment variables, unlike Python's macOS
        # system-proxy discovery. Respect the user's existing transport settings.
        for scheme, address in urllib.request.getproxies().items():
            if scheme in {"http", "https", "all"}:
                transport_env.setdefault(scheme.upper() + "_PROXY", address)
        self.process = await asyncio.create_subprocess_exec(
            str(CODEX),
            *overrides,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=16_000_000,
            env=transport_env,
        )
        if self.on_spawn:
            self.on_spawn(self.process)
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.reader = asyncio.create_task(self._read())
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "goalkeeper_evaluation", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})
        if self.workspace:
            effective = (
                await self.request("config/read", {"includeLayers": False, "cwd": str(self.workspace)})
            )["config"]
            normalized = normalized_configuration(effective, self.workspace)
            self.configuration_fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        return result

    async def _drain_stderr(self):
        while chunk := await self.process.stderr.read(8192):
            self.diagnostics = (self.diagnostics + chunk)[-16000:]

    async def _send(self, body):
        self.process.stdin.write((json.dumps(body, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def notify(self, method, params):
        await self._send({"method": method, "params": params})

    async def request(self, method, params, timeout=30):
        self.counter += 1
        number = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        await self._send({"id": number, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(number, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(str(redact(message["error"]))))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    # A formal run never stalls waiting for a human or grants additional permissions.
                    method = message["method"]
                    decision = {"decision": "decline"}
                    if method == "item/tool/requestUserInput":
                        decision = {"answers": {}}
                    if method == "item/permissions/requestApproval":
                        decision = {"permissions": {}, "scope": "turn"}
                    await self._send({"id": message["id"], "result": decision})
                    await self.events.put({"method": "approval_rejected", "params": {"request": method}})
                else:
                    params = message.get("params", {})
                    if message.get("method") == "thread/tokenUsage/updated":
                        self.usage_by_thread[params["threadId"]] = params["tokenUsage"]["total"]
                    await self.events.put(redact(message))
        except (ValueError, OSError) as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError(type(error).__name__))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        ProtocolError(
                            "app-server 已退出："
                            + str(redact(self.diagnostics.decode(errors="replace")[-3000:]))
                        )
                    )

    @property
    def observed_tokens(self):
        # Native child thread totals are counted once per thread; cached/reasoning subsets aren't added again.
        return sum(v.get("inputTokens", 0) + v.get("outputTokens", 0) for v in self.usage_by_thread.values())

    async def create_thread(self, workspace: Path):
        result = await self.request(
            "thread/start",
            {
                "cwd": str(workspace),
                "model": MODEL,
                "approvalPolicy": "never",
                "permissions": self.profile_id,
                "ephemeral": False,
                "config": {
                    "model_reasoning_effort": EFFORT,
                    "agents.max_threads": 3,
                    "agents.max_depth": 1,
                    "features.multi_agent": True,
                    "features.goals": True,
                    "features.memory_tool": False,
                },
            },
        )
        self.thread_id = result["thread"]["id"]
        return self.thread_id

    async def set_goal(self, objective: str | None = None, budget: int | None = None, status="active"):
        if objective is not None and (not objective.strip() or len(objective) > 4000):
            raise ProtocolError("原生 Goal 的目标必须为 1–4,000 字符，禁止截断后评测。")
        return await self.request(
            "thread/goal/set",
            {"threadId": self.thread_id, "objective": objective, "tokenBudget": budget, "status": status},
        )

    async def turn(self, text: str, workspace: Path, read_roots: list[Path]):
        return await self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "model": MODEL,
                "effort": EFFORT,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never",
                "permissions": self.profile_id,
            },
        )

    async def steer(self, text: str, turn_id: str):
        return await self.request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def interrupt(self, turn_id: str):
        return await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id})

    async def resume(self, thread_id: str, workspace: Path):
        result = await self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "model": MODEL,
                "permissions": self.profile_id,
            },
        )
        self.thread_id = thread_id
        return result

    async def close(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
            if self.on_exit:
                self.on_exit(self.process)
        if self.stderr_task:
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


async def inspect_protocol(schema_dir: Path, own_email: str | None = None) -> dict:
    """Local read-only handshake. Does not start a thread, Goal, turn, or model inference."""
    schema = schema_dir / "v2/TurnStartParams.json"
    supports_read_scope = schema.exists() and "permissions" in json.loads(schema.read_text()).get(
        "properties", {}
    )
    client = CodexClient()
    try:
        initialized = await client.start()
        account = await client.request("account/read", {})
        return {
            "version": PINNED_VERSION,
            "handshake": True,
            "user_agent": initialized.get("userAgent"),
            "signed_in": bool(account.get("account")),
            "same_chatgpt_account": bool(
                own_email
                and (account.get("account") or {}).get("email", "").casefold() == own_email.casefold()
            )
            if own_email
            else None,
            "restricted_read_schema": supports_read_scope,
            "formal_permissions_verified": False,
            "note": "必须实际拒绝评分包、其他工作区和凭据访问，不能仅凭 workspace-write 宣称读取已隔离。",
        }
    finally:
        await client.close()
