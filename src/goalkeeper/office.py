from __future__ import annotations
import json
import os
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from .grader import save_report
from .paths import PROJECT

RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"


def office_demo():
    identifier = uuid4().hex[:10]
    build = PROJECT / "work" / ("f1-" + identifier)
    output = PROJECT / "outputs" / "f1" / identifier
    build.mkdir(parents=True)
    (build / "node_modules").symlink_to(RUNTIME / "node/node_modules", target_is_directory=True)
    shutil.copy2(PROJECT / "scripts/build_f1.mjs", build / "build.mjs")
    env = {
        **os.environ,
        "RUNTIME_NODE": str(RUNTIME / "node/bin/node"),
        "RUNTIME_NODE_MODULES": str(RUNTIME / "node/node_modules"),
        "RUNTIME_PYTHON": str(RUNTIME / "python/bin/python3"),
        "GK_PROJECT": str(PROJECT),
        "GK_F1_BUILD": str(build),
        "GK_F1_OUTPUT": str(output),
    }
    result = subprocess.run(
        [str(RUNTIME / "node/bin/node"), str(build / "build.mjs")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-6000:])
    metrics = json.loads((build / "metrics.json").read_text())
    files = [output / "销售分析.xlsx", output / "销售汇报-六页初版.pptx", output / "销售汇报-三页改版.pptx"]
    checks = []
    for file in files:
        with zipfile.ZipFile(file) as package:
            names = package.namelist()
            checks.append(
                {
                    "file": str(file),
                    "zip_ok": package.testzip() is None,
                    "native_charts": len([n for n in names if "/charts/chart" in n and n.endswith(".xml")]),
                    "has_formulas": any(
                        bool(
                            ET.fromstring(package.read(n)).findall(
                                ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f"
                            )
                        )
                        for n in names
                        if n.startswith("xl/worksheets/") and n.endswith(".xml")
                    )
                    if file.suffix == ".xlsx"
                    else None,
                }
            )
    value = {
        "passed": all(c["zip_ok"] and c["native_charts"] and c["has_formulas"] is not False for c in checks)
        and metrics["input_edit_verified"],
        "real_agent_run": False,
        "data": "synthetic",
        "checks": checks,
        "metrics": metrics,
        "build": str(build),
        "output": str(output),
        "visual_review": "pending",
        "target_application_edit_test": "not_run",
    }
    save_report(PROJECT / "outputs/f1-validation.json", value)
    return value
