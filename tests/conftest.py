import os
import sys
import json
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(root / "work/tokenizer-cache"))


@pytest.fixture
def summary_provider():
    from goalkeeper.provider import ScriptedProvider

    class SummaryProvider(ScriptedProvider):
        def __init__(self, text):
            super().__init__([])
            self.text = text

        async def stream(self, request):
            data = json.loads(request.input[0]["content"])
            text = self.text
            if data.get("summary_format") == "structured_v1":
                text = json.dumps(
                    {
                        "goal_version": data["goal_version"],
                        "decisions": [self.text],
                        "unresolved": [],
                        "position": "历史已保存，尚需核对后续工作。",
                        "next_steps": ["读取来源并继续检查。"],
                        "files": [],
                        "checks": [],
                        "source_refs": ["archive:" + data["source"]],
                    },
                    ensure_ascii=False,
                )
            self.responses.append([{"type": "message", "content": [{"type": "output_text", "text": text}]}])
            async for event in super().stream(request):
                yield event

    return SummaryProvider
