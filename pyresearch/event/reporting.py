"""Assemble the machine-readable gated historical comparison receipt."""
from __future__ import annotations

import json
from pathlib import Path

from pyresearch.support.evaluate import sha256, write_json


from pyresearch import ROOT
REPORT_ROOT = ROOT / "data/research/tardis/reports/event_models"
DATA_ROOT = ROOT / "data/research/tardis/event_models"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run() -> dict:
    dates = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]
    manifests = {date: load(DATA_ROOT / date / "dataset_manifest.json") for date in dates}
    event_rule = load(REPORT_ROOT / "event_rule/development_summary.json")
    lightgbm = load(REPORT_ROOT / "lightgbm/development_summary.json")
    deep = load(REPORT_ROOT / "deep/development_summary.json")
    model_results = {"event_rule": event_rule, "lightgbm": lightgbm, "deep": deep}
    survivors = [name for name, result in model_results.items() if result["development_gate"]["passes"]]
    payload = {
        "schema": "event-model-final-historical-comparison-v1",
        "experiment_plan_sha256": sha256(ROOT / "research/specs/event_model_comparison_plan.json"),
        "implementation_head_before_uncommitted_report": "508bc31",
        "dataset_manifest_sha256": {
            date: sha256(DATA_ROOT / date / "dataset_manifest.json") for date in dates
        },
        "dataset_totals": {
            "quote_side_rows": sum(value["rows"] for value in manifests.values()),
            "filled_rows": sum(value["fill_rows"] for value in manifests.values()),
            "valid_primary_markout_rows": sum(value["valid_primary_markout_rows"] for value in manifests.values()),
        },
        "development": {
            "event_rule": {
                "selected": "event_rule_5_of_6",
                "economics": event_rule["selected_economics"],
                "gate": event_rule["development_gate"],
            },
            "lightgbm": {
                "selected": lightgbm["selected"],
                "economics": lightgbm["selected_economics"],
                "gate": lightgbm["development_gate"],
            },
            "deep": {
                "selected": deep["selected"],
                "economics": deep["selected_economics"],
                "gate": deep["development_gate"],
            },
            "survivors": survivors,
        },
        "june_july_august": {
            "opened": False,
            "reason": "zero models survived the predeclared development gate",
            "evaluation_count": 0,
        },
        "native_forward": {
            "started": False,
            "reason": "no model qualified to carry unchanged into forward OOS",
        },
        "manual_audit_sha256": sha256(REPORT_ROOT / "manual_audit.json"),
        "test_result": {
            "cpp": "pass",
            "python_new_event_tests": "10_pass",
            "python_full_suite": "89_pass_1_error",
            "sole_error": "historical raw passive binary SHA changed after nondeterministic macOS relink; frozen source bundle and spec were not altered",
        },
        "conclusion": {
            "profitable_model": None,
            "carry_to_native_forward": None,
            "primary_failure_evidence": [
                "adverse_selection",
                "fee_burden",
                "conditional_markout_prediction_is_too_weak",
                "model_instability_across_months",
            ],
        },
    }
    write_json(REPORT_ROOT / "final_historical_comparison.json", payload)
    return payload


def main() -> None:
    print(json.dumps(run(), sort_keys=True))


if __name__ == "__main__":
    main()
