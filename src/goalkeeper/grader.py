from __future__ import annotations

import json
import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .benchmark import Case
from .files import changed, digest, inventory
from .sandbox import Scope, Seatbelt, runtime_roots


class IndependentGrader:
    """Runs after execution processes stop, using an immutable snapshot and separate tests."""

    async def grade(self, case: Case, package: Path, artifact: Path, ports: list[int] | None = None) -> dict:
        initial = inventory(package / case.initial)
        files = inventory(artifact)
        unexpected = [
            f
            for f in changed(initial, files)
            if not any(f == p or (p.endswith("/") and f.startswith(p)) for p in case.editable)
        ]
        baseline = digest(artifact)
        scope = Scope(
            artifact,
            writable=False,
            runtimes=runtime_roots(),
            ports=case.ports if ports is None else ports,
        )
        sandbox = Seatbelt(scope)
        # Pytest walks collector ancestors. Keep private packages outside its sandbox;
        # copy only the trusted test into scratch after the execution Agent has stopped.
        hidden = sandbox.temp / "test_independent.py"
        hidden_bytes = (package / case.grader_file).read_bytes()
        hidden.chmod(0o600) if hidden.exists() else None
        hidden.write_bytes(hidden_bytes)
        hidden.chmod(0o400)
        scope.protected_files.append(hidden)
        report = sandbox.temp / "independent-junit.xml"
        config = sandbox.temp / "independent-pytest.ini"
        config.write_text("[pytest]\n")
        # Load pytest before inserting candidate imports. No candidate conftest, plugins or sitecustomize.
        program = (
            "import sys,pytest;sys.path.insert(0,sys.argv[1]);"
            'raise SystemExit(pytest.main([sys.argv[2],"-q","--noconftest",'
            '"-c",sys.argv[4],"--import-mode=importlib",'
            '"--override-ini=addopts=","--junitxml="+sys.argv[3]]))'
        )
        scope_env = sandbox.environment

        def environment():
            return {**scope_env(), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

        sandbox.environment = environment
        result = await sandbox.run(
            [
                sys.executable,
                "-I",
                "-c",
                program,
                str(artifact),
                str(hidden),
                str(report),
                str(config),
            ],
            timeout=180,
        )
        names, failed = [], []
        parse_error = None
        try:
            root = ET.parse(report).getroot()
            for test in root.iter("testcase"):
                names.append(test.attrib.get("name", ""))
                if any(test.find(tag) is not None for tag in ["failure", "error", "skipped"]):
                    failed.append(test.attrib.get("name", ""))
        except (OSError, ET.ParseError) as error:
            parse_error = type(error).__name__
        expected = sorted(case.expected_tests)
        complete = sorted(names) == expected and len(names) == len(set(names))
        passed = (
            result["exit_code"] == 0
            and not result["timed_out"]
            and complete
            and not failed
            and not unexpected
            and baseline == digest(artifact)
            and hidden.read_bytes() == hidden_bytes
        )
        return {
            "case_id": case.id,
            "passed": passed,
            "snapshot": baseline,
            "tests_seen": names,
            "tests_expected": expected,
            "failed_tests": failed,
            "unexpected_files": unexpected,
            "complete_test_collection": complete,
            "parse_error": parse_error,
            "grader_sha256": hashlib.sha256(hidden_bytes).hexdigest(),
            "infrastructure_error": result["exit_code"] in {3, 4, 5}
            or bool(parse_error and not result["timed_out"]),
            "process": result,
        }

    async def validate(self, case: Case, package: Path) -> dict:
        reference = await self.grade(case, package, package / case.reference)
        mutants = [await self.grade(case, package, package / m) for m in case.mutants]
        checks = []
        for path, expected in case.mutant_expectations.items():
            index = case.mutants.index(path)
            score = mutants[index]
            checks.append(
                {
                    "mutant": path,
                    "expected_rejections": expected,
                    "passed": not score["passed"] and set(expected).issubset(score["failed_tests"]),
                }
            )
        covered = {name for names in case.mutant_expectations.values() for name in names}
        coverage = (
            bool(checks) and set(case.expected_tests).issubset(covered) and all(c["passed"] for c in checks)
        )
        return {
            "case_id": case.id,
            "passed": reference["passed"] and bool(mutants) and all(not m["passed"] for m in mutants),
            "reference": reference,
            "mutants": mutants,
            "critical_constraint_coverage_complete": coverage,
            "constraint_checks": checks,
        }


def save_report(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)
