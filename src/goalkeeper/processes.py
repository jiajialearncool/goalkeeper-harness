"""Persistent process ownership, with PID reuse checks before killing an orphan."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path


def birth(pid: int) -> str | None:
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip() or None


class RunLease:
    def __init__(self, directory: Path, run_id: str):
        root = directory / "leases"
        root.mkdir(exist_ok=True)
        self.file = (root / (run_id + ".lock")).open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("该尝试已经有存活的执行进程。") from None

    def close(self):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def track(store, run_id, runner):
    def spawned(process):
        store.register_process(run_id, process.pid, birth(process.pid))

    def exited(process):
        store.finish_process(run_id, process.pid)

    runner.on_spawn, runner.on_exit = spawned, exited
    return runner


def process_tree_groups(parent_pid):
    output = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid="], capture_output=True, text=True
    ).stdout
    rows = [tuple(map(int, line.split())) for line in output.splitlines() if len(line.split()) == 4]
    owned = {parent_pid}
    for _ in range(12):
        added = {pid for pid, parent, group, user in rows if parent in owned and user == os.getuid()}
        if added.issubset(owned):
            break
        owned.update(added)
    return {group for pid, parent, group, user in rows if pid in owned and user == os.getuid()}


def collect_tree(store, run_id, parent_pid):
    known = {r["pid"] for r in store.process_records(run_id)}
    for group in process_tree_groups(parent_pid) - known:
        if stamp := birth(group):
            store.register_process(run_id, group, stamp)


def orphan_group_matches(group, original_birth):
    if not original_birth:
        return False
    result = subprocess.run(["/bin/ps", "-axo", "pgid=,uid=,lstart="], capture_output=True, text=True)
    members = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3 and parts[0] == str(group):
            members.append(parts)
    try:
        created = datetime.strptime(original_birth, "%a %b %d %H:%M:%S %Y")
        return bool(members) and all(
            int(row[1]) == os.getuid() and datetime.strptime(row[2], "%a %b %d %H:%M:%S %Y") >= created
            for row in members
        )
    except ValueError:
        return False


def reconcile(store, run_id):
    results = []
    for row in store.process_records(run_id):
        actual = birth(row["pid"])
        killed = False
        if (actual and actual == row["birth"]) or (
            actual is None and orphan_group_matches(row["pid"], row["birth"])
        ):
            try:
                os.killpg(row["pid"], signal.SIGKILL)
                killed = True
            except ProcessLookupError:
                pass
        store.finish_process(run_id, row["pid"])
        results.append({"pid": row["pid"], "same_birth": actual == row["birth"], "killed": killed})
    store.event(run_id, "processes_reconciled", {"processes": results})
    return results
