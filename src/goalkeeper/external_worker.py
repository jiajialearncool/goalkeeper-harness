"""Native OpenCode/Pi controllers; local OAuth transport, no GoalKeeper planning or compression."""

from __future__ import annotations
import argparse
import asyncio
import json
import secrets
import socket
import time
from pathlib import Path
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from .auth import AuthBroker, RESOURCE
from .benchmark import Feedback
from .models import MODEL, EFFORT
from .paths import DATA, PROJECT
from .sandbox import Scope, Seatbelt, runtime_roots
from .store import Store
from .verifier import Verifier
from .engine import Engine
from .provider import ScriptedProvider
from .files import digest
from .secrets import redact

TOOLS_ROOT = PROJECT / "work/four-harness-tools"
UNSUPPORTED = {
    "background",
    "conversation",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "moderation",
    "multi_agent",
    "prompt",
    "prompt_cache_retention",
    "safety_identifier",
    "temperature",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
    "previous_response_id",
}


def subscription_payload(body):
    if body.get("model") != MODEL or body.get("reasoning", {}).get("effort") != EFFORT:
        raise ValueError("Requested model/effort differs from fixed evaluation configuration")
    if not isinstance(body.get("input"), list):
        raise ValueError("History must be sent as an array")
    if body.get("previous_response_id"):
        raise ValueError("Server-side history is unsupported")
    result = {k: v for k, v in body.items() if k not in UNSUPPORTED}
    result["store"] = False
    result["stream"] = True
    result["input"] = [{**x, "role": "developer"} if x.get("role") == "system" else x for x in body["input"]]
    if result.get("service_tier") not in {None, "auto", "default"}:
        raise ValueError("Nonstandard service tier is not allowed")
    result.pop("service_tier", None)
    result["include"] = list(dict.fromkeys([*result.get("include", []), "reasoning.encrypted_content"]))
    return result


