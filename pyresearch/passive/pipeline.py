"""Leakage-gated passive-fill and adverse-selection research pipeline.

The alpha model, feature transforms, queue rules, placement rules, and split are
loaded from pre-existing frozen artifacts or the pre-result maker draft.  The
only development-derived quote-filter values are two signal-distribution
quantiles; they never inspect maker outcomes.  Validation and historical
holdout commands refuse to run until the maker methodology is frozen.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.execution.engine import frozen_prediction
from pyresearch.passive.analysis import analyze_stage
from pyresearch.passive.labeling import label_probe_file
from pyresearch.passive.manual_audit import build_manual_audit
from pyresearch.passive.placements import build_placements
from pyresearch.support.evaluate import sha256, write_json


from pyresearch import ROOT
DRAFT_SPEC = ROOT / "research/specs/maker_research_spec_draft.json"
FROZEN_SPEC = ROOT / "research/specs/maker_research_spec_frozen.json"
FROZEN_SPEC_HASH = ROOT / "research/specs/maker_research_spec_frozen.json.sha256"
FEATURE_ROOT = ROOT / "data/research/tardis"
PASSIVE_ROOT = FEATURE_ROOT / "passive"
REPORT_ROOT = FEATURE_ROOT / "reports/passive"
L2_MANIFEST = ROOT / "data/historical/tardis/reports/2026-first-days/manifest.json"
TRADES_MANIFEST = (
    ROOT / "data/historical/tardis/binance-futures/trades/trades_manifest.json"
)
ALPHA_SPEC = ROOT / "research/specs/research_spec_frozen.json"
EXECUTION_SPEC = ROOT / "research/specs/execution_spec_frozen.json"
ALPHA_MODELS = FEATURE_ROOT / "reports/development/fitted_models.json"
ALPHA_TRANSFORMS = FEATURE_ROOT / "reports/development/development_transforms.json"
THRESHOLDS_REPORT = REPORT_ROOT / "development_signal_thresholds.json"
PASSIVE_BINARY = ROOT / "build/cpp/tardis_passive_probe"

FROZEN_SOURCE_FILES = (
    (ROOT / "cpp/app/tardis_passive_main.cpp", "cpp/app/tardis_passive_main.cpp"),
    (ROOT / "cpp/research/passive_queue.cpp", "cpp/research/passive_queue.cpp"),
    (ROOT / "cpp/research/passive_queue.hpp", "cpp/research/passive_queue.hpp"),
    (ROOT / "research/archive/frozen_source_bundles/passive_research/analysis.py", "passive_research/analysis.py"),
    (ROOT / "research/archive/frozen_source_bundles/passive_research/labeling.py", "passive_research/labeling.py"),
    (ROOT / "research/archive/frozen_source_bundles/passive_research/placements.py", "passive_research/placements.py"),
    (ROOT / "research/archive/frozen_source_bundles/passive_research/pipeline.py", "passive_research/pipeline.py"),
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def _source_bundle_hash() -> str:
    digest = hashlib.sha256()
    for path, frozen_label in sorted(FROZEN_SOURCE_FILES, key=lambda item: item[1]):
        digest.update((frozen_label + "\n").encode())
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest_map(path: Path) -> dict[str, dict[str, Any]]:
    return {entry["date"]: entry for entry in _load_json(path)["entries"]}


def _raw_path(entry: dict[str, Any]) -> Path:
    path = ROOT / entry["path"]
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _audit_inputs(spec: dict[str, Any], *, verify_raw_dates: list[str] | None = None) -> dict[str, Any]:
    paths = {
        "alpha_spec_sha256": ALPHA_SPEC,
        "execution_spec_sha256": EXECUTION_SPEC,
        "l2_manifest_sha256": L2_MANIFEST,
        "trades_manifest_sha256": TRADES_MANIFEST,
    }
    observed = {key: sha256(path) for key, path in paths.items()}
    for key, value in observed.items():
        expected = spec["audit"][key]
        if value != expected:
            raise ValueError(f"maker audit hash mismatch for {key}: {value} != {expected}")
    bundle = hashlib.sha256(
        (observed["l2_manifest_sha256"] + "\n" + observed["trades_manifest_sha256"] + "\n").encode()
    ).hexdigest()
    if bundle != spec["audit"]["dataset_bundle_sha256"]:
        raise ValueError("maker dataset bundle hash mismatch")
    observed["dataset_bundle_sha256"] = bundle
    observed["alpha_models_sha256"] = sha256(ALPHA_MODELS)
    observed["alpha_transforms_sha256"] = sha256(ALPHA_TRANSFORMS)
    observed["maker_source_bundle_sha256"] = _source_bundle_hash()
    observed["passive_binary_sha256"] = sha256(PASSIVE_BINARY) if PASSIVE_BINARY.exists() else None

    if verify_raw_dates:
        l2_entries = _manifest_map(L2_MANIFEST)
        trade_entries = _manifest_map(TRADES_MANIFEST)
        raw: dict[str, Any] = {}
        for date in verify_raw_dates:
            if date not in l2_entries or date not in trade_entries:
                raise ValueError(f"date absent from frozen raw manifests: {date}")
            l2_path = _raw_path(l2_entries[date])
            trade_path = _raw_path(trade_entries[date])
            l2_hash = sha256(l2_path)
            trade_hash = sha256(trade_path)
            if l2_hash != l2_entries[date]["sha256"]:
                raise ValueError(f"raw L2 hash mismatch for {date}")
            if trade_hash != trade_entries[date]["sha256"]:
                raise ValueError(f"raw trade hash mismatch for {date}")
            raw[date] = {
                "l2_path": _relative(l2_path),
                "l2_sha256": l2_hash,
                "trades_path": _relative(trade_path),
                "trades_sha256": trade_hash,
            }
        observed["verified_raw_inputs"] = raw
    return observed


def _validate_frozen(*, verify_binary: bool = True) -> dict[str, Any]:
    """Validate frozen inputs; an exact historical binary receipt is optional."""
    if not FROZEN_SPEC.exists() or not FROZEN_SPEC_HASH.exists():
        raise FileNotFoundError("validation/holdout requires the frozen maker methodology")
    spec = _load_json(FROZEN_SPEC)
    if spec.get("status") != "frozen_after_development":
        raise ValueError("maker methodology does not have frozen_after_development status")
    if FROZEN_SPEC_HASH.read_text(encoding="utf-8").strip() != sha256(FROZEN_SPEC):
        raise ValueError("frozen maker methodology hash sidecar mismatch")
    audit = _audit_inputs(spec)
    required = (
        "alpha_models_sha256",
        "alpha_transforms_sha256",
        "maker_source_bundle_sha256",
    )
    if verify_binary:
        required += ("passive_binary_sha256",)
    for key in required:
        if audit[key] != spec["audit"][key]:
            raise ValueError(f"post-freeze maker input changed: {key}")
    return spec


def compute_signal_thresholds() -> dict[str, Any]:
    """Fit only predeclared development signal quantiles, never maker outcomes."""
    draft = _load_json(DRAFT_SPEC)
    _audit_inputs(draft)
    dates = list(draft["quote_filter"]["threshold_fit_dates"])
    if dates != list(draft["split"]["development"]):
        raise ValueError("quote-filter fit dates do not equal the declared development split")
    model = _load_json(ALPHA_MODELS)["models"][draft["alpha"]["frozen_model"]]
    transforms = _load_json(ALPHA_TRANSFORMS)
    values: list[np.ndarray] = []
    feature_hashes: dict[str, str] = {}
    for date in dates:
        path = FEATURE_ROOT / date / "features_100ms.parquet"
        frame = pd.read_parquet(path, columns=list(model["features"]))
        prediction = frozen_prediction(frame, model, transforms)
        values.append(prediction[np.isfinite(prediction)])
        feature_hashes[date] = sha256(path)
    combined = np.concatenate(values)
    bearish_q = float(draft["quote_filter"]["bearish_quantile"])
    bullish_q = float(draft["quote_filter"]["bullish_quantile"])
    thresholds = np.quantile(combined, [bearish_q, bullish_q], method="linear")
    payload = {
        "schema": "passive-development-signal-thresholds-v1",
        "created_before_development_maker_analysis": True,
        "maker_outcomes_read": False,
        "signal": draft["quote_filter"]["signal"],
        "dates": dates,
        "finite_prediction_rows": int(len(combined)),
        "bearish_quantile": bearish_q,
        "bearish_threshold_ticks": float(thresholds[0]),
        "bullish_quantile": bullish_q,
        "bullish_threshold_ticks": float(thresholds[1]),
        "alpha_models_sha256": sha256(ALPHA_MODELS),
        "alpha_transforms_sha256": sha256(ALPHA_TRANSFORMS),
        "feature_sha256": feature_hashes,
    }
    write_json(THRESHOLDS_REPORT, payload)
    return payload


def _day_paths(date: str) -> dict[str, Path]:
    root = PASSIVE_ROOT / date
    return {
        "root": root,
        "placements": root / "placements.csv.gz",
        "placements_report": root / "placements_report.json",
        "probes": root / "probes.csv.zst",
        "probes_report": root / "probes_report.json",
        "labeled": root / "labeled_probes.parquet",
        "label_report": root / "label_report.json",
        "manifest": root / "day_manifest.json",
    }


def _reusable_day(date: str, minimum_probe_repeat: int, minimum_label_repeat: int) -> bool:
    paths = _day_paths(date)
    if not paths["manifest"].exists():
        return False
    manifest = _load_json(paths["manifest"])
    try:
        if manifest["date"] != date:
            return False
        if int(manifest["probe_repeat_count"]) < minimum_probe_repeat:
            return False
        if int(manifest["label_repeat_count"]) < minimum_label_repeat:
            return False
        for key in ("placements", "probes", "labeled"):
            if not paths[key].exists() or sha256(paths[key]) != manifest[f"{key}_sha256"]:
                return False
    except (KeyError, OSError, ValueError):
        return False
    return True


def _run_probe(
    date: str,
    paths: dict[str, Path],
    trades_path: Path,
    quote_qty: float,
    repeat: int,
) -> list[str]:
    if not PASSIVE_BINARY.exists():
        raise FileNotFoundError(
            f"missing {PASSIVE_BINARY}; configure and build cpp before passive research"
        )
    command = [
        str(PASSIVE_BINARY),
        "--date", date,
        "--placements", str(paths["placements"]),
        "--trades", str(trades_path),
        "--output", str(paths["probes"]),
        "--report", str(paths["probes_report"]),
        "--quote-qty", format(quote_qty, ".12g"),
        "--repeat", str(repeat),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    return command


def _label_deterministically(
    date: str,
    paths: dict[str, Path],
    repeat: int,
) -> dict[str, Any]:
    first = label_probe_file(
        date,
        paths["probes"],
        paths["labeled"],
        paths["label_report"],
    )
    content_identical = True
    byte_identical = True
    if repeat > 1:
        repeated_output = paths["labeled"].with_suffix(".repeat.parquet")
        repeated_report = paths["label_report"].with_suffix(".repeat.json")
        second = label_probe_file(
            date,
            paths["probes"],
            repeated_output,
            repeated_report,
        )
        content_identical = first["content_checksum"] == second["content_checksum"]
        byte_identical = first["output_sha256"] == second["output_sha256"]
        repeated_output.unlink()
        repeated_report.unlink()
    first["repeat_count"] = repeat
    first["deterministic_content"] = content_identical
    first["byte_identical_output"] = byte_identical
    write_json(paths["label_report"], first)
    if not content_identical or not byte_identical:
        raise RuntimeError("passive markout labeling was not byte deterministic")
    return first


def build_day(
    date: str,
    *,
    probe_repeat: int = 1,
    label_repeat: int = 1,
    reuse: bool = True,
) -> dict[str, Any]:
    if reuse and _reusable_day(date, probe_repeat, label_repeat):
        return _load_json(_day_paths(date)["manifest"])
    spec = _load_json(FROZEN_SPEC if FROZEN_SPEC.exists() else DRAFT_SPEC)
    audit = _audit_inputs(spec, verify_raw_dates=[date])
    l2_entry = _manifest_map(L2_MANIFEST)[date]
    trades_entry = _manifest_map(TRADES_MANIFEST)[date]
    l2_path = _raw_path(l2_entry)
    trades_path = _raw_path(trades_entry)
    paths = _day_paths(date)
    paths["root"].mkdir(parents=True, exist_ok=True)

    build_placements(
        date,
        paths["placements"],
        paths["placements_report"],
        repeat=max(2, probe_repeat),
    )
    command = _run_probe(
        date,
        paths,
        trades_path,
        float(spec["placement"]["quote_quantity_btc"]),
        probe_repeat,
    )
    label = _label_deterministically(date, paths, label_repeat)
    probe = _load_json(paths["probes_report"])
    placement = _load_json(paths["placements_report"])
    manifest = {
        "schema": "passive-research-day-manifest-v1",
        "date": date,
        "created_at_utc": _utc_now(),
        "raw_inputs": audit["verified_raw_inputs"][date],
        "l2_validation_report": l2_entry["report_path"],
        "l2_validation_report_sha256": sha256(ROOT / l2_entry["report_path"]),
        "placements": _relative(paths["placements"]),
        "placements_sha256": sha256(paths["placements"]),
        "probes": _relative(paths["probes"]),
        "probes_sha256": sha256(paths["probes"]),
        "labeled": _relative(paths["labeled"]),
        "labeled_sha256": sha256(paths["labeled"]),
        "placement_report": placement,
        "probe_report": probe,
        "label_report": label,
        "probe_repeat_count": probe_repeat,
        "label_repeat_count": label_repeat,
        "probe_command": " ".join(shlex.quote(part) for part in command),
        "maker_source_bundle_sha256": _source_bundle_hash(),
        "passive_binary_sha256": sha256(PASSIVE_BINARY),
    }
    write_json(paths["manifest"], manifest)
    return manifest


def _threshold_values() -> tuple[float, float]:
    report = _load_json(THRESHOLDS_REPORT) if THRESHOLDS_REPORT.exists() else compute_signal_thresholds()
    return float(report["bearish_threshold_ticks"]), float(report["bullish_threshold_ticks"])


def _analyze(stage: str, dates: list[str]) -> dict[str, Any]:
    bearish, bullish = _threshold_values()
    spec = _load_json(FROZEN_SPEC if FROZEN_SPEC.exists() else DRAFT_SPEC)
    return analyze_stage(
        stage,
        dates,
        [_day_paths(date)["labeled"] for date in dates],
        REPORT_ROOT / stage,
        bearish_threshold=bearish,
        bullish_threshold=bullish,
        maker_fee_scenarios=[float(value) for value in spec["fees"]["maker_fee_scenarios_bps"]],
    )


def run_stage_a() -> dict[str, Any]:
    draft = _load_json(DRAFT_SPEC)
    if draft.get("status") != "draft_before_maker_results":
        raise ValueError("Stage A requires the pre-result maker draft")
    thresholds = compute_signal_thresholds()
    date = draft["split"]["stage_a_single_development_day"][0]
    manifest = build_day(date, probe_repeat=2, label_repeat=2, reuse=True)
    l2_entry = _manifest_map(L2_MANIFEST)[date]
    trades_entry = _manifest_map(TRADES_MANIFEST)[date]
    audit_dir = REPORT_ROOT / "stage_a_may"
    manual = build_manual_audit(
        _day_paths(date)["labeled"],
        _raw_path(trades_entry),
        _raw_path(l2_entry),
        audit_dir / "manual_fill_audit.json",
        audit_dir / "manual_fill_audit_samples.csv",
        per_category=int(draft["manual_audit"]["minimum_examples_per_available_category"]),
    )
    if not manual["all_fill_fields_match"]:
        raise RuntimeError("Stage A manual fill audit failed")
    analysis = _analyze("stage_a_may", [date])
    result = {
        "schema": "passive-stage-a-completion-v1",
        "date": date,
        "thresholds": thresholds,
        "day_manifest_sha256": sha256(_day_paths(date)["manifest"]),
        "manual_audit": {
            "sample_count": manual["sample_count"],
            "consistent_examples": manual["consistent_examples"],
            "all_fill_fields_match": manual["all_fill_fields_match"],
        },
        "analysis": analysis,
    }
    write_json(audit_dir / "stage_a_summary.json", result)
    return result


def _freeze_after_development(dates: list[str]) -> dict[str, Any]:
    if FROZEN_SPEC.exists():
        return _validate_frozen()
    draft = _load_json(DRAFT_SPEC)
    thresholds = _load_json(THRESHOLDS_REPORT)
    development_dir = REPORT_ROOT / "development"
    required_artifacts = (
        development_dir / "run_summary.json",
        development_dir / "day_summary.csv",
        development_dir / "fill_probability_by_day_and_obi.csv",
        development_dir / "maker_markout_by_day_and_obi.csv",
        development_dir / "queue_model_sensitivity_by_day.csv",
        development_dir / "quote_filter_by_day.csv",
        development_dir / "maker_fee_envelope_by_day.csv",
    )
    missing = [str(path) for path in required_artifacts if not path.exists()]
    if missing:
        raise FileNotFoundError(f"cannot freeze before development analysis: {missing}")
    audit = _audit_inputs(draft, verify_raw_dates=dates)
    frozen = json.loads(json.dumps(draft))
    frozen["status"] = "frozen_after_development"
    frozen["frozen_at_utc"] = _utc_now()
    frozen["audit"].update({
        key: audit[key]
        for key in (
            "alpha_models_sha256",
            "alpha_transforms_sha256",
            "maker_source_bundle_sha256",
            "passive_binary_sha256",
        )
    })
    frozen["quote_filter"]["numeric_thresholds"] = {
        "bearish_20pct_ticks": thresholds["bearish_threshold_ticks"],
        "bullish_80pct_ticks": thresholds["bullish_threshold_ticks"],
        "artifact": _relative(THRESHOLDS_REPORT),
        "artifact_sha256": sha256(THRESHOLDS_REPORT),
        "maker_outcomes_used": False,
    }
    frozen["development_maker_analysis"] = {
        "dates": dates,
        "parameter_selected_from_maker_outcomes": False,
        "artifacts": {
            _relative(path): sha256(path) for path in required_artifacts
        },
        "day_manifests": {
            date: sha256(_day_paths(date)["manifest"]) for date in dates
        },
    }
    write_json(FROZEN_SPEC, frozen)
    FROZEN_SPEC_HASH.write_text(sha256(FROZEN_SPEC) + "\n", encoding="utf-8")
    return frozen


def run_development() -> dict[str, Any]:
    draft = _load_json(DRAFT_SPEC)
    if draft.get("status") != "draft_before_maker_results":
        raise ValueError("development requires the pre-result maker draft")
    thresholds = compute_signal_thresholds()
    dates = list(draft["split"]["development"])
    manifests = [build_day(date, reuse=True) for date in dates]
    analysis = _analyze("development", dates)
    frozen = _freeze_after_development(dates)
    result = {
        "schema": "passive-development-completion-v1",
        "dates": dates,
        "candidate_quotes": analysis["candidate_quotes"],
        "full_fills": analysis["full_fills"],
        "partial_fills": analysis["partial_fills"],
        "thresholds": thresholds,
        "day_manifest_sha256": {
            date: sha256(_day_paths(date)["manifest"]) for date in dates
        },
        "maker_spec_sha256": sha256(FROZEN_SPEC),
        "maker_parameters_selected_from_outcomes": False,
        "reused_day_count": sum(bool(item) for item in manifests),
    }
    write_json(REPORT_ROOT / "development/development_completion.json", result)
    if frozen["status"] != "frozen_after_development":
        raise RuntimeError("maker methodology freeze failed")
    return result


def run_validation() -> dict[str, Any]:
    spec = _validate_frozen()
    dates = list(spec["split"]["validation"])
    _audit_inputs(spec, verify_raw_dates=dates)
    for date in dates:
        build_day(date, reuse=True)
    analysis = _analyze("validation", dates)
    result = {
        "schema": "passive-validation-completion-v1",
        "dates": dates,
        "maker_spec_sha256": sha256(FROZEN_SPEC),
        "analysis": analysis,
    }
    write_json(REPORT_ROOT / "validation/validation_completion.json", result)
    return result


def run_holdout() -> dict[str, Any]:
    spec = _validate_frozen()
    dates = list(spec["split"]["historical_holdout"])
    audit = _audit_inputs(spec, verify_raw_dates=dates)
    opening_path = REPORT_ROOT / "historical_holdout/holdout_opening_audit.json"
    if opening_path.exists():
        opening = _load_json(opening_path)
        if opening["maker_spec_sha256"] != sha256(FROZEN_SPEC):
            raise ValueError("existing holdout opening audit belongs to another maker spec")
    else:
        opening = {
            "schema": "passive-historical-holdout-opening-audit-v1",
            "opened_at_utc": _utc_now(),
            "opened_before_holdout_probe_or_label_read": True,
            "dates": dates,
            "maker_spec_sha256": sha256(FROZEN_SPEC),
            "audit": audit,
            "methodology_changes_after_opening_allowed": False,
        }
        write_json(opening_path, opening)
    for date in dates:
        build_day(date, reuse=True)
    analysis = _analyze("historical_holdout", dates)
    result = {
        "schema": "passive-historical-holdout-completion-v1",
        "dates": dates,
        "maker_spec_sha256": sha256(FROZEN_SPEC),
        "opening_audit_sha256": sha256(opening_path),
        "analysis": analysis,
    }
    write_json(REPORT_ROOT / "historical_holdout/holdout_completion.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("thresholds", "stage-a", "development", "validation", "holdout"),
    )
    args = parser.parse_args()
    actions = {
        "thresholds": compute_signal_thresholds,
        "stage-a": run_stage_a,
        "development": run_development,
        "validation": run_validation,
        "holdout": run_holdout,
    }
    print(json.dumps(actions[args.command](), sort_keys=True))


if __name__ == "__main__":
    main()
