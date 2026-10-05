"""Shared subscription transport must not change model/effort or expose server-side history."""

import pytest
from goalkeeper.external_worker import subscription_payload
from goalkeeper.models import MODEL, EFFORT


def body():
    return {
        "model": MODEL,
        "reasoning": {"effort": EFFORT},
        "input": [
            {"type": "message", "role": "system", "content": "native harness instructions"},
            {"type": "function_call", "call_id": "c", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c", "output": "file content"},
        ],
        "tools": [{"type": "function", "name": "read", "parameters": {"type": "object", "properties": {}}}],
        "max_output_tokens": 128000,
        "temperature": 1,
        "store": True,
        "stream": True,
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "gpt-6-astra"},
        {"reasoning": {"effort": "high"}},
        {"previous_response_id": "resp_other"},
        {"input": "server history"},
        {"service_tier": "priority"},
    ],
)
def test_transport_rejects_comparison_drift(changed):
    b = body()
    b.update(changed)
    with pytest.raises(ValueError):
        subscription_payload(b)


def test_transport_keeps_tool_transaction_and_native_content():
    b = body()
    result = subscription_payload(b)
    assert result["input"][0]["content"] == b["input"][0]["content"]
    assert result["input"][0]["role"] == "developer"
    assert result["input"][1:] == b["input"][1:]
    assert result["tools"] == b["tools"]
    assert result["reasoning"] == b["reasoning"]
    assert result["store"] is False and result["stream"] is True
    assert "max_output_tokens" not in result and "temperature" not in result
    assert b["store"] is True and b["input"][0]["role"] == "system"


def test_initial_case_copy_excludes_private_validation_artifacts(tmp_path):
    import runpy
    from goalkeeper.paths import PROJECT

    copy_initial_case = runpy.run_path(PROJECT / "scripts/four_harness/run_round.py")["copy_initial_case"]

    source = tmp_path / "private-initial"
    source.mkdir()
    (source / "main.py").write_text('print("task")')
    for name in [".tmp", ".home", "__pycache__", ".git", ".pytest_cache"]:
        (source / name).mkdir()
        (source / name / "test_independent.py").write_text("private scoring rule")
    target = tmp_path / "execution"
    copy_initial_case(source, target)
    assert sorted(p.name for p in target.iterdir()) == ["main.py"]