class Gateway:
    def __init__(self, store, run_id):
        self.store = store
        self.id = run_id
        self.secret = secrets.token_hex(32)
        self.auth = AuthBroker(DATA / "private")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(360, connect=30), trust_env=True)
        self.failures = []
        self.config_errors = []
        self.calls = 0
        self.completed = 0
        self.app = FastAPI()
        self.app.post("/v1/responses")(self.response)
        self.server = None
        self.task = None
        self.base = 0
        self.started = time.monotonic()

    def save(self, kind, payload=None):
        state = self.store.get(self.id)
        state.active_seconds = self.base + time.monotonic() - self.started
        return self.store.commit(state, kind, payload or {})

    async def response(self, request: Request):
        if request.headers.get("authorization") != "Bearer " + self.secret:
            return JSONResponse({"error": {"message": "Invalid local transport token"}}, 401)
        state = self.save("external_checkpoint")
        if (
            state.tokens_used >= state.config.token_budget
            or state.active_seconds >= state.config.seconds_budget
        ):
            return JSONResponse({"error": {"message": "Attempt budget exhausted"}}, 429)
        body = await request.json()
        try:
            payload = subscription_payload(body)
        except ValueError as e:
            self.config_errors.append(str(e))
            return JSONResponse({"error": {"message": str(e)}}, 400)
        self.calls += 1
        call = self.calls
        started = time.monotonic()
        self.store.event(
            self.id,
            "external_model_started",
            {
                "call": call,
                "model": payload["model"],
                "effort": payload["reasoning"]["effort"],
                "removed_unsupported_fields": sorted(set(body) & UNSUPPORTED),
                "request_blob": self.store.blob(json.dumps(payload, ensure_ascii=False)),
            },
        )
        try:
            token = await self.auth.access_token()
            upstream = await self.http.send(
                self.http.build_request(
                    "POST",
                    RESOURCE + "/responses",
                    json=payload,
                    headers={"Authorization": "Bearer " + token},
                ),
                stream=True,
            )
        except Exception as e:
            self.failures.append(type(e).__name__)
            self.store.event(self.id, "external_transport_error", {"call": call, "error": type(e).__name__})
            return JSONResponse({"error": {"message": type(e).__name__}}, 503)
        if upstream.status_code != 200:
            content = await upstream.aread()
            await upstream.aclose()
            try:
                problem = json.loads(content)
            except ValueError:
                problem = {"error": {"message": f"HTTP {upstream.status_code}"}}
            self.failures.append(redact(problem))
            self.store.event(
                self.id,
                "external_provider_error",
                {"call": call, "status": upstream.status_code, "error": redact(problem)},
            )
            return JSONResponse(redact(problem), upstream.status_code)

        async def stream():
            raw = []
            block = []
            finished = False
            try:
                async for line in upstream.aiter_lines():
                    if line.startswith("data:"):
                        block.append(line[5:].lstrip())
                    if line == "" and block:
                        data = "\n".join(block)
                        block = []
                        if data != "[DONE]":
                            try:
                                event = json.loads(data)
                                raw.append(event)
                                if event.get("type") == "response.completed":
                                    finished = True
                                    self.completed += 1
                                    response = event.get("response", {})
                                    usage = response.get("usage")
                                    state = self.store.get(self.id)
                                    if usage is not None:
                                        state.tokens_used += usage.get("input_tokens", 0) + usage.get(
                                            "output_tokens", 0
                                        )
                                    else:
                                        state.usage_complete = False
                                    if response.get("model") not in {None, MODEL}:
                                        self.config_errors.append("Response model mismatch")
                                    self.store.commit(
                                        state,
                                        "external_usage",
                                        {
                                            "call": call,
                                            "model": response.get("model"),
                                            "usage": usage,
                                            "seconds": time.monotonic() - started,
                                        },
                                    )
                                if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                                    self.failures.append(redact(event))
                            except ValueError:
                                pass
                    yield (line + "\n").encode()
            except Exception as e:
                self.failures.append(type(e).__name__)
                self.store.event(
                    self.id, "external_transport_error", {"call": call, "error": type(e).__name__}
                )
                yield (
                    "data: " + json.dumps({"type": "error", "error": {"message": type(e).__name__}}) + "\n\n"
                ).encode()
            finally:
                await upstream.aclose()
                state = self.store.get(self.id)
                if not finished:
                    state.usage_complete = False
                    self.store.commit(state, "external_usage_unknown", {"call": call})
                self.store.event(
                    self.id,
                    "external_model_finished",
                    {
                        "call": call,
                        "completed": finished,
                        "seconds": time.monotonic() - started,
                        "stream_blob": self.store.blob(json.dumps(raw, ensure_ascii=False)),
                    },
                )

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def start(self):
        self.base = self.store.get(self.id).active_seconds
        self.started = time.monotonic()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, log_level="error", access_log=False, lifespan="off")
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            await asyncio.sleep(0.02)

    async def close(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(self.task, 5)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        await self.http.aclose()
        await self.auth.close()


def configuration(group, gateway, sandbox):
    url = f"http://127.0.0.1:{gateway.port}/v1"
    env = sandbox.environment()
    env.update(
        {
            "NO_COLOR": "1",
            "TERM": "dumb",
            "XDG_CONFIG_HOME": str(sandbox.home / "config"),
            "XDG_CACHE_HOME": str(sandbox.home / "cache"),
            "XDG_DATA_HOME": str(sandbox.home / "data"),
            "TIKTOKEN_CACHE_DIR": str(PROJECT / "work/tokenizer-cache"),
        }
    )
    model = {
        "id": MODEL,
        "name": MODEL,
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 1050000,
        "maxTokens": 128000,
        "thinkingLevelMap": {"off": None, "minimal": None, "xhigh": "xhigh"},
        "cost": {"input": 2, "output": 10, "cacheRead": 0.1, "cacheWrite": 2.5},
    }
    if group == "pi":
        agent = sandbox.home / "pi"
        agent.mkdir(exist_ok=True)
        (agent / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "subscription": {
                            "api": "openai-responses",
                            "baseUrl": url,
                            "apiKey": gateway.secret,
                            "models": [model],
                        }
                    }
                }
            )
        )
        env["PI_CODING_AGENT_DIR"] = str(agent)
        return [
            str(TOOLS_ROOT / "node_modules/.bin/pi"),
            "--mode",
            "rpc",
            "--provider",
            "subscription",
            "--model",
            MODEL,
            "--thinking",
            EFFORT,
            "--session-dir",
            str(agent / "sessions"),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
        ], env
    # Native OpenCode Responses SDK; both normal and small-model roles use the frozen model.
    cfg = {
        "model": "subscription/" + MODEL,
        "small_model": "subscription/" + MODEL,
        "share": "disabled",
        "enabled_providers": ["subscription"],
        "provider": {
            "subscription": {
                "npm": "@ai-sdk/openai",
                "name": "ChatGPT subscription transport",
                "options": {"baseURL": url, "apiKey": gateway.secret},
                "models": {
                    MODEL: {
                        "name": MODEL,
                        "reasoning": True,
                        "limit": {"context": 1050000, "output": 128000},
                        "options": {"reasoningEffort": EFFORT, "store": False},
                        "variants": {"xhigh": {"reasoningEffort": EFFORT}},
                    }
                },
            }
        },
        "permission": {"*": "allow", "external_directory": "deny", "webfetch": "deny", "websearch": "deny"},
    }
    config_dir = sandbox.home / "config/opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "package.json").write_text(json.dumps({"dependencies": {"@opencode-ai/plugin": "1.18.34"}}))
    (config_dir / "package-lock.json").write_bytes((TOOLS_ROOT / "package-lock.json").read_bytes())
    link = config_dir / "node_modules"
    if not link.exists():
        link.symlink_to(TOOLS_ROOT / "node_modules", target_is_directory=True)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
    env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "true"
    env["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    return [
        str(TOOLS_ROOT / "node_modules/.bin/opencode"),
        "run",
        "--pure",
        "--print-logs",
        "--log-level",
        "DEBUG",
        "--format",
        "json",
        "--model",
        "subscription/" + MODEL,
        "--variant",
        EFFORT,
    ], env


