from __future__ import annotations
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from .evaluation import source_manifest
from .grader import save_report
from .paths import PROJECT


def engineering_checks():
    results = []
    commands = [
        ("Python 静态检查", [str(PROJECT / ".venv/bin/ruff"), "check", "src", "tests", "scripts"], PROJECT),
        (
            "Python 格式检查",
            [str(PROJECT / ".venv/bin/ruff"), "format", "--check", "src", "tests", "scripts"],
            PROJECT,
        ),
        (
            "工程与真实沙箱测试",
            [sys.executable, "-m", "pytest", "-q", "--junitxml=outputs/engineering-junit.xml"],
            PROJECT,
        ),
        ("前端代码检查", ["/usr/local/bin/npm", "run", "lint"], PROJECT / "frontend"),
        ("前端类型检查与构建", ["/usr/local/bin/npm", "run", "build"], PROJECT / "frontend"),
    ]
    for name, argv, cwd in commands:
        start = time.monotonic()
        run = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, "PYTHONPATH": str(PROJECT / "src")},
            capture_output=True,
            text=True,
            timeout=180,
        )
        results.append(
            {
                "name": name,
                "passed": run.returncode == 0,
                "exit_code": run.returncode,
                "seconds": time.monotonic() - start,
                "output": (run.stdout + run.stderr)[-12000:],
            }
        )
    root = ET.parse(PROJECT / "outputs/engineering-junit.xml").getroot()
    tests = list(root.iter("testcase"))
    skipped = sum(t.find("skipped") is not None for t in tests)
    result = {
        "passed": all(r["passed"] for r in results) and skipped == 0,
        "tests": len(tests),
        "skipped": skipped,
        "real_model_calls": 0,
        "results": results,
        "source_files": source_manifest(),
    }
    save_report(PROJECT / "outputs/engineering-checks.json", result)
    return result
