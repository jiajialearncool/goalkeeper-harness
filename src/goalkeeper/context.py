from __future__ import annotations

import json
import hashlib
import os
import re
from copy import deepcopy
from pathlib import Path

import tiktoken

from .models import MODEL, RunState, Goal, ToolResult
from .store import Store


class ContextLimit(RuntimeError):
    pass


def fact_allowed(goal: Goal, text: str, source: str) -> bool:
    if Path(source.removeprefix("file:")).name.casefold() in {"task.md", "goal.md", "agents.md"}:
        return False
    if any(word in text.casefold() for word in ["todo", "待办", "验收标准", "任务目标", "里程碑"]):
        return False
    goal_parts = [goal.current, goal.original, *goal.constraints, *goal.acceptance]
    phrases = [
        part.strip()
        for value in goal_parts
        for part in re.split(r"[。；;\n]", value)
        if len(part.strip()) >= 8
    ]
    return not any(part in text or (len(text) >= 8 and text in part) for part in phrases)


class ContextManager:
    tool_result_tokens = 4096

    def __init__(self, store: Store):
        self.store = store
        os.environ.setdefault(
            "TIKTOKEN_CACHE_DIR", str(Path(__file__).resolve().parents[2] / "work/tokenizer-cache")
        )
        try:
            self.encoder = tiktoken.encoding_for_model(MODEL)
        except KeyError:
            self.encoder = tiktoken.get_encoding("o200k_base")

    def count(self, value) -> int:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return len(self.encoder.encode(text, disallowed_special=()))

    def trim(self, text: str, tokens: int) -> str:
        return self.encoder.decode(self.encoder.encode(text, disallowed_special=())[: max(0, tokens)])

    def request_tokens(self, messages: list[dict], instructions: str, tools: list[dict]) -> int:
        cleaned = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        return self.count(cleaned) + self.count(instructions) + self.count(tools)

    @staticmethod
    def summary_history(messages: list[dict]) -> list[dict]:
        """Prepare readable summary material without changing archived or live protocol items.

        Filter only Responses item fields. Tool arguments, results and visible reasoning
        remain intact, including business data that happens to use the same field names.
        """
        return [
            {k: deepcopy(v) for k, v in item.items() if k != "encrypted_content" and not k.startswith("_")}
            for item in messages
        ]

    def summary_material(self, state: RunState, messages: list[dict]) -> list[dict]:
        """Bound completed-tool material only; the archive and live items stay exact."""
        prepared = self.summary_history(messages)
        if not state.config.efficiency_enabled:
            return prepared
        completed, calls, latest = {}, {}, {}
        for item in prepared:
            if item.get("type") == "function_call":
                calls[item.get("call_id")] = item
            elif item.get("type") == "function_call_output":
                try:
                    result = json.loads(item["output"])
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(result, dict) and result.get("ok") is True:
                    completed[item.get("call_id")] = result
        for call_id, call in calls.items():
            if call_id not in completed or call.get("name") not in {"write_file", "replace_in_file"}:
                continue
            try:
                args = json.loads(call["arguments"])
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(args, dict) and isinstance(args.get("path"), str):
                latest[args["path"]] = call_id
        seen = {}
        for item in prepared:
            call_id = item.get("call_id")
            if call_id not in completed or call_id not in calls:
                continue  # Failed and unfinished transactions are never trimmed.
            call = calls[call_id]
            if (
                state.config.focused_context_enabled
                and item.get("type") == "function_call"
                and call.get("name") == "replace_in_file"
            ):
                args = json.loads(item["arguments"])
                for field in ("old_text", "new_text"):
                    if isinstance(args.get(field), str) and self.count(args[field]) > 256:
                        original = args[field]
                        args[field] = self.excerpt(original, 256)
                        args[field + "_source"] = {
                            "reference": "call:" + call_id,
                            "sha256": hashlib.sha256(original.encode()).hexdigest(),
                            "characters": len(original),
                        }
                item["arguments"] = json.dumps(args, ensure_ascii=False)
            if item.get("type") == "function_call" and call.get("name") == "write_file":
                try:
                    args = json.loads(item["arguments"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not isinstance(args, dict) or not isinstance(args.get("content"), str):
                    continue
                text = args["content"]
                stale = latest.get(args.get("path")) != call_id
                limit = 256 if stale else 1024
                if self.count(text) > limit:
                    args["content"] = {
                        "preview": self.excerpt(text, limit),
                        "characters": len(text),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "read_full": "call:" + call_id,
                        "superseded_by": "call:" + latest[args["path"]] if stale else None,
                        "note": "历史写入材料节选，不能据此认定当前文件内容；必要时读回原文或实际文件。",
                    }
                    item["arguments"] = json.dumps(args, ensure_ascii=False)
            elif item.get("type") == "function_call_output":
                result = completed[call_id].copy()
                text = result.get("output")
                if not isinstance(text, str):
                    continue
                # Preserve status, exit code, file changes and source evidence.
                signature = (call.get("name"), hashlib.sha256(text.encode()).hexdigest())
                if text and signature in seen and len(text) > 128:
                    result["output"] = "与 " + seen[signature] + " 的输出完全相同；完整原文保留。"
                    result["summary_source_ref"] = "call:" + call_id
                elif call.get("name") in {"read_file", "read_source", "read_tool_output", "recall"}:
                    limit = 384 if state.config.focused_context_enabled else 1024
                    if self.count(text) > limit:
                        result["output"] = self.excerpt(text, limit)
                        result["summary_source_ref"] = "call:" + call_id
                seen.setdefault(signature, "call:" + call_id)
                item["output"] = json.dumps(result, ensure_ascii=False)
        return prepared

    def work_messages(self, state: RunState, source_refs: dict | None = None) -> list[dict]:
        """A reversible model view; never edit the durable history or split call pairs."""
        if not state.config.focused_context_enabled:
            return state.messages
        messages = state.messages
        boundaries = [i for i, m in enumerate(messages) if m.get("_boundary")]
        cutoff = boundaries[-2] if len(boundaries) >= 2 else 0
        if state.config.global_efficiency_enabled:
            cutoff = boundaries[-1] if boundaries else 0
        calls = {m.get("call_id"): (i, m) for i, m in enumerate(messages) if m.get("type") == "function_call"}
        outputs = {}
        for i, m in enumerate(messages):
            if m.get("type") != "function_call_output":
                continue
            try:
                result = json.loads(m["output"])
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(result, dict) and result.get("ok") is True:
                outputs[m.get("call_id")] = (i, m, result)
        # Retain recent, exact file bodies, so repeated reads can refer to visible text.
        retained, seen, remaining = set(), set(), 6000
        body_limit = 3000 if state.config.global_efficiency_enabled else 1200
        from .files import inventory

        actual = inventory(state.root)
        for i in range(len(messages) - 1, -1, -1):
            marker = messages[i].get("_file_read")
            if not marker or marker["path"] in seen:
                continue
            seen.add(marker["path"])
            cost = self.count(messages[i])
            if actual.get(marker["path"]) == marker["sha256"] and cost <= min(body_limit, remaining):
                retained.add(i)
                remaining -= cost
        replacements, removed = {}, set()
        foldable = {
            "write_file",
            "replace_in_file",
            "read_file",
            "read_source",
            "recall",
            "read_tool_output",
            "run_command",
        }
        if state.config.global_efficiency_enabled:
            foldable.update({"check_stage", "revise_stage"})
        for call_id, (j, message, result) in outputs.items():
            if call_id not in calls:
                continue
            i, call = calls[call_id]
            if i >= cutoff or j >= cutoff or j in retained or call.get("name") not in foldable:
                continue
            try:
                args = json.loads(call.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            args = dict(args)
            for key in ("content", "old_text", "new_text"):
                if isinstance(args.get(key), str):
                    value = args[key]
                    args[key] = {
                        "preview": self.excerpt(value, 96),
                        "characters": len(value),
                        "sha256": hashlib.sha256(value.encode()).hexdigest(),
                    }
            record = {
                "reference": (source_refs or {}).get(call_id, "call:" + call_id),
                "tool": call["name"],
                "arguments": args,
                "ok": True,
                "exit_code": result.get("exit_code"),
                "evidence": result.get("evidence", {}),
                "changed_files": result.get("changed_files", []),
                "result_excerpt": self.excerpt(result.get("output", ""), 192),
            }
            replacements[j] = {
                "role": "developer",
                "content": "已完成工具过程的归档视图（不能重放；细节须按reference读回原文）："
                + json.dumps(record, ensure_ascii=False),
            }
            removed.update((i, j))
        if state.config.global_efficiency_enabled:
            # Opaque reasoning is protocol state, not task evidence. Keep the latest
            # and incomplete turns. Failure tools/results and visible decisions stay exact.
            starts = sorted({0, *boundaries, len(messages)})
            for left, right in zip(starts, starts[1:]):
                turn_calls = [m for m in messages[left:right] if m.get("type") == "function_call"]
                answered = {m.get("call_id") for m in messages if m.get("type") == "function_call_output"}
                if right > cutoff or not turn_calls or not all(m.get("call_id") in answered for m in turn_calls):
                    continue
                for i in range(left, right):
                    m = messages[i]
                    if m.get("type") == "reasoning":
                        visible = m.get("summary", [])
                        if visible:
                            replacements[i] = {
                                "role": "developer",
                                "content": "已结束步骤的可见推理摘要：" + json.dumps(visible, ensure_ascii=False),
                            }
                        removed.add(i)
        last_handoff = max((i for i, m in enumerate(messages) if m.get("_handoff")), default=-1)
        return [
            replacements[i] if i in replacements else m
            for i, m in enumerate(messages)
            if (i not in removed or i in replacements) and (not m.get("_handoff") or i == last_handoff)
        ]

    @staticmethod
    def recall_key(record: dict, goal_version: int) -> list:
        return [
            record.get("id"),
            record.get("blob"),
            record.get("goal_version"),
            record.get("superseded"),
            goal_version,
        ]

    def novel_recall(self, state: RunState, record: dict) -> tuple[dict | None, dict]:
        """Subtract exact visible source ranges, retaining every uncovered character."""
        key = self.recall_key(record, state.goal.version)
        start = record.get("offset", 0)
        text = record.get("text", "")
        end = start + len(text)
        gaps = [(start, end)]
        for message in self.work_messages(state):
            for visible in message.get("_recall_ranges", []):
                if visible.get("key") != key:
                    continue
                for left, right in visible.get("segments", [(visible["start"], visible["end"])]):
                    updated = []
                    for a, b in gaps:
                        if right <= a or left >= b:
                            updated.append((a, b))
                        else:
                            if a < left:
                                updated.append((a, left))
                            if right < b:
                                updated.append((right, b))
                    gaps = updated
        metadata = {"key": key, "start": start, "end": end, "segments": gaps}
        if not gaps:
            return None, metadata
        if gaps == [(start, end)]:
            return record, metadata
        clipped = dict(record)
        clipped["text"] = "\n".join(f"[原文字符 {a}:{b}]\n" + text[a - start : b - start] for a, b in gaps)
        clipped["visible_remainder_only"] = True
        clipped["note"] = "重叠原文仍在当前上下文中；这里仅补充尚未注入的部分。"
        return clipped, metadata

    @staticmethod
    def output_reserve(state: RunState) -> int:
        return min(4000, int(state.config.context_limit * 0.1))

    def excerpt(self, text: str, tokens: int) -> str:
        encoded = self.encoder.encode(text, disallowed_special=())
        if len(encoded) <= tokens:
            return text
        if tokens < 32:
            return self.trim(text, tokens)
        available = tokens - 24
        head = available // 2
        return (
            self.encoder.decode(encoded[:head])
            + "\n[…完整内容已保存，请按引用补读…]\n"
            + self.encoder.decode(encoded[-(available - head) :])
        )

    def result_preview(self, result: ToolResult, tokens: int) -> str:
        try:
            data = json.loads(result.output)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and data.get("role") and "summary" in data:
            checks = []
            for check in data.get("checks", []):
                item = check.get("result", check.get("verdict", {}))
                checks.append(
                    {
                        "command": self.trim(json.dumps(check.get("argv", check.get("kind", "check"))), 100),
                        "ok": item.get("ok", item.get("passed")),
                        "exit_code": item.get("exit_code"),
                    }
                )
            data = {
                "role": data["role"],
                "summary": self.excerpt(data["summary"], 1000),
                "files": data.get("files", []),
                "checks": checks,
                "issues": data.get("issues", []),
                "evidence_refs": data.get("evidence_refs", []),
                "patch": "补丁已合并或保存在完整工具结果中；不重复回传正文。",
            }
        elif isinstance(data, dict) and "stdout" in data and "stderr" in data:
            data = {
                **data,
                "stdout": self.excerpt(data["stdout"], tokens // 3),
                "stderr": self.excerpt(data["stderr"], tokens // 2),
            }
        text = json.dumps(data, ensure_ascii=False) if data is not None else result.output
        return self.excerpt(text, tokens)

    def pack_result(self, state: RunState, result: ToolResult, tool: str) -> str:
        original = result.model_dump_json()
        if self.count(original) <= self.tool_result_tokens:
            return original
        reference = self.store.save_tool_output(state.id, original, tool)
        body = result.model_dump()
        body["evidence"] = {
            "tool_output_ref": reference,
            "original_tokens": self.count(original),
            "truncated": True,
            "read_with": "read_tool_output(reference, offset=0)",
        }
        # Preserve status and file metadata; remove only the text shown to the model.
        body["output"] = ""
        if self.count(body) >= self.tool_result_tokens:
            body["changed_files"] = result.changed_files[:20]
            body["evidence"]["changed_files_truncated"] = True
        remaining = max(0, self.tool_result_tokens - self.count(body) - 64)
        body["output"] = self.result_preview(result, remaining)
        packed = json.dumps(body, ensure_ascii=False)
        while self.count(packed) > self.tool_result_tokens and remaining:
            remaining = max(0, remaining - (self.count(packed) - self.tool_result_tokens) - 32)
            body["output"] = self.result_preview(result, remaining)
            packed = json.dumps(body, ensure_ascii=False)
        if self.count(packed) > self.tool_result_tokens:
            raise ContextLimit("工具结果的必要元数据超过返回预算，完整结果已经保存。")
        self.store.event(
            state.id,
            "tool_output_bounded",
            {
                "tool": tool,
                "blob": reference,
                "original_tokens": self.count(original),
                "returned_tokens": self.count(packed),
            },
        )
        return packed

    def read_tool_output(self, state: RunState, reference: str, offset: int = 0) -> ToolResult:
        raw = self.store.read_tool_output(state.id, reference)
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(raw):
            raise ValueError("offset 必须是完整工具结果中的有效字符位置。")

        # Use character offsets to avoid cutting a multibyte character at a token boundary.
        def portion(end: int) -> ToolResult:
            return ToolResult(
                ok=True,
                output=json.dumps(
                    {
                        "reference": reference,
                        "offset": offset,
                        "next_offset": end,
                        "done": end == len(raw),
                        "text": raw[offset:end],
                    },
                    ensure_ascii=False,
                ),
            )

        low, high = offset, len(raw)
        while low < high:
            end = (low + high + 1) // 2
            if self.count(portion(end).model_dump_json()) <= self.tool_result_tokens:
                low = end
            else:
                high = end - 1
        return portion(low)

    def memory_text(self, state: RunState) -> str:
        if not state.config.memory_enabled or not state.memory_enabled:
            return ""
        records = [
            m
            for m in self.store.memories(state.id)
            if m["enabled"] and m["goal_version"] <= state.goal.version
        ]
        valid = []
        for record in records:
            if record["source"].startswith("file:"):
                if not fact_allowed(state.goal, record["text"], record["source"]):
                    continue
                path = (state.root / record["source"][5:]).resolve()
                if not path.is_relative_to(state.root) or not path.exists():
                    continue
                if record["kind"] == "project" and record["text"] not in path.read_text(errors="replace"):
                    continue
            valid.append({k: record[k] for k in ("kind", "text", "source", "timestamp")})
        return self.trim(json.dumps(valid, ensure_ascii=False), 2000)

    def build(self, state: RunState, instructions: str, tools: list[dict]) -> list[dict]:
        prefix = []
        if state.config.features.goal_anchor:
            prefix.append(
                {
                    "role": "developer",
                    "content": "常驻目标锚（用户目标，不得自行更改）：\n" + state.goal.model_dump_json(),
                }
            )
        if state.config.features.progress_skeleton:
            progress = [
                {"id": m.id, "description": m.description, "status": m.status, "depends_on": m.depends_on}
                for m in state.milestones
            ]
            if state.config.focused_context_enabled:
                for item, stage in zip(progress, state.milestones):
                    item["implementation_paths"] = stage.implementation_paths
                    item["test_coverage"] = stage.test_coverage
                progress = {
                    "stages": progress,
                    "active_stage": state.active_stage,
                    "next_action": "继续active_stage中的未完成要求；已通过且仍有效的工作不重新探索。",
                    "final_review_required": state.config.final_review_required,
                }
                if state.config.global_efficiency_enabled:
                    progress["closing_mode"] = state.closing_mode
                    progress["review_issues"] = state.review_evidence.get("issues", [])
                    progress["next_action"] = (
                        "先修复复核发现的实际缺陷，再验证受影响阶段；不要只添加失败测试或重新探索。"
                        if state.review_evidence.get("issues") and not state.review_evidence.get("passed")
                        else "完成当前阶段的实现和相关测试后立即申请检查；有效的已完成工作无需重复确认。"
                    )
            prefix.append(
                {
                    "role": "developer",
                    "content": "当前任务清单与已验证进展：\n" + json.dumps(progress, ensure_ascii=False),
                }
            )
        if memory := self.memory_text(state):
            prefix.append(
                {
                    "role": "developer",
                    "content": "带来源的项目记忆，使用前核对实际文件。用户目标与硬约束优先于偏好和自动事实：\n"
                    + memory,
                }
            )
        if state.summary:
            prefix.append(
                {
                    "role": "developer",
                    "content": "历史摘要（可能含已过时决策，请核对来源）：\n" + state.summary,
                }
            )
        assembled = [
            *prefix,
            *[{k: v for k, v in m.items() if not k.startswith("_")} for m in self.work_messages(state)
              if not (state.config.global_efficiency_enabled and state.config.features.goal_anchor
                      and m.get("role") == "user" and m.get("content") == state.goal.model_dump_json())],
        ]
        if self.count(assembled) + self.count(instructions) + self.count(tools) > state.config.context_limit:
            raise ContextLimit("工作上下文超过配置上限，需要压缩；不会静默删除目标锚。")
        return assembled

    def should_compact(self, state: RunState, instructions: str, tools: list[dict]) -> bool:
        try:
            assembled = self.build(state, instructions, tools)
        except ContextLimit:
            return True
        size = self.request_tokens(assembled, instructions, tools) + self.output_reserve(state)
        return size >= state.config.context_limit * state.config.compact_at

    def split(self, state: RunState, force: bool = False) -> tuple[list[dict], list[dict]]:
        budget = int(state.config.context_limit * state.config.recent_fraction)
        boundaries, pending = [0], set()
        for index, message in enumerate(state.messages):
            starts = message.get("_boundary") is True or (
                message.get("role") == "user" and message.get("_boundary") is not False
            )
            if starts and not pending and index:
                boundaries.append(index)
            if message.get("type") == "function_call":
                pending.add(message["call_id"])
            elif message.get("type") == "function_call_output":
                pending.discard(message["call_id"])
        if not pending:
            # A completed oversized final transaction can be archived whole, with no raw tail.
            boundaries.append(len(state.messages))
        candidates = [
            i for i in boundaries if (not force or i > 0) and self.count(state.messages[i:]) <= budget
        ]
        if not candidates:
            raise ContextLimit("未完成的完整工具调用过程超过近期原文预算；禁止拆散或盲目重放。")
        start = min(candidates)
        return state.messages[:start], state.messages[start:]

    def source_excerpt(
        self, state: RunState, record: dict, query: str = "", budget: int | None = None
    ) -> dict:
        text = record["text"]
        location = text.casefold().find(query.casefold()) if query else 0
        location = max(0, location)
        start = max(0, location - 256)
        item = {k: record[k] for k in ("id", "goal_version", "timestamp", "superseded", "blob")}
        item["offset"] = start
        item["query_found"] = bool(query) and query.casefold() in text.casefold()
        available = budget if budget is not None else state.config.recall_tokens
        text_budget = max(0, available - self.count(item) - 64)
        item["text"] = self.trim(text[start:], text_budget)
        item["next_offset"] = start + len(item["text"])
        while self.count(item) > available and text_budget > 0:
            text_budget = max(0, text_budget - (self.count(item) - available) - 16)
            item["text"] = self.trim(text[start:], text_budget)
            item["next_offset"] = start + len(item["text"])
        return item

    def read_source(self, state: RunState, reference: str, query: str = "") -> dict:
        record = self.store.source(state.id, reference)
        item = self.source_excerpt(state, record, query)
        self.store.event(
            state.id,
            "source_read",
            {
                "reference": reference,
                "offset": item["offset"],
                "query_found": item["query_found"],
                "tokens": self.count(item),
                "superseded": item["superseded"],
                "goal_version": item["goal_version"],
            },
        )
        return item

    def recall(self, state: RunState, query: str, include_superseded: bool = False) -> list[dict]:
        if not state.config.features.layered_context:
            return []
        output, remaining = [], state.config.recall_tokens
        for record in self.store.search(state.id, query, state.config.recall_top_k, include_superseded):
            item = self.source_excerpt(
                state, record, query, min(remaining, max(512, state.config.recall_tokens // 2))
            )
            cost = self.count(item)
            if cost > remaining or not item["text"]:
                break
            remaining -= cost
            output.append(item)
        self.store.event(
            state.id,
            "recall",
            {
                "query": query,
                "hits": [r["id"] for r in output],
                "offsets": [r["offset"] for r in output],
                "tokens": state.config.recall_tokens - remaining,
            },
        )
        return output
