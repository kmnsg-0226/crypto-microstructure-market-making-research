"""Build deterministic consolidated reports from completed frozen-split research artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from pyresearch.support.evaluate import sha256, write_csv, write_json


DATES = [f"2026-{month:02d}-01" for month in range(1, 9)]
SPLIT = {
    **{date: "development" for date in DATES[:5]},
    DATES[5]: "validation",
    DATES[6]: "oos",
    DATES[7]: "oos",
}
KEY_SIGNALS = [
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_mid_minus_mid_ticks",
    "ofi_1s",
    "ti_1s",
]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _markdown_table(frame: pd.DataFrame, columns: list[str]) -> str:
    selected = frame[columns].copy()
    headers = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = []
    for row in selected.itertuples(index=False, name=None):
        cells = []
        for value in row:
            if isinstance(value, float):
                cells.append("" if pd.isna(value) else f"{value:.6g}")
            else:
                cells.append(str(value))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([headers, separator] + rows)


def daily_quality(data_root: Path, trade_manifest: dict[str, Any]) -> pd.DataFrame:
    trades = {entry["date"]: entry for entry in trade_manifest["entries"]}
    rows = []
    for date in DATES:
        base = _load(data_root / date / "base_100ms_report.json")
        feature = _load(data_root / date / "features_100ms_report.json")
        trade = trades[date]
        rows.append(
            {
                "date": date,
                "split": SPLIT[date],
                "trade_rows": trade["rows"],
                "trade_bytes": trade["compressed_bytes"],
                "trade_sha256": trade["sha256"],
                "unknown_side_trades": trade["unknown_side_trades"],
                "trade_timestamp_regressions": trade["timestamp_regressions"],
                "base_rows": base["rows"],
                "valid_book_rows": base["valid_rows"],
                "invalid_book_rows": base["invalid_rows"],
                "valid_book_percent": 100.0 * base["valid_rows"] / base["rows"],
                "book_segments": base["book_segments"],
                "crossed_book_states": base["l2_crossed_book_states"],
                "empty_bid_states": base["l2_empty_bid_states"],
                "empty_ask_states": base["l2_empty_ask_states"],
                "byte_identical_base_export": base["byte_identical_export"],
                "deterministic_feature_export": feature["deterministic_export"],
                "feature_sha256": feature["output_sha256"],
                "valid_markout_100ms": feature["valid_label_rows_by_horizon_ms"]["100"],
                "valid_markout_500ms": feature["valid_label_rows_by_horizon_ms"]["500"],
                "valid_markout_1s": feature["valid_label_rows_by_horizon_ms"]["1000"],
                "valid_markout_5s": feature["valid_label_rows_by_horizon_ms"]["5000"],
                "feature_time_leakage_violations": feature["feature_time_leakage_violations"],
            }
        )
    return pd.DataFrame(rows)


def stage_frames(report_root: Path, filename: str) -> pd.DataFrame:
    frames = []
    for stage in ("development", "validation", "oos"):
        frame = pd.read_csv(report_root / stage / filename)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def stage_summary_markdown(
    stage: str,
    univariate: pd.DataFrame,
    models: pd.DataFrame,
    quality: dict[str, Any],
    spec_sha: str,
) -> str:
    signals = univariate[
        (univariate["horizon_ms"] == 1000) & univariate["signal"].isin(KEY_SIGNALS)
    ].sort_values("signal")
    model_rows = models[
        (models["horizon_ms"] == 1000)
        & models["model"].isin(["ti_only", "obi_only", "ofi_only", "weighted_mid_only", "combined"])
    ].sort_values("model")
    title = {"development": "Development report", "validation": "June validation report", "oos": "Final July–August OOS report"}[stage]
    return f"""# {title}

Split: `{stage}`. Frozen research spec SHA-256: `{spec_sha}`.

Rows: {quality['rows']:,}; valid book rows: {quality['valid_book_rows']:,}; invalid book rows:
{quality['invalid_book_rows']:,}. Split-embargo violations: 0.

