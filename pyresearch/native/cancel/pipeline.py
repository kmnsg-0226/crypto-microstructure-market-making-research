"""Run the cancel / stay falsification study.

    python -m pyresearch.native.cancel.pipeline frames     # 100 ms decision frames
    python -m pyresearch.native.cancel.pipeline scores     # refit the phase 4A sweep model, score at 100 ms
    python -m pyresearch.native.cancel.pipeline timeline   # first-crossing times for every fixed threshold
    python -m pyresearch.native.cancel.pipeline surface    # the whole threshold x latency x queue grid
    python -m pyresearch.native.cancel.pipeline signal     # deciles, lead time, persistence, mechanism
    python -m pyresearch.native.cancel.pipeline all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess

import numpy as np
import pandas as pd

from pyresearch.native.cancel import analysis, counterfactual, scoring, spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.queue_tail import spec as qt_spec
from pyresearch.native.core import corpus

FLOAT_FORMAT = "%.10g"
CELL_KEYS = ["queue_cell", "threshold", "latency_ms"]


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(frame: pd.DataFrame, name: str) -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(spec.REPORT_DIR / name, index=False, float_format=FLOAT_FORMAT)
    print(f"  {name}: {len(frame):,} rows")


def folds():
    return predictive_data.build_folds(
        predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy()
    )


def write_methodology() -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    inputs = {
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=spec.ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "corpus_spec_sha256": _sha256(corpus.SPEC_PATH),
        "phase_3_queue_fills_sha256": {
            f"file{i}": _sha256(
                spec.ROOT / f"data/research/native_economic_v1/queue_fills_file{i}.csv.zst"
            )
            for i in (0, 1, 2)
        },
        "phase_4a_level_grid_sha256": {
            f"file{i}": _sha256(qt_data.grid_path(i)) for i in (0, 1, 2)
        },
        "phase_4a_oof_sweep_predictions_sha256": _sha256(
            qt_spec.DATA_DIR / "oof_sweep_predictions.csv.zst"
        ),
        "phase_4a_model_frame_sha256": _sha256(qt_data.frame_path()),
    }
    (spec.REPORT_DIR / "methodology.json").write_text(
        json.dumps(spec.methodology(inputs), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (spec.REPORT_DIR / "grid_spec.json").write_text(
        json.dumps(spec.grid_spec(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    predictive_data.fold_table(
        predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy(),
        folds(),
    ).to_csv(spec.REPORT_DIR / "folds.csv", index=False, float_format=FLOAT_FORMAT)


# --------------------------------------------------------------------------------------------
# Cohort and decision timeline
# --------------------------------------------------------------------------------------------
def oof_window() -> tuple[int, int]:
    """The chronological span over which a genuinely out-of-fold score exists."""
    blocks = folds()
    return blocks[0].validation_start_ns, blocks[-1].validation_end_ns


def timeline_path():
    return spec.DATA_DIR / "decision_timeline.parquet"


def run_timeline() -> None:
    write_methodology()
    scores = scoring.load_scores()
    orders = counterfactual.load_orders()
    start, end = oof_window()
    placement = orders["placement_ns"].to_numpy()
    # Only placements that sit inside a validation block can be judged with a score that the
    # model never trained on. Everything earlier stays in the training past and is excluded.
    cohort = orders[(placement >= start) & (placement < end)].reset_index(drop=True)
    opportunity = counterfactual.opportunities(cohort)
    timeline = counterfactual.decision_timeline(opportunity, scores)
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    timeline.to_parquet(timeline_path(), index=False, compression="zstd")
    cohort.to_parquet(spec.DATA_DIR / "cohort.parquet", index=False, compression="zstd")

    instants = timeline["decision_instants"].to_numpy()
    covered = np.where(
        instants > 0,
        (timeline["last_decision_ns"].to_numpy() - timeline["placement_ns"].to_numpy()) / 1e6,
        0.0,
    )
    window_ms = (
        timeline["observed_end_ns"].to_numpy() - timeline["placement_ns"].to_numpy()
    ) / 1e6
    coverage = {
        "opportunities": int(len(timeline)),
        "excluded_before_first_validation_block": int(
            (orders["placement_ns"].to_numpy() < start).sum() / len(spec.QUEUE_CELLS)
        ),
        "mean_decision_instants": float(instants.mean()),
        "median_decision_instants": float(np.median(instants)),
        "orders_with_no_decision_instant": float((instants == 0).mean()),
        "mean_scored_window_ms": float(covered.mean()),
        "median_scored_window_ms": float(np.median(covered)),
        "mean_observation_window_ms": float(window_ms.mean()),
        "mean_scored_fraction_of_window": float((covered / window_ms).mean()),
        "note": "a decision instant exists only while the order's own quote price is still the "
        "best price on its side; once it is not, the level-sweep score no longer refers to the "
        "order's level and no cancellation decision is taken",
    }
    (spec.REPORT_DIR / "signal_coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(coverage, indent=2, sort_keys=True))


def load_cohort() -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        pd.read_parquet(spec.DATA_DIR / "cohort.parquet"),
        pd.read_parquet(timeline_path()),
    )


# --------------------------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------------------------
def _stability_frames(applied: pd.DataFrame, tags: dict) -> dict[str, pd.DataFrame]:
    applied = applied.copy()
    applied["block"] = analysis.block_id(applied["placement_ns"].to_numpy())
    applied["utc_day"] = analysis.utc_day(applied["placement_ns"].to_numpy())
    out = {}
    for name, keys in (
        ("block", ["block"]),
        ("day", ["utc_day"]),
        ("segment", ["file_index", "segment_id"]),
    ):
        table = analysis.grouped_metrics(applied, keys)
        for key, value in tags.items():
            table.insert(0, key, value)
        out[name] = table
    return out


def run_surface() -> None:
    cohort, timeline = load_cohort()
    surface: list[dict] = []
    stability = {"block": [], "day": [], "segment": []}
    mechanism: list[pd.DataFrame] = []
    lead: list[dict] = []

    for name in spec.QUEUE_CELLS:
        orders = cohort[cohort["queue_cell"] == name].reset_index(drop=True)
        baseline = analysis.baseline_metrics(
            counterfactual.apply_cancel(orders, timeline, spec.CANCEL_THRESHOLDS[0], 0)
        )
        surface.append(
            {"queue_cell": name, "threshold": np.nan, "latency_ms": np.nan,
             "policy": spec.NEVER_CANCEL, **baseline}
        )
        for threshold in spec.CANCEL_THRESHOLDS:
            applied = counterfactual.apply_cancel(orders, timeline, threshold, 0)
            # Lead time does not depend on the latency: it is the distance from the first
            # crossing to the never-cancel fill.
            filled = applied[applied["filled"]]
            markout = filled[analysis.PRIMARY].to_numpy(dtype="float64")
            populations = {
                "all_fills": filled,
                "adverse_fills": filled[markout < 0],
            }
            for ticks in spec.CATASTROPHIC_THRESHOLDS_TICKS:
                populations[f"catastrophic_{ticks}"] = filled[markout <= -ticks]
            for population, part in populations.items():
                lead.append(
                    {
                        "queue_cell": name,
                        "threshold": threshold,
                        **analysis.lead_time(part, population),
                    }
                )
            for latency in spec.CANCEL_LATENCIES_MS:
                cell = (
                    applied
                    if latency == 0
                    else counterfactual.apply_cancel(orders, timeline, threshold, latency)
                )
                tags = {"queue_cell": name, "threshold": threshold, "latency_ms": latency}
                surface.append({**tags, "policy": "cancel", **analysis.cell_metrics(cell)})
                for key, table in _stability_frames(cell, tags).items():
                    stability[key].append(table)
                if (
                    threshold in spec.HEADLINE_THRESHOLDS
                    and latency in spec.HEADLINE_LATENCIES_MS
                ):
                    table = analysis.grouped_metrics(cell, ["mechanism_name"])
                    for key in reversed(list(tags)):
                        table.insert(0, key, tags[key])
                    mechanism.append(table)
            print(f"  {name} p>={threshold:.2f} done")

    table = pd.DataFrame(surface)
    _write(table, "threshold_latency_surface.csv")

    headline = table[
        table["threshold"].isin(spec.HEADLINE_THRESHOLDS)
        & table["latency_ms"].isin(spec.HEADLINE_LATENCIES_MS)
    ]
    _write(headline, "headline_cells.csv")

    economics = [
        "eligible_opportunities", "baseline_fills", "baseline_unfilled",
        "cancelled_opportunities", "cancel_rate", "fills_prevented", "surviving_fills",
        "adverse_fills_avoided", "favourable_fills_sacrificed",
        "non_negative_fills_sacrificed", "prevented_fills_markout_censored",
        "total_baseline_negative_ticks", "total_baseline_positive_ticks",
        "adverse_markout_avoided", "favourable_markout_sacrificed", "net_markout_preserved",
        "net_per_eligible_opportunity", "net_per_baseline_fill", "net_per_cancelled_order",
        "avoidance_efficiency_ticks", "fraction_baseline_negative_removed",
        "fraction_baseline_positive_removed", "prevented_share_of_baseline_fills",
        "baseline_negative_to_positive_ratio", "random_match_adverse_avoided",
        "random_match_favourable_sacrificed", "random_match_net_markout",
        "net_markout_lift_over_random", "avoidance_efficiency_lift",
    ] + [
        f"catastrophic_{t}_{suffix}"
        for t in spec.CATASTROPHIC_THRESHOLDS_TICKS
        for suffix in ("avoidance_efficiency", "random_match", "avoidance_lift")
    ]
    _write(table[CELL_KEYS + ["policy"] + economics], "avoided_vs_sacrificed.csv")

    tail = [
        "eligible_opportunities", "baseline_fills", "cancelled_opportunities", "cancel_rate",
        "surviving_fills", "baseline_fill_rate", "surviving_fill_rate", "fill_rate_reduction",
        "cancel_too_late", "cancel_too_late_rate", "cancel_without_baseline_fill",
        "unnecessary_cancel_rate",
    ]
    tail += [c for c in table.columns if c.startswith(("baseline_", "surviving_")) and c not in tail]
    tail += [f"catastrophic_{t}_avoided" for t in spec.CATASTROPHIC_THRESHOLDS_TICKS]
    _write(table[CELL_KEYS + ["policy"] + [c for c in dict.fromkeys(tail)]], "tail_protection.csv")

    _write(pd.DataFrame(lead), "signal_lead_time.csv")
    _write(pd.concat(mechanism, ignore_index=True), "mechanism_decomposition.csv")

    # The per-block table is one row per cell per 30-minute block, so only the columns the
    # stability question actually needs are committed; the full metric set stays in the surface.
    stability_columns = CELL_KEYS + [
        "block", "utc_day", "file_index", "segment_id",
        "eligible_opportunities", "baseline_fills", "cancelled_opportunities", "cancel_rate",
        "fills_prevented", "surviving_fills", "favourable_fills_sacrificed",
        "adverse_markout_avoided", "favourable_markout_sacrificed", "net_markout_preserved",
        "net_per_eligible_opportunity", "avoidance_efficiency_ticks",
        "avoidance_efficiency_lift", "net_markout_lift_over_random", "cancel_too_late_rate",
    ] + [
        f"catastrophic_{t}_{suffix}"
        for t in spec.CATASTROPHIC_THRESHOLDS_TICKS
        for suffix in ("avoided", "avoidance_lift")
    ]
    for name, frames in stability.items():
        combined = pd.concat(frames, ignore_index=True)
        combined = combined[[c for c in stability_columns if c in combined.columns]]
        _write(combined, f"{name}_stability.csv")
        if name == "block":
            summaries = [
                analysis.block_summary(combined, CELL_KEYS, column)
                for column in (
                    "net_per_eligible_opportunity",
                    "adverse_markout_avoided",
                    "favourable_markout_sacrificed",
                    "avoidance_efficiency_ticks",
                    "net_markout_lift_over_random",
                )
            ]
            _write(pd.concat(summaries, ignore_index=True), "block_stability_summary.csv")

    run_transport(table)
    run_monotonicity(table)


def run_transport(table: pd.DataFrame) -> None:
    """Does the same observable state protect the order under every queue assumption?"""
    cancels = table[table["policy"] == "cancel"]
    columns = [
        "cancel_rate",
        "net_per_eligible_opportunity",
        "avoidance_efficiency_ticks",
        "fraction_baseline_negative_removed",
        "fraction_baseline_positive_removed",
        "cancel_too_late_rate",
        "net_markout_lift_over_random",
        "avoidance_efficiency_lift",
    ] + [f"catastrophic_{t}_avoidance_lift" for t in spec.CATASTROPHIC_THRESHOLDS_TICKS]
    wide = cancels.pivot_table(
        index=["threshold", "latency_ms"], columns="queue_cell", values=columns
    )
    wide.columns = [f"{stat}__{cell}" for stat, cell in wide.columns]
    wide = wide.reset_index()
    for stat in columns:
        cells = [f"{stat}__{name}" for name in spec.QUEUE_CELLS]
        values = wide[cells].to_numpy(dtype="float64")
        wide[f"{stat}__min"] = np.nanmin(values, axis=1)
        wide[f"{stat}__max"] = np.nanmax(values, axis=1)
        wide[f"{stat}__spread"] = wide[f"{stat}__max"] - wide[f"{stat}__min"]
    # A signal that only works under one queue assumption is the failure mode phase 2 already
    # hit, so the sign agreement across the three cells is reported directly.
    net = wide[[f"net_per_eligible_opportunity__{n}" for n in spec.QUEUE_CELLS]].to_numpy()
    wide["net_positive_in_all_cells"] = np.all(net > 0, axis=1)
    wide["net_positive_in_any_cell"] = np.any(net > 0, axis=1)
    wide["net_positive_only_in_optimistic"] = (
        (wide["net_per_eligible_opportunity__optimistic"].to_numpy() > 0)
        & (wide["net_per_eligible_opportunity__conservative"].to_numpy() <= 0)
        & (wide["net_per_eligible_opportunity__midpoint"].to_numpy() <= 0)
    )
    _write(wide, "queue_transport.csv")


def run_monotonicity(table: pd.DataFrame) -> None:
    cancels = table[table["policy"] == "cancel"]
    rows = [
        analysis.monotonicity(cancels, ["queue_cell", "latency_ms"], "threshold", column)
        for column in (
            "cancel_rate",
            "surviving_cat_25_per_opportunity",
            "surviving_cat_50_per_opportunity",
            "favourable_fills_sacrificed",
            "adverse_markout_avoided",
            "net_per_eligible_opportunity",
            "avoidance_efficiency_ticks",
            "net_markout_lift_over_random",
        )
    ]
    _write(pd.concat(rows, ignore_index=True), "threshold_monotonicity.csv")


# --------------------------------------------------------------------------------------------
# Descriptive signal studies
# --------------------------------------------------------------------------------------------
def run_signal() -> None:
    cohort, _ = load_cohort()
    scores = scoring.load_scores()
    placement = scores[
        ["timestamp_ns", "file_index", "segment_id", "side", "sweep_p", spec.SWEEP_TARGET]
    ].rename(
        columns={
            "timestamp_ns": "placement_ns",
            "sweep_p": "sweep_p_at_placement",
            spec.SWEEP_TARGET: "realised_trade_through",
        }
    )
    fills = cohort.merge(
        placement, on=["placement_ns", "file_index", "segment_id", "side"], how="left"
    )
    _write(analysis.score_deciles(fills), "sweep_score_deciles.csv")

    persistence = []
    for name in spec.QUEUE_CELLS:
        orders = cohort[cohort["queue_cell"] == name]
        for threshold in spec.CANCEL_THRESHOLDS:
            part = counterfactual.persistence(orders, scores, threshold)
            markout = part[analysis.PRIMARY].to_numpy(dtype="float64")
            populations = {"all_fills": np.isfinite(markout), "adverse_fills": markout < 0}
            for ticks in spec.CATASTROPHIC_THRESHOLDS_TICKS:
                populations[f"catastrophic_{ticks}"] = markout <= -ticks
            for population, mask in populations.items():
                block = part[mask]
                observations = block["run_observations"].to_numpy(dtype="float64")
                duration = block["run_duration_ms"].to_numpy(dtype="float64")
                warning = block["warning_ms"].to_numpy(dtype="float64")
                active = observations > 0
                persistence.append(
                    {
                        "queue_cell": name,
                        "threshold": threshold,
                        "population": population,
                        "fills": int(len(block)),
                        "fills_above_threshold_at_last_instant": int(active.sum()),
                        "share_above_threshold_at_last_instant": float(active.mean())
                        if len(block)
                        else np.nan,
                        "share_single_observation": float((observations == 1).mean())
                        if len(block)
                        else np.nan,
                        "share_two_or_more": float((observations >= 2).mean())
                        if len(block)
                        else np.nan,
                        "share_five_or_more": float((observations >= 5).mean())
                        if len(block)
                        else np.nan,
                        "mean_consecutive_observations": float(observations[active].mean())
                        if active.any()
                        else np.nan,
                        "median_run_duration_ms": float(np.nanmedian(duration[active]))
                        if active.any()
                        else np.nan,
                        "median_warning_ms": float(np.nanmedian(warning[active]))
                        if active.any()
                        else np.nan,
                    }
                )
        print(f"  persistence {name} done")
    _write(pd.DataFrame(persistence), "signal_persistence.csv")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=["frames", "scores", "timeline", "surface", "signal", "all"],
    )
    stage = parser.parse_args().stage
    if stage in ("frames", "all"):
        scoring.build_decision_frames()
    if stage in ("scores", "all"):
        checks = scoring.score_all()
        _write(checks, "score_provenance.csv")
    if stage in ("timeline", "all"):
        run_timeline()
    if stage in ("surface", "all"):
        run_surface()
    if stage in ("signal", "all"):
        run_signal()


if __name__ == "__main__":
    main()