async def start_opencode_server(sandbox, env, store, run_id, port):
    password = secrets.token_hex(24)
    env["OPENCODE_SERVER_PASSWORD"] = password
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        sandbox.profile(),
        str(TOOLS_ROOT / "node_modules/.bin/opencode"),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
        "--print-logs",
        "--log-level",
        "INFO",
        cwd=sandbox.root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    from .processes import birth

    store.register_process(run_id, process.pid, birth(process.pid))

    async def collect():
        out, err = await process.communicate()
        store.event(
            run_id,
            "external_server_diagnostic",
            {"blob": store.blob((out + err).decode(errors="replace")[-32000:])},
        )

    drain = asyncio.create_task(collect())
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", auth=("opencode", password), trust_env=False, timeout=2
        ) as client:
            for _ in range(20):
                await asyncio.sleep(0.25)
                if process.returncode is not None:
                    raise RuntimeError("OpenCode service exited during startup")
                try:
                    response = await client.get("/global/health")
                    if response.status_code == 200 and response.json().get("healthy"):
                        return process, drain
                except httpx.HTTPError:
                    pass
            raise RuntimeError("OpenCode service health check timed out")
    except BaseException:
        Seatbelt.kill(process)
        await drain
        store.finish_process(run_id, process.pid)
        raise