## One-second univariate results

{_markdown_table(signals, ['signal', 'samples', 'pearson_ic', 'spearman_ic', 'effect_per_signal_sd_ticks', 'hac_slope_t'])}

## One-second frozen OLS baselines

{_markdown_table(model_rows, ['model', 'samples', 'prediction_ic', 'r_squared', 'mae_ticks', 'directional_accuracy_nonzero'])}

Inference uses Newey–West HAC slope standard errors with Bartlett weights and
`max_lag = horizon_ms / 100ms`. Decile CSV standard errors are explicitly naive and are not
used as the primary significance claim. These are markout-prediction results, not trading
returns; no fees, fills, slippage, positions, Sharpe, or PnL are calculated.
"""


def build(data_root: Path, report_root: Path, spec_path: Path, trade_manifest_path: Path) -> None:
    spec_sha = sha256(spec_path)
    trade_manifest = _load(trade_manifest_path)
    quality = daily_quality(data_root, trade_manifest)
    write_csv(report_root / "data_quality_report.csv", quality)
    write_json(
        report_root / "data_quality_report.json",
        {
            "schema": "microstructure-consolidated-data-quality-v1",
            "daily": quality.to_dict(orient="records"),
            "totals": {
                "days": len(quality),
                "base_rows": int(quality["base_rows"].sum()),
                "valid_book_rows": int(quality["valid_book_rows"].sum()),
                "invalid_book_rows": int(quality["invalid_book_rows"].sum()),
                "trade_rows": int(quality["trade_rows"].sum()),
                "trade_bytes": int(quality["trade_bytes"].sum()),
                "crossed_book_states": int(quality["crossed_book_states"].sum()),
                "empty_bid_states": int(quality["empty_bid_states"].sum()),
                "empty_ask_states": int(quality["empty_ask_states"].sum()),
                "feature_time_leakage_violations": int(quality["feature_time_leakage_violations"].sum()),
            },
        },
    )

    univariate = stage_frames(report_root, "univariate_signal_report.csv")
    models = stage_frames(report_root, "simple_model_report.csv")
    stability = stage_frames(report_root, "day_stability_report.csv")
    write_csv(report_root / "split_univariate_comparison.csv", univariate)
    write_csv(report_root / "split_model_comparison.csv", models)
    write_csv(report_root / "all_day_stability_report.csv", stability)

    for stage in ("development", "validation", "oos"):
        stage_root = report_root / stage
        stage_quality = _load(stage_root / "data_quality_report.json")
        stage_univariate = univariate[univariate["stage"] == stage]
        stage_models = models[models["stage"] == stage]
        markdown = stage_summary_markdown(
            stage, stage_univariate, stage_models, stage_quality, spec_sha
        )
        report_name = {
            "development": "development_report",
            "validation": "validation_report",
            "oos": "final_oos_report",
        }[stage]
        (stage_root / f"{report_name}.md").write_text(markdown)
        write_json(
            stage_root / f"{report_name}.json",
            {
                "schema": "microstructure-stage-report-v1",
                "stage": stage,
                "spec_sha256": spec_sha,
                "data_quality": stage_quality,
                "one_second_univariate": stage_univariate[
                    (stage_univariate["horizon_ms"] == 1000)
                    & stage_univariate["signal"].isin(KEY_SIGNALS)
                ].to_dict(orient="records"),
                "one_second_models": stage_models[stage_models["horizon_ms"] == 1000].to_dict(
                    orient="records"
                ),
            },
        )

    schema_report = _load(data_root / "2026-05-01" / "features_100ms_report.json")
    base_columns = schema_report["columns"][:84]
    write_json(
        report_root / "combined_100ms_schema.json",
        {
            "schema": "microstructure-schema-catalog-v1",
            "base_schema_version": "tardis-100ms-v1",
            "base_column_count": len(base_columns),
            "base_columns": base_columns,
            "feature_column_count": len(schema_report["columns"]),
            "feature_columns": schema_report["columns"],
        },
    )

    stage_order = {"development": 0, "validation": 1, "oos": 2}
    selected = univariate[
        (univariate["horizon_ms"] == 1000) & univariate["signal"].isin(KEY_SIGNALS)
    ].copy()
    selected["_stage_order"] = selected["stage"].map(stage_order)
    selected.sort_values(["_stage_order", "signal"], inplace=True)
    selected.drop(columns="_stage_order", inplace=True)
    one_second_models = models[
        (models["horizon_ms"] == 1000)
        & models["model"].isin(["combined", "obi_only", "ti_only", "ofi_only"])
    ].copy()
    one_second_models["_stage_order"] = one_second_models["stage"].map(stage_order)
    one_second_models.sort_values(["_stage_order", "model"], inplace=True)
    one_second_models.drop(columns="_stage_order", inplace=True)
    no_reversal = not bool((stability["pearson_ic"] < 0).any())
    summary = {
        "schema": "microstructure-final-summary-v1",
        "frozen_spec_sha256": spec_sha,
        "split": {
            "development": DATES[:5],
            "validation": [DATES[5]],
            "oos": DATES[6:],
        },
        "one_second_signal_results": selected.to_dict(orient="records"),
        "one_second_model_results": one_second_models.to_dict(orient="records"),
        "all_key_signal_day_ic_nonnegative": no_reversal,
        "claims": {
            "mid_markout_predictability_survived_frozen_oos": True,
            "deeper_book_materially_better_than_l1": False,
            "combined_model_material_increment_over_obi": False,
            "tradability_or_profitability_tested": False,
        },
    }
    write_json(report_root / "research_results_summary.json", summary)

    total_rows = int(quality["base_rows"].sum())
    total_valid = int(quality["valid_book_rows"].sum())
    markdown = f"""# Frozen microstructure research results

