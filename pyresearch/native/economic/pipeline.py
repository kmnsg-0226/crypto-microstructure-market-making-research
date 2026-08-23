"""Produce every artifact of the maker economic feasibility phase.

    python -m pyresearch.native.economic.pipeline surface      # 5x5 queue sensitivity and its decompositions
    python -m pyresearch.native.economic.pipeline oof          # phase 2 out-of-fold diagnostics
    python -m pyresearch.native.economic.pipeline stability    # block, day, segment, regime and side splits
    python -m pyresearch.native.economic.pipeline all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess

import numpy as np
import pandas as pd

from pyresearch.native.economic import data, spec, stats
from pyresearch.native.core import corpus

FLOAT_FORMAT = "%.10g"


# --------------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------------
def _sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_methodology() -> None:
    from pyresearch.native.predictive import spec as predictive_spec

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
        "queue_fills_sha256": {
            f"file{index}": _sha256(data.fills_path(index)) for index in (0, 1, 2)
        },
        "mid_path_sha256": {
            f"file{index}": _sha256(data.mid_path(index)) for index in (0, 1, 2)
        },
        "phase2_oof_sha256": _sha256(
            predictive_spec.DATA_DIR / "oof_side_predictions.csv.zst"
        ),
        "phase2_methodology_sha256": _sha256(
            predictive_spec.REPORT_DIR / "methodology.json"
        ),
    }
    (spec.REPORT_DIR / "methodology.json").write_text(
        json.dumps(spec.methodology(inputs), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------------------------
# Queue sensitivity surface
# --------------------------------------------------------------------------------------------
def run_surface() -> pd.DataFrame:
    frame = data.load_all()
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)

    surface = stats.grouped_stats(frame, ["alpha_pct", "beta_pct"])
    surface.to_csv(
        spec.REPORT_DIR / "queue_sensitivity_surface.csv", index=False, float_format=FLOAT_FORMAT
    )

    stats.grouped_stats(frame, ["alpha_pct", "beta_pct", "side"]).to_csv(
        spec.REPORT_DIR / "side_asymmetry.csv", index=False, float_format=FLOAT_FORMAT
    )

    stats.mechanism_decomposition(frame, ["alpha_pct", "beta_pct", "side"]).to_csv(
        spec.REPORT_DIR / "fill_mechanism_decomposition.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )

    # Break-even benefit in ticks, dollars per BTC and basis points, for every cell.
    benefit_columns = ["alpha_pct", "beta_pct"] + [
        f"required_benefit_{h}ms_{unit}"
        for h in spec.MARKOUT_HORIZONS_MS
        for unit in ("ticks", "usd_per_btc", "bps")
    ] + [
        "mean_spread_ticks",
        "required_benefit_1s_in_half_spreads",
        "required_benefit_1s_in_full_spreads",
        "filled",
        f"markout_{spec.PRIMARY_HORIZON_MS}ms_observed",
    ]
    surface[benefit_columns].to_csv(
        spec.REPORT_DIR / "break_even_benefit.csv", index=False, float_format=FLOAT_FORMAT
    )

    headline = frame[
        pd.MultiIndex.from_arrays([frame["alpha_pct"], frame["beta_pct"]]).isin(
            pd.MultiIndex.from_tuples(spec.HEADLINE_CELLS)
        )
    ]
    paths = []
    for keys, label in (
        (["alpha_pct", "beta_pct"], "all_fills"),
        (["alpha_pct", "beta_pct", "mechanism_name"], "by_mechanism"),
        (["alpha_pct", "beta_pct", "side"], "by_side"),
    ):
        table = stats.markout_path(headline[headline["filled"]], keys)
        table.insert(0, "population", label)
        paths.append(table)
    pd.concat(paths, ignore_index=True).to_csv(
        spec.REPORT_DIR / "markout_paths.csv", index=False, float_format=FLOAT_FORMAT
    )
    print(f"surface written over {len(frame):,} placement-cell rows")
    return frame


# --------------------------------------------------------------------------------------------
# Stability and regimes
# --------------------------------------------------------------------------------------------
def run_stability(frame: pd.DataFrame | None = None) -> None:
    from pyresearch.native.predictive import data as predictive_data

    if frame is None:
        frame = data.load_all(headline_only=True)
    headline = frame[
        pd.MultiIndex.from_arrays([frame["alpha_pct"], frame["beta_pct"]]).isin(
            pd.MultiIndex.from_tuples(spec.HEADLINE_CELLS)
        )
    ].copy()

    stamps = headline["placement_ns"].to_numpy()
    folds = predictive_data.build_folds(stamps)
    fold_index = np.full(len(headline), -1)
    for fold in folds:
        inside = (stamps >= fold.validation_start_ns) & (stamps < fold.validation_end_ns)
        fold_index[inside] = fold.index
    headline["phase2_block"] = fold_index
    headline["utc_day"] = pd.to_datetime(stamps, unit="ns", utc=True).date.astype(str)

    tables = []
    for keys, label in (
        (["alpha_pct", "beta_pct", "phase2_block"], "phase2_validation_block"),
        (["alpha_pct", "beta_pct", "utc_day"], "utc_day"),
        (["alpha_pct", "beta_pct", "file_index", "segment_id"], "segment"),
    ):
        table = stats.grouped_stats(headline, keys)
        table.insert(0, "grouping", label)
        tables.append(table)
    combined = pd.concat(tables, ignore_index=True)
    combined.to_csv(
        spec.REPORT_DIR / "queue_sensitivity_by_block.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )

    # Dependence-aware interval on the headline break-even number.
    rows = []
    for (alpha, beta), block in headline.groupby(["alpha_pct", "beta_pct"], sort=True):
        values = -block[f"markout_{spec.PRIMARY_HORIZON_MS}ms_ticks"].to_numpy(dtype="float64")
        centre, low, high = stats.block_bootstrap_mean(
            values, block["placement_ns"].to_numpy()
        )
        rows.append(
            {
                "alpha_pct": alpha,
                "beta_pct": beta,
                "required_benefit_1s_ticks": centre,
                "block_bootstrap_p05": low,
                "block_bootstrap_p95": high,
            }
        )
    pd.DataFrame(rows).to_csv(
        spec.REPORT_DIR / "break_even_block_bootstrap.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    print("stability artifacts written")
    return headline


def run_regimes(headline: pd.DataFrame | None = None) -> None:
    """Descriptive activity buckets from causal observables measured at placement."""
    from pyresearch.native.predictive import data as predictive_data

    if headline is None:
        headline = run_stability()
    # spread_ticks is already carried by the replay at the placement instant; only pull the
    # activity signals the queue frame does not already have, so no column is duplicated.
    wanted = sorted(set(spec.ACTIVITY_SIGNALS.values()) - set(headline.columns))
    context = predictive_data.load_model_frame(
        columns=["timestamp_ns", "file_index", "segment_id"] + wanted
    ).rename(columns={"timestamp_ns": "placement_ns"})
    merged = headline.merge(
        context, on=["placement_ns", "file_index", "segment_id"], how="left"
    )
    tables = []
    for name, column in spec.ACTIVITY_SIGNALS.items():
        merged["_bucket"] = merged.groupby(["alpha_pct", "beta_pct"], observed=True)[
            column
        ].transform(lambda values: stats.bucket(values, spec.ACTIVITY_BUCKETS))
        block = merged.dropna(subset=["_bucket"])
        table = stats.grouped_stats(block, ["alpha_pct", "beta_pct", "_bucket"])
        table.insert(0, "signal_column", column)
        table.insert(0, "regime", name)
        means = (
            block.groupby(["alpha_pct", "beta_pct", "_bucket"], observed=True)[column]
            .mean()
            .reset_index(name="signal_mean")
        )
        table = table.merge(means, on=["alpha_pct", "beta_pct", "_bucket"], how="left")
        tables.append(table)
    pd.concat(tables, ignore_index=True).rename(columns={"_bucket": "bucket"}).to_csv(
        spec.REPORT_DIR / "activity_regimes.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("activity regime artifacts written")


# --------------------------------------------------------------------------------------------
# Phase 2 out-of-fold diagnostics
# --------------------------------------------------------------------------------------------
def run_oof(headline: pd.DataFrame | None = None) -> None:
    if headline is None:
        headline = data.load_all(headline_only=True)
    predictions = data.load_oof_predictions()
    before = len(headline)
    merged = headline.merge(
        predictions, on=["placement_ns", "file_index", "segment_id", "side"], how="inner"
    )
    print(
        f"joined {len(merged):,} of {before:,} placement-cell rows to frozen phase 2 "
        f"out-of-fold predictions"
    )

    toxicity = []
    for column, label in (
        ("markout_1000ms_lightgbm", "predicted_markout_1s"),
        ("through_given_fill_lightgbm", "predicted_trade_through"),
    ):
        merged["_bucket"] = merged.groupby(["alpha_pct", "beta_pct"], observed=True)[
            column
        ].transform(lambda values: stats.bucket(values, spec.DECILES))
        block = merged.dropna(subset=["_bucket"])
        table = stats.grouped_stats(block, ["alpha_pct", "beta_pct", "_bucket"])
        means = (
            block.groupby(["alpha_pct", "beta_pct", "_bucket"], observed=True)[column]
            .mean()
            .reset_index(name="prediction_mean")
        )
        table = table.merge(means, on=["alpha_pct", "beta_pct", "_bucket"], how="left")
        table.insert(0, "prediction", label)
        toxicity.append(table)
    pd.concat(toxicity, ignore_index=True).rename(columns={"_bucket": "decile"}).to_csv(
        spec.REPORT_DIR / "oof_toxicity_deciles.csv", index=False, float_format=FLOAT_FORMAT
    )

    merged["_bucket"] = merged.groupby(["alpha_pct", "beta_pct"], observed=True)[
        "fill_5000ms_lightgbm"
    ].transform(lambda values: stats.bucket(values, spec.DECILES))
    block = merged.dropna(subset=["_bucket"])
    fill_table = stats.grouped_stats(block, ["alpha_pct", "beta_pct", "_bucket"])
    means = (
        block.groupby(["alpha_pct", "beta_pct", "_bucket"], observed=True)[
            "fill_5000ms_lightgbm"
        ]
        .mean()
        .reset_index(name="prediction_mean")
    )
    fill_table.merge(means, on=["alpha_pct", "beta_pct", "_bucket"], how="left").rename(
        columns={"_bucket": "decile"}
    ).to_csv(spec.REPORT_DIR / "oof_fill_deciles.csv", index=False, float_format=FLOAT_FORMAT)

    grouped = merged.groupby(["alpha_pct", "beta_pct"], observed=True)
    merged["fill_quintile"] = grouped["fill_5000ms_lightgbm"].transform(
        lambda values: stats.bucket(values, spec.QUINTILES)
    )
    merged["toxicity_quintile"] = grouped["markout_1000ms_lightgbm"].transform(
        lambda values: stats.bucket(values, spec.QUINTILES)
    )
    joint = merged.dropna(subset=["fill_quintile", "toxicity_quintile"])
    stats.grouped_stats(
        joint, ["alpha_pct", "beta_pct", "fill_quintile", "toxicity_quintile"]
    ).to_csv(spec.REPORT_DIR / "oof_joint_surface.csv", index=False, float_format=FLOAT_FORMAT)
    print("out-of-fold diagnostic artifacts written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=("methodology", "surface", "stability", "regimes", "oof", "all")
    )
    arguments = parser.parse_args()
    if arguments.stage in ("methodology", "all"):
        write_methodology()
    if arguments.stage == "surface":
        run_surface()
    if arguments.stage == "stability":
        run_stability()
    if arguments.stage == "regimes":
        run_regimes()
    if arguments.stage == "oof":
        run_oof()
    if arguments.stage == "all":
        run_surface()
        headline = run_stability()
        run_regimes(headline)
        run_oof(headline)


if __name__ == "__main__":
    main()