async def opencode_turn(gateway, text, steps):
    from .processes import collect_tree, reconcile

    store = gateway.store
    state = store.get(gateway.id)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    sandbox = Seatbelt(
        Scope(
            state.root,
            runtimes=runtime_roots(),
            extra_read=[TOOLS_ROOT, Path("/private/var/db/timezone")],
            ports=[gateway.port, port],
        )
    )
    _, env = configuration("opencode", gateway, sandbox)
    process, drain = await start_opencode_server(sandbox, env, store, state.id, port)
    restart = delivered = False
    answer = ""
    last_probe = None
    initial_calls = gateway.calls
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", env["OPENCODE_SERVER_PASSWORD"]),
            trust_env=False,
            timeout=15,
        ) as client:
            session_id = state.native_thread
            if not session_id:
                response = await client.post("/session", json={"title": "Subscription harness comparison"})
                response.raise_for_status()
                session_id = response.json()["id"]
                current = store.get(state.id)
                current.native_thread = session_id
                store.commit(current, "external_session_recorded")
            response = await client.post(
                f"/session/{session_id}/prompt_async",
                json={
                    "model": {"providerID": "subscription", "modelID": MODEL},
                    "agent": "build",
                    "variant": EFFORT,
                    "parts": [{"type": "text", "text": text}],
                },
            )
            response.raise_for_status()
            while True:
                await asyncio.sleep(1)
                collect_tree(store, state.id, process.pid)
                current = gateway.save("external_checkpoint")
                if (
                    current.active_seconds >= current.config.seconds_budget
                    or current.tokens_used >= current.config.token_budget
                ):
                    current.phase = "budget_exhausted"
                    current.failure_origin = "budget"
                    store.commit(current, "external_budget_exhausted")
                    break
                if process.returncode is not None:
                    raise RuntimeError("OpenCode service exited during execution")
                index = store.event_count(state.id, "business_feedback_triggered")
                if index < len(steps):
                    new_digest = digest(current.root)
                    if new_digest != last_probe:
                        last_probe = new_digest
                        feedback = steps[index]
                        verdict = await Verifier(store).check(
                            current.root, [feedback.probe], current.goal.version
                        )
                        if verdict.passed:
                            store.event(
                                state.id,
                                "business_feedback_triggered",
                                {
                                    "kind": feedback.kind,
                                    "index": index,
                                    "probe": verdict.model_dump(),
                                    "handoff": feedback.handoff,
                                },
                            )
                            await client.post(f"/session/{session_id}/abort")
                            if feedback.kind == "goal_change":
                                current = Engine(store, ScriptedProvider([])).update_goal(
                                    state.id,
                                    feedback.goal,
                                    feedback.constraints,
                                    feedback.acceptance,
                                    requirements=feedback.requirements,
                                )
                                current.native_handoff = "用户追加目标：" + feedback.goal
                            else:
                                current = store.get(state.id)
                                current.native_thread = None
                                current.native_handoff = feedback.handoff
                            store.commit(current, "external_feedback_checkpoint")
                            restart = True
                            break
                response = await client.get("/session/status")
                response.raise_for_status()
                status = response.json().get(session_id, {}).get("type", "idle")
                if status == "idle":
                    response = await client.get(f"/session/{session_id}/message")
                    response.raise_for_status()
                    messages = response.json()
                    assistants = [m for m in messages if m.get("info", {}).get("role") == "assistant"]
                    if assistants:
                        last = assistants[-1]
                        store.event(
                            state.id,
                            "external_native_messages",
                            {"blob": store.blob(json.dumps(messages, ensure_ascii=False))},
                        )
                        error = last["info"].get("error")
                        if error:
                            gateway.failures.append(redact(error))
                            break
                        if last["info"].get("finish") and gateway.calls > initial_calls:
                            answer = "\n".join(
                                p.get("text", "") for p in last.get("parts", []) if p.get("type") == "text"
                            )
                            delivered = True
                            break
                    if gateway.config_errors or gateway.failures:
                        break
                if gateway.config_errors:
                    break
    finally:
        collect_tree(store, state.id, process.pid)
        Seatbelt.kill(process)
        await drain
        reconcile(store, state.id)
        if (
            restart
            and steps[store.event_count(state.id, "business_feedback_triggered") - 1].kind != "goal_change"
        ):
            store.event(
                state.id,
                "program_closed_for_resume",
                {
                    "actual_process_exit": process.returncode is not None,
                    "native_process_exit": True,
                    "kind": "new_session",
                },
            )
        gateway.save(
            "external_process_stopped",
            {"returncode": process.returncode, "restart": restart, "native_api": "serve"},
        )
    return restart, delivered, answer