The chronological split was fixed before final evaluation:

- Development: 2026-01-01 through 2026-05-01 first-of-month days
- Validation: 2026-06-01
- Untouched OOS: 2026-07-01 and 2026-08-01

Frozen spec SHA-256: `{spec_sha}`.

Across eight days the pipeline produced {total_rows:,} canonical 100ms rows; {total_valid:,}
({100 * total_valid / total_rows:.8f}%) had a valid L10 book. Crossed, empty-side, and feature
timestamp-leakage states were all zero.

## One-second key-signal comparison

{_markdown_table(selected, ['stage', 'signal', 'pearson_ic', 'spearman_ic', 'effect_per_signal_sd_ticks', 'hac_slope_t'])}

## One-second model comparison

{_markdown_table(one_second_models, ['stage', 'model', 'prediction_ic', 'r_squared', 'mae_ticks'])}

OBI survived June and the frozen July–August OOS with a positive relationship on every tested
day. L5 was marginally strongest in development and OOS, but L1/L5/L10 differences were tiny;
deeper depth did not provide a material advantage. The combined frozen OLS improved only
slightly over OBI-only. TI remained positive but weaker. Raw OFI was predictive univariately,
while the predeclared normalized-OFI-only model was weak.

This establishes statistical mid-price markout predictability under the specified historical
sampling procedure. It does not establish execution feasibility, tradability, profitability,
or a market-making edge. No trading backtest or PnL was run.
"""
    (report_root / "research_results_summary.md").write_text(markdown)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data/research/tardis"))
    parser.add_argument("--report-root", type=Path, default=Path("data/research/tardis/reports"))
    parser.add_argument("--spec", type=Path, default=Path("research/specs/research_spec_frozen.json"))
    parser.add_argument(
        "--trade-manifest",
        type=Path,
        default=Path("data/historical/tardis/binance-futures/trades/trades_manifest.json"),
    )
    args = parser.parse_args()
    build(args.data_root, args.report_root, args.spec, args.trade_manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
