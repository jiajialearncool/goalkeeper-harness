"""Mechanical OOXML compatibility conversion for the unscored F1 demonstration."""

from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

AXIS_VALUE = re.compile(rb'(<(?:[\w.-]+:)?(?:axId|crossAx)\b[^>]*\bval=")(-\d+)(")')


def normalize_chart_identifiers(content: bytes) -> tuple[bytes, int]:
    changes = 0

    def unsigned(match):
        nonlocal changes
        value = int(match[2])
        if not -(2**31) <= value < 0:
            raise ValueError("图表轴编号超出可机械转换的 32 位范围。")
        changes += 1
        return match[1] + str(value + 2**32).encode() + match[3]

    corrected = AXIS_VALUE.sub(unsigned, content)
    for item in ET.fromstring(corrected).iter():
        if item.tag.rsplit("}", 1)[-1] in {"axId", "crossAx"}:
            if not 0 <= int(item.attrib["val"]) < 2**32:
                raise ValueError("图表轴编号不符合无符号 32 位格式。")
    return corrected, changes


def finalize_office_delivery(source: Path, destination: Path, names: list[str]) -> dict:
    if destination.exists():
        raise FileExistsError("交付副本已存在，禁止覆盖。")
    destination.mkdir(parents=True)
    files = []
    for name in names:
        original, target = source / name, destination / name
        modified_parts = {}
        if original.suffix == ".pptx":
            with zipfile.ZipFile(original) as package, zipfile.ZipFile(target, "w") as output:
                for info in package.infolist():
                    data = package.read(info.filename)
                    if info.filename.startswith("ppt/charts/chart") and info.filename.endswith(".xml"):
                        data, count = normalize_chart_identifiers(data)
                        if count:
                            modified_parts[info.filename] = count
                    output.writestr(info, data)
        else:
            shutil.copyfile(original, target)
        target.chmod(0o400)
        files.append(
            {
                "name": name,
                "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                "delivery_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "chart_axis_identifiers_normalized": modified_parts,
            }
        )
    return {
        "source": str(source),
        "output": str(destination),
        "files": files,
        "semantic_content_modified": False,
        "included_in_formal_score": False,
        "real_model_calls": 0,
    }
