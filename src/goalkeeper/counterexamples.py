from __future__ import annotations

from pathlib import Path

from .benchmark import Case
from .files import inventory


def add_constraint_mutants(package: Path, cases: list[Case]) -> None:
    """Concrete constraint violations, beyond syntax crashes and unchanged initial code."""

    def mutant(case, name, replacements, rejected):
        destination = f"{case.id}/mutants/{name}"
        source = package / case.reference
        root = package / destination
        for filename in inventory(source):
            target = root / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / filename).read_bytes())
        for filename, transform in replacements.items():
            target = root / filename
            target.write_text(transform(target.read_text()))
        case.mutants.append(destination)
        case.mutant_expectations[destination] = rejected

    for case in cases:
        if case.id == "A1":
            mutant(
                case,
                "changed_calculation",
                {"unit0.py": lambda x: x.replace("value*1,", "value*2,")},
                ["test_behavior"],
            )
            mutant(
                case, "left_print", {"unit0.py": lambda x: x + '\nprint("leftover log")\n'}, ["test_no_print"]
            )
            mutant(
                case,
                "ignores_log_switch",
                {"log_config.py": lambda x: x.replace("setLevel(level)", "setLevel(logging.ERROR)")},
                ["test_log_level_and_off_switch"],
            )
        elif case.id == "A2":
            initial = (package / case.initial / "Counter0.jsx").read_text()
            mutant(case, "left_class", {"Counter0.jsx": lambda x: initial}, ["test_no_class_components"])
            mutant(
                case,
                "double_increment",
                {"Counter0.jsx": lambda x: x.replace("c=>c+1", "c=>c+2")},
                ["test_hooks_and_state_behavior"],
            )
        elif case.id == "A3":
            initial = (package / case.initial / "flow0.js").read_text()
            mutant(case, "left_then_chain", {"flow0.js": lambda x: initial}, ["test_no_promise_chains"])
            mutant(
                case,
                "masked_error",
                {"flow0.js": lambda x: x.replace("error:e.message", 'error:"masked"')},
                ["test_success_and_failure"],
            )
        elif case.id == "B1":
            mutant(
                case,
                "wrong_environment",
                {"src/main.js": lambda x: x.replace("import.meta.env.VITE_APP_LABEL", '"Wrong shop"')},
                ["test_build_and_environment"],
            )
            mutant(
                case,
                "left_webpack_script",
                {"package.json": lambda x: x.replace("vite build", "webpack")},
                ["test_vite_configuration"],
            )
            mutant(
                case,
                "wrong_base",
                {"vite.config.js": lambda x: x.replace("/shop/", "/wrong/")},
                ["test_dev_page_and_asset"],
            )
            mutant(
                case,
                "wrong_static_asset",
                {"public/logo.txt": lambda x: "missing business asset\n"},
                ["test_build_and_environment"],
            )
        elif case.id == "B2":
            mutant(
                case,
                "untrimmed_name",
                {"schema.py": lambda x: x.replace("v.strip()", "v")},
                ["test_validation_and_serialization"],
            )
            initial = (package / case.initial / "schema.py").read_text()
            mutant(
                case, "left_deprecated_apis", {"schema.py": lambda x: initial}, ["test_no_deprecated_apis"]
            )
            mutant(
                case,
                "old_dependency",
                {"requirements.txt": lambda x: "pydantic==1.10.15\n"},
                ["test_dependency_upgrade"],
            )
        elif case.id == "B3":
            mutant(
                case,
                "lexical_version_compare",
                {"compat.py": lambda x: x.replace("parts(a)>parts(b)", "a>b")},
                ["test_python312_behavior"],
            )
            mutant(
                case,
                "removed_stdlib",
                {"compat.py": lambda x: x + "\nfrom imp import new_module\n"},
                ["test_removed_imports"],
            )
        elif case.id == "C1":
            initial = (package / case.initial / "ledger.py").read_text()
            mutant(
                case,
                "unlocked_read_modify_write",
                {"ledger.py": lambda x: initial},
                ["test_fifty_concurrent_rounds"],
            )
            mutant(
                case,
                "changed_empty_batch",
                {"service.py": lambda x: x.replace("return ledger.total", "return ledger.total+1")},
                ["test_original_behavior"],
            )
            mutant(
                case,
                "missing_root_cause",
                {"ROOT_CAUSE.md": lambda x: ""},
                ["test_regression_and_root_cause"],
            )
            mutant(
                case,
                "no_pressure_regression",
                {"tests/test_regression.py": lambda x: "def test_nothing():pass\n"},
                ["test_regression_and_root_cause"],
            )
        elif case.id == "C2":
            initial = (package / case.initial / "render.py").read_text()
            mutant(case, "shared_mutable_state", {"render.py": lambda x: initial}, ["test_twenty_orders"])
            mutant(
                case,
                "missing_root_cause",
                {"ROOT_CAUSE.md": lambda x: ""},
                ["test_root_cause_and_regression"],
            )
            mutant(
                case,
                "missing_repetition",
                {"tests/test_regression.py": lambda x: "def test_nothing():pass\n"},
                ["test_root_cause_and_regression"],
            )
        elif case.id == "D1":
            mutant(
                case,
                "binary_float_amount",
                {"cart.py": lambda x: x.replace("Decimal(str(p))*q", "float(p)*q")},
                ["test_cart_original_contract"],
            )
            mutant(
                case,
                "wrong_threshold",
                {"cart.py": lambda x: x.replace("(value//100)*10", "(value//101)*10")},
                ["test_discount_boundaries"],
            )
            mutant(
                case,
                "ignored_coupon",
                {"cart.py": lambda x: x.replace("-Decimal(str(coupon))", "")},
                ["test_discount_boundaries"],
            )
            mutant(
                case,
                "negative_final_total",
                {
                    "cart.py": lambda x: x.replace(
                        "max(Decimal(0),value-reduction-Decimal(str(coupon)))",
                        "value-reduction-Decimal(str(coupon))",
                    )
                },
                ["test_discount_boundaries"],
            )
        elif case.id == "D2":
            mutant(
                case,
                "wrong_legacy_conversion",
                {"storage.py": lambda x: x.replace("*100", "*10")},
                ["test_old_data_migration"],
            )
            mutant(
                case,
                "wrong_display_units",
                {"view.py": lambda x: x.replace("/100", "/10")},
                ["test_new_schema_consistency"],
            )
            mutant(
                case,
                "old_api_shape",
                {
                    "api.py": lambda x: (
                        'from storage import load\ndef response(row):\n    value=load(row);return {"id":value["id"],"amount":value["amount_cents"]/100}\n'
                    )
                },
                ["test_new_schema_consistency"],
            )
        elif case.id in {"E1", "E2"}:
            mutant(
                case,
                "no_category_trim",
                {"parse.py": lambda x: x.replace("category.strip()", "category")},
                ["test_parse_contract"],
            )
            mutant(
                case,
                "overwrites_category_total",
                {"aggregate.py": lambda x: x.replace("result.get(key,0)+value", "value")},
                ["test_aggregate_and_export"],
            )
            mutant(
                case,
                "unsorted_export",
                {"export.py": lambda x: x.replace("sort_keys=True", "sort_keys=False")},
                ["test_aggregate_and_export"],
            )
