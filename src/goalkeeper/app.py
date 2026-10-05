from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse

from .auth import AuthBroker, AuthError
from .paths import DATA, PROJECT, initialize
from .provider import ResponsesProvider, ProviderError
from .secrets import redact
from .store import Store, StaleState
from .models import EFFORT, MODEL, Features, Goal, RunConfig, RunState, CheckSpec, uid


def create_app(data_dir: Path | None = None, auth: AuthBroker | None = None) -> FastAPI:
    directory = data_dir or DATA
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / "private")
    broker = auth or AuthBroker(directory / "private")
    provider = ResponsesProvider(broker)
    session_secret = secrets.token_urlsafe(48)
    service = {"engine": None, "tasks": {}, "smoke": {"status": "not_run"}}

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in service["tasks"].values():
            task.cancel()
        await asyncio.gather(*service["tasks"].values(), return_exceptions=True)
        await provider.close()
        await broker.close()
        store.close()

    app = FastAPI(title="GoalKeeper Harness", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.auth, app.state.service = store, broker, service

    def editable_run(run_id):
        state = store.get(run_id)
        if state.evaluation_phase == "formal":
            raise HTTPException(409, "正式尝试由冻结的调度器控制，工作台仅供查看。")
        return state

    def smoke_status():
        saved = next(
            (
                row
                for row in store.list_runs()
                if row["goal"]["original"] == "订阅接入检查"
                and row["config"]["model"] == MODEL
                and row.get("evidence_mode") == "real"
            ),
            None,
        )
        if not saved:
            return {"status": "not_run"}
        phase = saved["phase"]
        status = (
            "passed"
            if phase == "completed" and saved["tool_calls"] >= 2
            else "running"
            if phase in {"running", "recovering"}
            else "not_run"
            if phase == "ready"
            else phase
        )
        return {
            "status": status,
            "run_id": saved["id"],
            "tokens": saved["tokens_used"],
            "error": saved.get("error"),
            "usage_complete": saved["usage_complete"],
        }

    @app.exception_handler(StaleState)
    async def stale_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def input_error(request, error):
        return JSONResponse({"detail": "输入不符合要求，请核对字段、类型和验收命令。"}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing_error(request, error):
        return JSONResponse({"detail": "任务或必填字段不存在。"}, status_code=404)

    @app.exception_handler(AuthError)
    async def auth_error(request, error):
        return JSONResponse({"detail": redact(str(error))}, status_code=401)

    @app.exception_handler(ProviderError)
    async def model_error(request, error):
        return JSONResponse({"detail": redact(str(error))}, status_code=503 if error.resource else 400)

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        allowed_hosts = {"127.0.0.1", "localhost", "testserver"}
        if request.url.hostname not in allowed_hosts:
            return HTMLResponse("只允许本机访问。", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return HTMLResponse("跨来源请求已拒绝。", status_code=403)
        root_request = request.url.path == "/" and request.method == "GET"
        static_request = request.url.path.startswith("/assets/")
        if not root_request and not static_request:
            cookie = request.cookies.get("gk_session", "")
            if not secrets.compare_digest(cookie, session_secret):
                return HTMLResponse("请从工作台首页建立本机会话。", status_code=401)
        response = await call_next(request)
        if root_request:
            response.set_cookie("gk_session", session_secret, httponly=True, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    async def index():
        built = PROJECT / "frontend/dist/index.html"
        if built.exists():
            return FileResponse(built)
        return HTMLResponse("""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>GoalKeeper · 接入准备</title><style>body{font:17px system-ui;background:#f4f6f4;color:#24372c;margin:10vh auto;max-width:720px;padding:32px}h1{font-size:42px}a,button{display:inline-block;background:#254e38;color:white;padding:14px 24px;border:0;border-radius:12px;text-decoration:none;font-size:16px}.note{line-height:1.8;color:#657267}</style>
<h1>GoalKeeper Harness</h1><p>守住目标，把任务做到可验收。</p>
<p class="note">第一步是授权 GoalKeeper 自身使用你的 ChatGPT 订阅。凭据由本机钥匙串保存。完成授权后返回此页，程序仍需验证模型和工具闭环。</p>
<a href="/auth/start">Continue with ChatGPT</a><pre id="status"></pre>
<script>setInterval(async()=>{let r=await fetch('/api/auth/status');document.querySelector('#status').textContent=JSON.stringify(await r.json(),null,2)},2500)</script></html>""")

    @app.get("/assets/{asset:path}")
    async def assets(asset: str):
        root = (PROJECT / "frontend/dist/assets").resolve()
        path = (root / asset).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/api/auth/status")
    async def auth_status():
        return broker.status()

    @app.get("/auth/start")
    async def auth_start(request: Request):
        callback = f"http://127.0.0.1:{request.url.port or 8765}/auth/callback"
        url = broker.begin(callback, request.cookies["gk_session"])
        return RedirectResponse(url)

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            await broker.callback(dict(request.query_params), request.cookies["gk_session"])
        except Exception as error:
            broker.last_error = str(redact(str(error)))
            return HTMLResponse(
                "<h2>授权未完成</h2><p>请返回工作台查看原因并重新授权。</p><a href='/'>返回工作台</a>",
                status_code=400,
            )
        # Strip authorization code from browser URL immediately; access logs are disabled at startup.
        return RedirectResponse("/?authorized=1", status_code=303)

    @app.post("/api/auth/logout")
    async def logout():
        if any(not task.done() for task in service["tasks"].values()):
            raise HTTPException(409, "仍有运行任务，请先停止。")
        broker.logout()
        return broker.status()

    @app.get("/api/models")
    async def model_catalog():
        try:
            models = await provider.models()
            return {"models": models, "required": MODEL, "effort": EFFORT}
        except (AuthError, RuntimeError) as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/runs")
    async def runs():
        return [
            {
                k: s[k]
                for k in (
                    "id",
                    "goal",
                    "config",
                    "phase",
                    "tokens_used",
                    "active_seconds",
                    "created_at",
                    "error",
                )
            }
            for s in store.list_runs()
        ]

    @app.get("/api/runs/{run_id}")
    async def run_state(run_id: str):
        try:
            saved = store.get(run_id)
            state = saved.model_dump()
            from .assurance import requirements_status

            state["requirement_results"] = (
                requirements_status(saved) if saved.config.engine == "goalkeeper" else []
            )
            state.pop("messages", None)
            return state
        except KeyError:
            raise HTTPException(404, "任务不存在。")

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isdigit():
            after = max(after, int(last_id))

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                batch = store.events(run_id, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: state\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/memories")
    async def memories(run_id: str):
        return store.memories(run_id)

    @app.patch("/api/runs/{run_id}/memories/{item_id}")
    async def edit_memory(run_id: str, item_id: str, request: Request):
        editable_run(run_id)
        payload = await request.json()
        try:
            store.edit_memory(run_id, item_id, payload.get("text"), payload.get("enabled"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return {"ok": True}

    @app.delete("/api/runs/{run_id}/memories/{item_id}")
    async def delete_memory(run_id: str, item_id: str):
        editable_run(run_id)
        store.delete_memory(run_id, item_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        return {"events_jsonl": store.export(run_id), "memories": store.memories(run_id)}

    @app.get("/api/health")
    async def health():
        from .evaluation import read, source_manifest

        active = read(directory / "private/active-round.json").get("round")
        frozen = (
            read(directory / "private" / active / "freeze.json")
            if active
            else read(directory / "private/freeze.json")
        )
        frozen_ready = bool(frozen) and frozen.get("files") == source_manifest()
        return {
            "status": "ready",
            "auth": broker.status(),
            "smoke": smoke_status(),
            "model": MODEL,
            "effort": EFFORT,
            "implementation": "frozen" if frozen_ready else "development",
            "formal_ready": frozen_ready,
        }

    @app.post("/api/runs")
    async def create_run(request: Request):
        payload = await request.json()
        config = RunConfig.model_validate(payload.get("config", {}))
        config.require_current_model()
        if config.engine == "codex":
            raise HTTPException(400, "Codex 组通过评测调度器运行。")
        if config.engine == "controlled":
            config.features = Features.baseline()
        goal = Goal.model_validate(payload["goal"])
        run_id = uid()
        root = directory / "runs" / run_id / "workspace"
        checks = [CheckSpec.model_validate(c) for c in payload.get("checks", [])]
        if not checks:
            raise HTTPException(400, "至少指定一条最终检查命令，避免凭文字声明完成。")
        root.mkdir(parents=True)
        source = payload.get("source_path", "").strip()
        if source:
            from .files import import_repository

            try:
                import_repository(Path(source), root)
            except (ValueError, RuntimeError) as error:
                import shutil

                shutil.rmtree(root)
                raise HTTPException(400, str(error)) from error
        state = store.create(
            RunState(id=run_id, workspace=str(root), goal=goal, config=config, final_checks=checks)
        )
        return state.model_dump(exclude={"messages"})

    @app.post("/api/smoke")
    async def smoke():
        if service["smoke"]["status"] == "running" or smoke_status()["status"] == "running":
            raise HTTPException(409, "接入检查已经运行。")
        if not broker.status()["connected"]:
            raise HTTPException(400, "请先完成 GoalKeeper 自身的订阅授权。")
        spent = sum(s["tokens_used"] for s in store.list_runs() if s["goal"]["original"] == "订阅接入检查")
        if spent >= 50_000:
            raise HTTPException(409, "接入检查的累计 50,000 token 预算已用完。")
        models = await provider.models()
        chosen = next(
            (m for m in models if (m.get("slug") or m.get("id")) == MODEL and m.get("visibility") == "list"),
            None,
        )
        if not chosen:
            raise HTTPException(400, f"该账号的模型列表没有 {MODEL}，不能静默更换模型。")
        if not any(level.get("effort") == EFFORT for level in chosen.get("supported_reasoning_levels", [])):
            raise HTTPException(400, f"当前模型列表未确认 {MODEL} 支持 {EFFORT}，不能降低推理档位。")
        run_id = uid()
        root = directory / "runs" / run_id / "workspace"
        root.mkdir(parents=True)
        import sys

        checks = [
            CheckSpec(
                name="本地工具闭环",
                argv=[
                    sys.executable,
                    "-c",
                    'from pathlib import Path; assert Path("note.txt").read_text().strip()=="OK"',
                ],
            )
        ]
        store.create(
            RunState(
                id=run_id,
                workspace=str(root),
                goal=Goal(
                    original="订阅接入检查",
                    current="使用 write_file 写入 note.txt，内容为 OK；然后调用 finish。",
                ),
                config=RunConfig(
                    engine="controlled",
                    features=Features.baseline(),
                    max_children=0,
                    token_budget=50_000 - spent,
                    seconds_budget=300,
                ),
                final_checks=checks,
            )
        )
        service["smoke"] = {"status": "running", "run_id": run_id}

        async def test():
            result = await engine().run(run_id)
            service["smoke"] = {
                "status": "passed" if result.phase == "completed" and result.tool_calls >= 2 else "failed",
                "run_id": run_id,
                "tokens": result.tokens_used,
                "error": result.error,
                "usage_complete": result.usage_complete,
            }

        service["tasks"][run_id] = asyncio.create_task(test())
        return service["smoke"]

    @app.post("/api/runs/{run_id}/memories")
    async def add_preference(run_id: str, request: Request):
        state = editable_run(run_id)
        payload = await request.json()
        item = store.add_memory(run_id, "preference", payload["text"], "user:workbench", state.goal.version)
        return {"id": item}

    @app.patch("/api/runs/{run_id}/memory")
    async def memory_toggle(run_id: str, request: Request):
        state = editable_run(run_id)
        state.memory_enabled = bool((await request.json())["enabled"])
        store.commit(state, "memory_toggled")
        return {"ok": True}

    def engine():
        if service["engine"] is None:
            from .engine import Engine

            service["engine"] = Engine(store, provider)
        return service["engine"]

    @app.post("/api/runs/{run_id}/actions")
    async def actions(run_id: str, request: Request):
        payload = await request.json()
        action = payload.get("action")
        try:
            state = editable_run(run_id)
        except KeyError:
            raise HTTPException(404, "任务不存在。")
        if state.phase in {"completed", "cancelled", "budget_exhausted", "failed"}:
            raise HTTPException(409, "该尝试已经结束，不能更改结果或重置预算。")
        if action in {"pause", "cancel"}:
            state.phase = "paused" if action == "pause" else "cancelled"
            store.commit(state, action)
            if task := service["tasks"].get(run_id):
                task.cancel()
        elif action in {"start", "resume"}:
            state.config.require_current_model()
            if state.phase in {"completed", "cancelled", "budget_exhausted"}:
                raise HTTPException(409, "该任务已经终止，不能重置预算后继续。")
            if existing := service["tasks"].get(run_id):
                if not existing.done():
                    raise HTTPException(409, "任务已经运行。")
            if not broker.status()["connected"]:
                raise HTTPException(400, "请先授权 GoalKeeper。")
            service["tasks"][run_id] = asyncio.create_task(engine().run(run_id))
        else:
            raise HTTPException(400, "动作必须是 start、pause、resume 或 cancel。")
        return {"ok": True}

    @app.patch("/api/runs/{run_id}/goal")
    async def goal_update(run_id: str, request: Request):
        editable_run(run_id)
        payload = await request.json()
        return (
            engine()
            .update_goal(
                run_id,
                payload["current"],
                payload.get("constraints"),
                payload.get("acceptance"),
                payload.get("expected_version"),
            )
            .model_dump()
        )

    return app


initialize()
app = create_app()
