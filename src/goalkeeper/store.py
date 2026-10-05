from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RunState, utcnow, uid
from .secrets import contains_secret, redact


class StaleState(RuntimeError):
    pass


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.blobs = directory / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            directory / "state.sqlite", check_same_thread=False, isolation_level=None, timeout=30
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS processes(run_id TEXT, pid INTEGER, birth TEXT, PRIMARY KEY(run_id,pid));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS goals(run_id TEXT, version INTEGER, data TEXT, PRIMARY KEY(run_id,version));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, run_id TEXT, status TEXT,
                goal_version INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS usage(call_id TEXT PRIMARY KEY, run_id TEXT, role TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS archives(id TEXT PRIMARY KEY, run_id TEXT, goal_version INTEGER,
                timestamp TEXT, text TEXT, blob TEXT, superseded INTEGER DEFAULT 0);
            CREATE VIRTUAL TABLE IF NOT EXISTS archives_fts USING fts5(id UNINDEXED, text, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, text TEXT,
                source TEXT, timestamp TEXT, goal_version INTEGER, enabled INTEGER DEFAULT 1);
        """)

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))

    def create(self, state: RunState) -> RunState:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO runs VALUES(?,?,?)",
                    (state.id, state.version, self.encode(state.model_dump())),
                )
                self.db.execute(
                    "INSERT INTO goals VALUES(?,?,?)",
                    (state.id, state.goal.version, self.encode(state.goal.model_dump())),
                )
                self.event(
                    state.id,
                    "run_created",
                    {
                        "engine": state.config.engine,
                        "configuration_id": hashlib.sha256(
                            state.config.model_dump_json().encode()
                        ).hexdigest(),
                        "goal_version": state.goal.version,
                    },
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return state

    def get(self, run_id: str) -> RunState:
        with self.lock:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return RunState.model_validate_json(row["state"])

    def list_runs(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT state FROM runs ORDER BY rowid DESC").fetchall()
        return [json.loads(row["state"]) for row in rows]

    def commit(self, state: RunState, kind: str, payload: dict | None = None) -> RunState:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                updated = state.model_copy(deep=True)
                updated.version += 1
                result = self.db.execute(
                    "UPDATE runs SET version=?,state=? WHERE id=? AND version=?",
                    (
                        updated.version,
                        self.encode(updated.model_dump()),
                        state.id,
                        state.version,
                    ),
                )
                if result.rowcount != 1:
                    raise StaleState("任务状态已更新，拒绝覆盖新版本。")
                self.db.execute(
                    "INSERT OR IGNORE INTO goals VALUES(?,?,?)",
                    (
                        updated.id,
                        updated.goal.version,
                        self.encode(updated.goal.model_dump()),
                    ),
                )
                self.event(updated.id, kind, payload or {})
                self.db.execute("COMMIT")
                return updated
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def event(self, run_id: str, kind: str, payload: dict) -> int:
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO events(run_id,kind,timestamp,payload) VALUES(?,?,?,?)",
                (run_id, kind, utcnow(), self.encode(payload)),
            )
            return cursor.lastrowid

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500", (run_id, after)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def find_workspace(self, root):
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM runs WHERE json_extract(state, '$.workspace')=?", (str(root),)
            ).fetchone()
            return row["id"] if row else None

    def register_process(self, run_id, pid, birth):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO processes VALUES(?,?,?)", (run_id, pid, birth))

    def finish_process(self, run_id, pid):
        with self.lock:
            self.db.execute("DELETE FROM processes WHERE run_id=? AND pid=?", (run_id, pid))

    def process_records(self, run_id):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM processes WHERE run_id=?", (run_id,))]

    def has_event(self, run_id: str, kind: str, matching: dict | None = None) -> bool:
        with self.lock:
            rows = self.db.execute("SELECT payload FROM events WHERE run_id=? AND kind=?", (run_id, kind))
            return any(
                not matching or all(json.loads(row["payload"]).get(k) == v for k, v in matching.items())
                for row in rows
            )

    def blob(self, text: str) -> str:
        data = redact(text).encode()
        digest = hashlib.sha256(data).hexdigest()
        destination = self.blobs / digest
        if not destination.exists():
            temp = self.blobs / f".{uid()}"
            with temp.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temp.chmod(0o600)
            temp.replace(destination)
        return digest

    def archive(self, run_id: str, goal_version: int, text: str) -> str:
        item_id, timestamp = uid(), utcnow()
        cleaned = redact(text)
        digest = self.blob(cleaned)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO archives VALUES(?,?,?,?,?,?,0)",
                    (item_id, run_id, goal_version, timestamp, cleaned, digest),
                )
                self.db.execute("INSERT INTO archives_fts(id,text) VALUES(?,?)", (item_id, cleaned))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return item_id

    def save_tool_output(self, run_id: str, text: str, tool: str) -> str:
        reference = self.blob(text)
        self.event(run_id, "tool_output_archived", {"blob": reference, "tool": tool})
        return reference

    def read_tool_output(self, run_id: str, reference: str) -> str:
        # A content hash is not authorization. Only results recorded for this run are readable.
        if (
            not isinstance(reference, str)
            or len(reference) != 64
            or any(c not in "0123456789abcdef" for c in reference)
        ):
            raise PermissionError("工具结果引用无效。")
        with self.lock:
            owned = self.db.execute(
                "SELECT 1 FROM events WHERE run_id=? AND kind='tool_output_archived' "
                "AND json_extract(payload, '$.blob')=? LIMIT 1",
                (run_id, reference),
            ).fetchone()
        if not owned:
            raise PermissionError("不能读取其他尝试的工具结果或任意归档文件。")
        return (self.blobs / reference).read_text()

    def search(self, run_id: str, query: str, limit: int = 5, include_superseded: bool = False) -> list[dict]:
        with self.lock:
            valid = "" if include_superseded else " AND a.superseded=0"
            if len(query) >= 3:
                phrase = '"' + query.replace('"', '""') + '"'
                rows = self.db.execute(
                    f"""SELECT a.* FROM archives a JOIN archives_fts f ON f.id=a.id
                    WHERE archives_fts MATCH ? AND a.run_id=? {valid}
                    ORDER BY bm25(archives_fts),a.timestamp DESC LIMIT ?""",
                    (phrase, run_id, limit),
                ).fetchall()
            else:
                rows = []
            if not rows:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = self.db.execute(
                    f"SELECT a.* FROM archives a WHERE a.run_id=? AND a.text LIKE ? ESCAPE '\\' {valid} ORDER BY a.timestamp DESC LIMIT ?",
                    (run_id, f"%{escaped}%", limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def event_count(self, run_id: str, kind: str, matching: dict | None = None) -> int:
        with self.lock:
            rows = self.db.execute("SELECT payload FROM events WHERE run_id=? AND kind=?", (run_id, kind))
            return sum(
                not matching or all(json.loads(row["payload"]).get(k) == v for k, v in matching.items())
                for row in rows
            )

    def source(self, run_id: str, reference: str) -> dict:
        if not isinstance(reference, str) or not reference.startswith(("archive:", "call:")):
            raise ValueError("需要 archive: 或 call: 引用。")
        kind, value = reference.split(":", 1)
        with self.lock:
            if kind == "archive":
                row = self.db.execute(
                    "SELECT * FROM archives WHERE run_id=? AND id=?", (run_id, value)
                ).fetchone()
                if not row:
                    raise PermissionError("归档引用不属于本次尝试。")
                return dict(row)
            rows = self.db.execute(
                "SELECT * FROM archives WHERE run_id=? ORDER BY timestamp DESC", (run_id,)
            ).fetchall()
        state = self.get(run_id)
        histories = [
            {
                "text": json.dumps(state.messages),
                "goal_version": state.goal.version,
                "timestamp": state.created_at,
                "superseded": False,
                "blob": "",
            },
            *[dict(r) for r in rows],
        ]
        for record in histories:
            try:
                items = json.loads(record["text"])
            except ValueError:
                continue
            if not isinstance(items, list):
                continue
            selected = [
                m for m in items if isinstance(m, dict) and m.get("call_id") in {value, "call_" + value}
            ]
            if selected:
                return {**record, "id": reference, "text": json.dumps(selected, ensure_ascii=False)}
        raise PermissionError("工具调用引用不属于本次尝试的原文。")

    def supersede(self, run_id: str, version: int) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE archives SET superseded=1 WHERE run_id=? AND goal_version<?", (run_id, version)
            )

    def operation(self, run_id: str, op_id: str, status: str, version: int, data: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                (op_id, run_id, status, version, self.encode(data)),
            )
            self.event(run_id, f"operation_{status}", {"operation_id": op_id, **data})

    def unfinished(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM operations WHERE run_id=? AND status IN ('prepared','running','completed','uncertain')",
                (run_id,),
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def usage(self, run_id: str, call_id: str, role: str, usage: dict) -> bool:
        with self.lock:
            result = self.db.execute(
                "INSERT OR IGNORE INTO usage VALUES(?,?,?,?)", (call_id, run_id, role, self.encode(usage))
            )
        return result.rowcount == 1

    def add_memory(self, run_id: str, kind: str, text: str, source: str, goal_version: int) -> str:
        if kind not in {"project", "preference", "feedback", "reference"}:
            raise ValueError("记忆类型无效。待办由进度模块保存。")
        if contains_secret(text):
            raise ValueError("记忆包含疑似凭据，已拒绝保存。")
        if not source or source.startswith(("external:", "untrusted:")):
            raise ValueError("记忆必须有可信任务来源。")
        item_id = uid()
        with self.lock:
            self.db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?,?,?,1)",
                (item_id, run_id, kind, text, source, utcnow(), goal_version),
            )
        return item_id

    def memories(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM memories WHERE run_id=? ORDER BY timestamp", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def edit_memory(
        self, run_id: str, item_id: str, text: str | None = None, enabled: bool | None = None
    ) -> None:
        if text is not None and contains_secret(text):
            raise ValueError("记忆包含疑似凭据。")
        with self.lock:
            if text is not None:
                self.db.execute("UPDATE memories SET text=? WHERE id=? AND run_id=?", (text, item_id, run_id))
            if enabled is not None:
                self.db.execute(
                    "UPDATE memories SET enabled=? WHERE id=? AND run_id=?", (int(enabled), item_id, run_id)
                )

    def delete_memory(self, run_id: str, item_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM memories WHERE id=? AND run_id=?", (item_id, run_id))

    def export(self, run_id: str) -> str:
        after, lines = 0, []
        while batch := self.events(run_id, after):
            lines.extend(self.encode(row) for row in batch)
            after = batch[-1]["seq"]
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        self.db.close()