async def native_turn(group, gateway, text, steps):
    if group == "opencode":
        return await opencode_turn(gateway, text, steps)
    store = gateway.store
    state = store.get(gateway.id)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server_port = sock.getsockname()[1]
    sandbox = Seatbelt(
        Scope(
            state.root,
            runtimes=runtime_roots(),
            extra_read=[TOOLS_ROOT, Path("/private/var/db/timezone")],
            ports=[gateway.port, server_port] if group == "opencode" else [gateway.port],
        )
    )
    argv, env = configuration(group, gateway, sandbox)
    server_process = server_drain = None
    if group == "opencode":
        server_process, server_drain = await start_opencode_server(sandbox, env, store, state.id, server_port)
        argv += ["--attach", f"http://127.0.0.1:{server_port}", "--dir", str(state.root)]
    if group == "pi" and state.native_thread:
        argv += ["--session", state.native_thread]
    if group == "opencode":
        if state.native_thread:
            argv += ["--session", state.native_thread]
        argv.append(text)
    profile = sandbox.profile()
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        profile,
        *argv,
        cwd=state.root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=16_000_000,
    )
    gateway.process = process
    # Existing process tracker accepts PID and process-group identity through Store.
    from .processes import birth, collect_tree, reconcile

    store.register_process(state.id, process.pid, birth(process.pid))
    store.event(state.id, "external_process_started", {"pid": process.pid, "group": group})

    async def drain_stderr():
        chunks = []
        while chunk := await process.stderr.read(8192):
            chunks.append(chunk)
            store.event(
                state.id, "external_diagnostic_chunk", {"blob": store.blob(chunk.decode(errors="replace"))}
            )
        return b"".join(chunks)

    stderr_task = asyncio.create_task(drain_stderr())
    if group == "pi":
        process.stdin.write(
            (
                json.dumps({"id": "inspect", "type": "get_state"})
                + "\n"
                + json.dumps({"id": "task", "type": "prompt", "message": text})
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
    done = False
    restart = False
    last_probe = None
    event_count = 0
    answer = []
    model_error = False
    try:
        while not done:
            collect_tree(store, state.id, process.pid)
            if server_process:
                collect_tree(store, state.id, server_process.pid)
            current = gateway.save("external_checkpoint")
            if (
                current.active_seconds >= current.config.seconds_budget
                or current.tokens_used >= current.config.token_budget
            ):
                current.phase = "budget_exhausted"
                current.failure_origin = "budget"
                store.commit(current, "external_budget_exhausted")
                break
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 2)
            except TimeoutError:
                line = None
            if line == b"":
                break
            event = None
            if line:
                try:
                    event = json.loads(line)
                except ValueError:
                    store.event(
                        state.id,
                        "external_non_json",
                        {"blob": store.blob(line.decode(errors="replace")[:10000])},
                    )
            if event:
                event_count += 1
                store.event(
                    state.id,
                    "external_event",
                    {"kind": event.get("type"), "blob": store.blob(json.dumps(event, ensure_ascii=False))},
                )
                if group == "pi" and event.get("type") == "response" and event.get("id") == "inspect":
                    data = event.get("data", {})
                    current = store.get(state.id)
                    current.native_thread = data.get("sessionFile")
                    store.commit(current, "external_session_recorded")
                    if data.get("thinkingLevel") != EFFORT or data.get("model", {}).get("id") != MODEL:
                        gateway.config_errors.append("Pi effective model/effort mismatch")
                if group == "opencode" and event.get("sessionID"):
                    current = store.get(state.id)
                    current.native_thread = event["sessionID"]
                    store.commit(current, "external_session_recorded")
                if group == "pi" and event.get("type") == "response" and event.get("success") is False:
                    gateway.config_errors.append(event.get("error", "RPC command failed"))
                    break
                if (
                    group == "pi"
                    and event.get("type") == "message_end"
                    and event.get("message", {}).get("stopReason") in {"error", "aborted"}
                ):
                    model_error = True
                if event.get("type") == "error":
                    model_error = True
                if group == "pi" and event.get("type") in {"agent_end", "agent_settled"}:
                    done = True
                if event.get("type") == "text":
                    answer.append(event.get("part", {}).get("text", ""))
            index = store.event_count(state.id, "business_feedback_triggered")
            if index < len(steps):
                # Common business-progress probe; never based on a harness's own milestone.
                current = store.get(state.id)
                new_digest = digest(current.root)
                if new_digest != last_probe:
                    last_probe = new_digest
                    feedback = steps[index]
                    verdict = await Verifier(store).check(
                        current.root, [feedback.probe], current.goal.version
                    )
                    if verdict.passed:
                        store.event(
                            state.id,
                            "business_feedback_triggered",
                            {
                                "kind": feedback.kind,
                                "index": index,
                                "probe": verdict.model_dump(),
                                "handoff": feedback.handoff,
                            },
                        )
                        if feedback.kind == "goal_change":
                            current = Engine(store, ScriptedProvider([])).update_goal(
                                state.id,
                                feedback.goal,
                                feedback.constraints,
                                feedback.acceptance,
                                requirements=feedback.requirements,
                            )
                            current.native_handoff = "用户追加目标：" + feedback.goal
                        else:
                            current = store.get(state.id)
                            current.native_thread = None
                            current.native_handoff = feedback.handoff
                            store.event(
                                state.id,
                                "program_closed_for_resume",
                                {
                                    "kind": feedback.kind,
                                    "actual_process_exit": True,
                                    "native_process_exit": True,
                                },
                            )
                        store.commit(current, "external_feedback_checkpoint")
                        restart = True
                        break
            if gateway.config_errors:
                break
    finally:
        collect_tree(store, state.id, process.pid)
        Seatbelt.kill(process)
        await process.wait()
        if server_process:
            collect_tree(store, state.id, server_process.pid)
            Seatbelt.kill(server_process)
            await server_drain
        reconcile(store, state.id)
        err = await stderr_task
        if err:
            store.event(
                state.id, "external_stderr", {"blob": store.blob(err.decode(errors="replace")[-32000:])}
            )
        gateway.save(
            "external_process_stopped",
            {"returncode": process.returncode, "event_count": event_count, "restart": restart},
        )
    if process.returncode in {-5, -6, -11} and gateway.calls == 0:
        raise RuntimeError("Native runtime failed before model request: " + str(process.returncode))
    return restart, (done or process.returncode == 0) and not model_error, "\n".join(answer)


async def run(run_id, control):
    store = Store(DATA / "private")
    gateway = Gateway(store, run_id)
    state = store.get(run_id)
    steps = [Feedback.model_validate(x) for x in json.loads(control.read_text()).get("feedback_sequence", [])]
    try:
        await gateway.start()
        state.phase = "running"
        state.error = None
        store.commit(state, "external_running")
        for attempt in range(5):
            state = store.get(run_id)
            if state.phase == "budget_exhausted":
                break
            text = (
                "请在任务目录完成项目并运行必要检查。依赖已经安装，禁止公网，不能读取其他任务。所有工作、复核与摘要共用30分钟和100万可观测token预算；最多委派两个子Agent，子Agent不可继续委派。"
                + state.goal.model_dump_json()
                + "\n公共检查："
                + json.dumps([x.model_dump() for x in state.final_checks], ensure_ascii=False)
                + "\n"
                + state.native_handoff
            )
            restart, delivered, answer = await native_turn(state.config.engine, gateway, text, steps)
            state = store.get(run_id)
            if restart:
                continue
            if gateway.config_errors:
                state.phase = "failed"
                state.failure_origin = "configuration"
                state.error = str(gateway.config_errors[-1])
                store.commit(state, "external_configuration_failed")
                break
            if gateway.failures and not delivered:
                state.phase = "resource_interrupted"
                state.failure_origin = "resource"
                state.error = str(gateway.failures[-1])[:2000]
                store.commit(state, "external_resource_interrupted")
                break
            if state.phase == "budget_exhausted":
                break
            # Same small public check and repair allowance as the Codex controller.
            verdict = await Verifier(store).check(state.root, state.final_checks, state.goal.version)
            store.event(run_id, "external_final_checked", verdict.model_dump())
            if (
                verdict.passed
                and store.event_count(run_id, "business_feedback_triggered") == len(steps)
                and delivered
            ):
                state = store.get(run_id)
                state.phase = "completed"
                state.answer = answer
                store.commit(state, "external_delivered")
                break
            repairs = state.repairs.get("final", 0) + 1
            state.repairs["final"] = repairs
            if repairs > state.config.repair_rounds:
                state.phase = "failed"
                state.failure_origin = "task"
                state.error = "最终公共检查或反馈流程未完成"
                store.commit(state, "external_task_failed")
                break
            state.native_handoff = "公共检查结果：" + verdict.model_dump_json() + "；请完成尚未完成的要求。"
            store.commit(state, "external_repair")
    except Exception as e:
        state = store.get(run_id)
        state.phase = "failed"
        state.failure_origin = "integration"
        state.error = type(e).__name__ + ": " + str(e)
        store.commit(state, "external_controller_failed")
        raise
    finally:
        state = gateway.save("external_checkpoint_stop")
        if state.phase == "completed" and (
            state.tokens_used > state.config.token_budget
            or state.active_seconds > state.config.seconds_budget
        ):
            state.phase = "budget_exhausted"
            state.failure_origin = "budget"
            store.commit(state, "external_terminal_budget_check")
        await gateway.close()
        store.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--control", type=Path, required=True)
    args = p.parse_args()
    asyncio.run(run(args.run_id, args.control))
