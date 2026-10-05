from fastapi.testclient import TestClient

from goalkeeper.app import create_app
from goalkeeper.auth import AuthBroker


class Vault:
    def get_password(self, *args):
        return None

    def set_password(self, *args):
        raise AssertionError("No authentication is performed")


def client(tmp_path):
    broker = AuthBroker(tmp_path / "private", vault=Vault())
    return TestClient(create_app(tmp_path, broker))


def task_body():
    return {
        "goal": {"original": "制作测试文件", "current": "制作测试文件"},
        "checks": [{"name": "文件检查", "argv": ["/bin/test", "-f", "done.txt"]}],
    }


def test_cookie_origin_and_host_reject_control_requests(tmp_path):
    with client(tmp_path) as c:
        assert c.get("/api/runs").status_code == 401
        assert c.get("/").status_code == 200
        assert c.get("/api/runs").status_code == 200
        assert (
            c.post("/api/runs", json=task_body(), headers={"origin": "https://untrusted.example"}).status_code
            == 403
        )
        assert c.get("/api/runs", headers={"host": "untrusted.example"}).status_code == 403
        assert c.post("/api/smoke").status_code == 400


def test_create_change_goal_memory_toggle_and_export(tmp_path):
    with client(tmp_path) as c:
        c.get("/")
        body = task_body()
        body["config"] = {"engine": "controlled"}
        r = c.post("/api/runs", json=body)
        assert r.status_code == 200, r.text
        state = r.json()
        run = state["id"]
        assert not any(state["config"]["features"].values())
        assert (
            c.patch(f"/api/runs/{run}/goal", json={"current": "制作新文件", "expected_version": 1}).json()[
                "goal"
            ]["version"]
            == 2
        )
        assert (
            c.patch(f"/api/runs/{run}/goal", json={"current": "旧目标", "expected_version": 1}).status_code
            == 409
        )
        pref = c.post(f"/api/runs/{run}/memories", json={"text": "请使用中文解释"}).json()["id"]
        assert c.get(f"/api/runs/{run}/memories").json()[0]["text"] == "请使用中文解释"
        assert c.patch(f"/api/runs/{run}/memories/{pref}", json={"enabled": False}).status_code == 200
        assert c.patch(f"/api/runs/{run}/memory", json={"enabled": False}).status_code == 200
        exported = c.get(f"/api/runs/{run}/export").json()
        assert "goal_changed" in exported["events_jsonl"]
        assert c.delete(f"/api/runs/{run}/memories/{pref}").status_code == 200
        assert c.get(f"/api/runs/{run}/memories").json() == []
        assert c.post(f"/api/runs/{run}/actions", json={"action": "start"}).status_code == 400
        assert c.post(f"/api/runs/{run}/actions", json={"action": "cancel"}).status_code == 200
        assert c.post(f"/api/runs/{run}/actions", json={"action": "resume"}).status_code == 409


def test_invalid_task_and_terminal_result_are_not_overwritten(tmp_path):
    with client(tmp_path) as c:
        c.get("/")
        assert (
            c.post("/api/runs", json={"goal": {"original": "x", "current": "x"}, "checks": []}).status_code
            == 400
        )
        body = task_body()
        body["config"] = {"model": "unapproved-model"}
        assert c.post("/api/runs", json=body).status_code == 400
        body["config"] = {"model": "gpt-6-astra"}
        assert c.post("/api/runs", json=body).status_code == 400
        assert c.get("/api/health").json()["model"] == "gpt-6.1-sol"
        assert c.get("/api/runs/missing").status_code == 404


def test_import_local_repository_without_credentials_or_original_changes(tmp_path):
    from goalkeeper.files import git

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text('print("local source")\n')
    (source / ".env").write_text("secret=value")
    git(source, "init", "-b", "main")
    git(source, "add", "--all")
    with client(tmp_path / "service") as c:
        c.get("/")
        body = {**task_body(), "source_path": str(source)}
        result = c.post("/api/runs", json=body)
        assert result.status_code == 200, result.text
        from pathlib import Path

        root = Path(result.json()["workspace"])
        assert (root / "app.py").read_text() == (source / "app.py").read_text()
        assert not (root / ".env").exists()
        (root / "app.py").write_text("changed")
        assert "local source" in (source / "app.py").read_text()


def test_formal_attempt_cannot_receive_workbench_rescue(tmp_path):
    with client(tmp_path) as c:
        c.get("/")
        state = c.post("/api/runs", json=task_body()).json()
        store = c.app.state.store
        run = store.get(state["id"])
        run.evaluation_phase = "formal"
        store.commit(run, "formal_fixture")
        prefix = "/api/runs/" + state["id"]
        assert c.patch(prefix + "/goal", json={"current": "改目标"}).status_code == 409
        assert c.post(prefix + "/memories", json={"text": "临时答案"}).status_code == 409
        assert c.patch(prefix + "/memory", json={"enabled": False}).status_code == 409
        assert c.post(prefix + "/actions", json={"action": "start"}).status_code == 409
        assert c.get(prefix).status_code == 200


def test_completed_smoke_status_remains_visible_after_restart(tmp_path):
    from goalkeeper.models import Goal, RunState

    with client(tmp_path) as c:
        c.get("/")
        state = RunState(
            workspace=str(tmp_path / "workspace"),
            goal=Goal(original="订阅接入检查", current="test"),
            phase="completed",
            tool_calls=2,
            tokens_used=123,
        )
        c.app.state.store.create(state)
    with client(tmp_path) as c:
        c.get("/")
        status = c.get("/api/health").json()["smoke"]
        assert status["status"] == "passed" and status["run_id"] == state.id and status["tokens"] == 123
