import asyncio
import json
import sys

import pytest

from goalkeeper.engine import Engine
from goalkeeper.models import CheckSpec, Features, Goal, RunConfig, RunState
from goalkeeper.provider import ScriptedProvider
from goalkeeper.sandbox import PermissionDenied, Scope, Seatbelt, runtime_roots
from goalkeeper.store import Store


def tool(name, args, call="one"):
    return {"type": "function_call", "call_id": call, "name": name, "arguments": json.dumps(args)}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private")
    config = RunConfig(engine="controlled", features=Features.baseline(), max_children=0)
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="test", current="test"),
            config=config,
            final_checks=[
                CheckSpec(
                    name="content",
                    argv=[
                        sys.executable,
                        "-c",
                        'from pathlib import Path; assert Path("a.txt").read_text()=="correct"',
                    ],
                )
            ],
        )
    )
    yield store, state
    store.close()


async def test_real_sandbox_blocks_siblings_and_allows_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    forbidden = tmp_path / "grader-secret"
    forbidden.write_text("synthetic-secret")
    sandbox = Seatbelt(Scope(workspace, runtimes=runtime_roots()))
    allowed = await sandbox.run(
        [sys.executable, "-c", 'from pathlib import Path;Path("ok").write_text("allowed")']
    )
    assert allowed["exit_code"] == 0, allowed
    denied = await sandbox.run(["/bin/cat", str(forbidden)])
    assert denied["exit_code"] != 0 and "synthetic-secret" not in denied["stdout"]
    assert (workspace / "ok").read_text() == "allowed"


async def test_readonly_role_and_worker_scope_are_os_enforced(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    readonly = Seatbelt(Scope(tmp_path, writable=False, runtimes=runtime_roots()))
    assert (await readonly.run(["/bin/sh", "-c", "echo bad > a.txt"]))["exit_code"] != 0
    worker = Seatbelt(Scope(tmp_path, write_paths=["a.txt"], runtimes=runtime_roots()))
    assert (await worker.run(["/bin/sh", "-c", "echo good > a.txt"]))["exit_code"] == 0
    assert (await worker.run(["/bin/sh", "-c", "echo bad > b.txt"]))["exit_code"] != 0
    assert (tmp_path / "b.txt").read_text() == "b"


async def test_public_network_and_control_port_are_denied(tmp_path):
    sandbox = Seatbelt(Scope(tmp_path, runtimes=runtime_roots()))
    for host, port in [("1.1.1.1", 443), ("127.0.0.1", 8765)]:
        result = await sandbox.run(
            [
                sys.executable,
                "-c",
                f"import socket;s=socket.socket();s.settimeout(.5);s.connect(({host!r},{port}))",
            ]
        )
        assert result["exit_code"] != 0
        assert "Operation not permitted" in result["stderr"], result


async def test_assigned_loopback_port_works(tmp_path):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await Seatbelt(Scope(tmp_path, runtimes=runtime_roots(), ports=[port])).run(
            [
                sys.executable,
                "-c",
                f'import socket;s=socket.socket();s.connect(("127.0.0.1",{port}));s.close()',
            ]
        )
        assert result["exit_code"] == 0, result
    finally:
        server.close()
        await server.wait_closed()


async def test_tool_timeout_and_output_limit(tmp_path):
    sandbox = Seatbelt(Scope(tmp_path, runtimes=runtime_roots()))
    large = await sandbox.run([sys.executable, "-c", 'print("x"*300000)'])
    assert large["exit_code"] == 0 and len(large["stdout"]) < 100_100 and "truncated" in large["stdout"]
    slow = await sandbox.run([sys.executable, "-c", "import time;time.sleep(20)"], timeout=1)
    assert slow["timed_out"] and not sandbox.processes


def test_paths_reject_parent_symlink_and_git(tmp_path):
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    scope = Scope(tmp_path)
    for path in ["../secret", "escape/secret", ".git/config"]:
        with pytest.raises(PermissionDenied):
            scope.path(path, write=True)


async def test_git_diff_runs_without_launcher_and_git_writes_stay_denied(tmp_path):
    from goalkeeper.files import initialize_repository

    (tmp_path / "source.py").write_text("x = 1\n")
    initialize_repository(tmp_path)
    (tmp_path / "source.py").write_text("x = 2\n")
    sandbox = Seatbelt(Scope(tmp_path, runtimes=runtime_roots()))
    result = await sandbox.run(["git", "diff", "--check"])
    assert result["exit_code"] == 0, result
    denied = await sandbox.run(["git", "add", "source.py"])
    assert denied["exit_code"] != 0


async def test_mock_model_real_tools_and_final_check(setup):
    store, state = setup
    provider = ScriptedProvider(
        [
            [tool("write_file", {"path": "a.txt", "content": "correct"})],
            [tool("finish", {"summary": "done"}, "two")],
        ]
    )
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "completed", result.error
    assert result.tokens_used == 250 and result.tool_calls == 2
    assert not store.unfinished(state.id)


async def test_false_completion_stops_after_two_repairs(setup):
    store, state = setup
    (state.root / "a.txt").write_text("wrong")
    provider = ScriptedProvider([[tool("finish", {"summary": "done"}, str(i))] for i in range(3)])
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "failed" and result.repairs["final"] == 3


def test_uncertain_mutation_is_not_replayed(setup):
    store, state = setup
    (state.root / "a.txt").write_text("changed-before-crash")
    state.messages = [tool("write_file", {"path": "a.txt", "content": "would-overwrite"})]
    state = store.commit(state, "response")
    store.operation(state.id, "op", "running", 1, {"call_id": "one", "before": {}})
    result = Engine(store, ScriptedProvider([])).recover(state)
    assert (state.root / "a.txt").read_text() == "changed-before-crash"
    assert "uncertain" in result.messages[-1]["output"]


def test_completed_result_is_restored_without_reexecution(setup):
    store, state = setup
    state.messages = [tool("write_file", {"path": "a.txt", "content": "x"})]
    state = store.commit(state, "response")
    store.operation(
        state.id, "op", "completed", 1, {"call_id": "one", "result": {"ok": True, "output": "saved"}}
    )
    result = Engine(store, ScriptedProvider([])).recover(state)
    assert json.loads(result.messages[-1]["output"])["output"] == "saved"
    assert not (state.root / "a.txt").exists()


async def test_exhausted_budget_stops_before_model(setup):
    store, state = setup
    state.tokens_used = state.config.token_budget
    state = store.commit(state, "spent")
    provider = ScriptedProvider([])
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "budget_exhausted" and not provider.requests


async def test_missing_file_and_invalid_command_are_recoverable_tool_errors(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    from goalkeeper.models import Goal, RunState, RunConfig, Features, CheckSpec
    from goalkeeper.store import Store
    from goalkeeper.engine import Engine
    from goalkeeper.provider import ScriptedProvider

    store = Store(tmp_path / "private")
    state = store.create(
        RunState(
            workspace=str(root),
            goal=Goal(original="生成 done.txt", current="生成 done.txt"),
            config=RunConfig(features=Features.baseline()),
            final_checks=[CheckSpec(name="文件存在", argv=["/bin/test", "-f", "done.txt"])],
        )
    )
    provider = ScriptedProvider(
        [
            [tool("read_file", {"path": "not-yet-created.txt"}, "missing")],
            [tool("run_command", {"argv": []}, "invalid")],
            [tool("write_file", {"path": "done.txt", "content": "ready"}, "write")],
            [tool("finish", {"summary": "完成"}, "finish")],
        ]
    )
    result = await Engine(store, provider).run(state.id)
    assert result.phase == "completed" and result.tool_calls == 4
    store.close()
