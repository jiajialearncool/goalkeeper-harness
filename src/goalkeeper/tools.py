from __future__ import annotations

import json
import hashlib

from .files import changed, inventory
from .models import ToolResult
from .sandbox import Scope, Seatbelt
from .secrets import contains_secret


def function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": False,
    }


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
TOOLS = [
    function(
        "read_file",
        "读取任务范围内的文件。可按行读取；force=true可要求重新返回正文。",
        {
            "path": STRING,
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "force": {"type": "boolean"},
        },
        ["path"],
    ),
    function(
        "write_file", "写入任务范围内的完整文件。", {"path": STRING, "content": STRING}, ["path", "content"]
    ),
    function(
        "replace_in_file",
        "局部修改已有文件；old_text 必须非空且唯一匹配。可提供读取时的 sha256 拒绝过期修改。",
        {"path": STRING, "old_text": STRING, "new_text": STRING, "sha256": STRING},
        ["path", "old_text", "new_text"],
    ),
    function(
        "run_command",
        "以 argv 数组执行命令；没有公网、其他工作区或凭据权限。",
        {"argv": STRINGS, "timeout": {"type": "integer"}},
        ["argv"],
    ),
    function("start_stage", "开始阶段前核对相关原文。", {"milestone_id": STRING}, ["milestone_id"]),
    function(
        "read_source",
        "读取本次尝试的 archive: 或 call: 引用；核对原文和是否失效。",
        {"reference": STRING, "query": STRING, "force": {"type": "boolean"}},
        ["reference"],
    ),
    function(
        "recall",
        "检索已归档原文，引用来源并核对目标版本。",
        {"query": STRING, "force": {"type": "boolean"}},
        ["query"],
    ),
    function(
        "read_tool_output",
        "按引用分段读取本次尝试的完整工具结果；offset 是字符位置，返回 next_offset。不是历史回溯。",
        {"reference": STRING, "offset": {"type": "integer", "minimum": 0}},
        ["reference"],
    ),
    function(
        "plan",
        "提出阶段清单，不能改变用户目标。每阶段列出可运行的检查命令。",
        {
            "milestones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": STRING,
                        "requirement_ids": STRINGS,
                        "implementation_paths": STRINGS,
                        "checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"name": STRING, "argv": STRINGS},
                                "required": ["name", "argv"],
                            },
                        },
                    },
                    "required": ["description", "checks"],
                },
            }
        },
        ["milestones"],
    ),
    function(
        "revise_stage",
        "调整已有阶段的检查方法，说明原因；保留阶段及用户目标，旧证据失效，修复次数不重置。",
        {
            "milestone_id": STRING,
            "reason": STRING,
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": STRING, "argv": STRINGS},
                    "required": ["name", "argv"],
                },
            },
        },
        ["milestone_id", "reason", "checks"],
    ),
    function(
        "check_stage",
        "按阶段检查命令验证实际文件，成功后保存证据。",
        {"milestone_id": STRING},
        ["milestone_id"],
    ),
    function(
        "delegate",
        "一次性分配子任务。只读角色 Explorer/Planner/Reviewer；Worker 只能修改指定文件。禁止继续派生。",
        {
            "role": {"type": "string", "enum": ["Explorer", "Planner", "Worker", "Reviewer"]},
            "task": STRING,
            "paths": STRINGS,
        },
        ["role", "task", "paths"],
    ),
    function(
        "remember",
        "提取带实际文件来源的项目事实，不得保存目标、待办、验收标准或凭据。",
        {"text": STRING, "source": STRING},
        ["text", "source"],
    ),
    function("finish", "申请交付。程序必须执行最终检查。", {"summary": STRING}, ["summary"]),
]


class LocalTools:
    def __init__(self, scope: Scope):
        self.scope = scope
        self.sandbox = Seatbelt(scope)

    async def execute(self, name: str, args: dict, timeout: int = 120) -> ToolResult:
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是对象。")
        if name in {"read_file", "write_file", "replace_in_file"} and not isinstance(args.get("path"), str):
            raise ValueError("文件路径必须是字符串。")
        if name == "write_file" and not isinstance(args.get("content"), str):
            raise ValueError("文件内容必须是字符串。")
        if name == "replace_in_file" and (
            not isinstance(args.get("old_text"), str)
            or not args["old_text"]
            or not isinstance(args.get("new_text"), str)
        ):
            raise ValueError("局部修改需要非空 old_text 和字符串 new_text。")
        if name == "run_command" and (
            not isinstance(args.get("argv"), list)
            or not args["argv"]
            or any(not isinstance(a, str) for a in args["argv"])
        ):
            raise ValueError("命令需要包含至少一个字符串的 argv 数组。")
        before = inventory(self.scope.workspace)
        if name == "read_file":
            path = self.scope.path(args["path"])
            if path.stat().st_size > 2_000_000:
                raise ValueError("文件过大，请用有范围的命令读取。")
            text = path.read_text(errors="replace")
            start, end = args.get("start_line", 1), args.get("end_line")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or start < 1
                or end is not None
                and (not isinstance(end, int) or isinstance(end, bool) or end < start)
            ):
                raise ValueError("行范围无效；start_line须为正整数，end_line不得早于start_line。")
            lines = text.splitlines(keepends=True)
            left = sum(map(len, lines[: start - 1]))
            right = sum(map(len, lines[:end])) if end is not None else len(text)
            body = text[left:right][:60_000]
            result = ToolResult(
                ok=True,
                output=body,
                evidence={
                    "path": str(path.relative_to(self.scope.workspace)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "start": left,
                    "end": left + len(body),
                    "file_characters": len(text),
                },
            )
        elif name == "write_file":
            path = self.scope.path(args["path"], write=True)
            if contains_secret(args["content"]):
                raise ValueError("拒绝向任务文件写入疑似凭据。")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            result = ToolResult(ok=True, output="文件已保存。", evidence={"path": args["path"]})
        elif name == "replace_in_file":
            path = self.scope.path(args["path"], write=True)
            raw = path.read_bytes()
            current_hash = hashlib.sha256(raw).hexdigest()
            if args.get("sha256") and args["sha256"] != current_hash:
                raise ValueError("文件已改变，拒绝过期修改；请重新读取。")
            text = raw.decode("utf-8")
            if text.count(args["old_text"]) != 1:
                raise ValueError("old_text 必须唯一匹配；文件未修改，请重新定位。")
            replacement = text.replace(args["old_text"], args["new_text"], 1)
            if contains_secret(replacement):
                raise ValueError("拒绝向任务文件写入疑似凭据。")
            path.write_bytes(replacement.encode("utf-8"))
            result = ToolResult(
                ok=True,
                output="已应用唯一匹配的局部修改。",
                evidence={
                    "path": args["path"],
                    "before_sha256": current_hash,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            )
        elif name == "run_command":
            data = await self.sandbox.run(args["argv"], min(timeout, max(1, args.get("timeout", timeout))))
            result = ToolResult(
                ok=data["exit_code"] == 0 and not data["timed_out"],
                output=json.dumps(data, ensure_ascii=False),
                exit_code=data["exit_code"],
            )
        else:
            raise ValueError("未知本地工具。")
        result.changed_files = changed(before, inventory(self.scope.workspace))
        return result
