"""Combine fixed fold/side OOF jobs into the final executable-PnL report."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from pyresearch.native.executable_pnl.pipeline import (
    BASE_FEATURES, COSTS_BP_PER_SIDE, DATA, HORIZONS_MS, MANIFEST, REPORT, TAILS, evaluate, folds, sha256,
)


def run() -> None:
    paths = sorted(DATA.glob("model_*_oof.parquet"))
    if len(paths) != 8:
        raise RuntimeError(f"expected 8 fixed OOF jobs, found {len(paths)}")
    summaries, blocks, tapes = [], [], []
    for model in ("obi_only", "linear", "lightgbm"):
        for side in ("long", "short"):
            side_paths = [path for path in paths if path.stem.endswith(f"_{side}_oof")]
            for horizon in HORIZONS_MS:
                group = pd.concat(
                    [pd.read_parquet(path, filters=[("model", "=", model), ("horizon_ms", "=", horizon)]) for path in side_paths],
                    ignore_index=True,
                )
                summary, block, tape = evaluate(group)
                summaries.append(summary)
                blocks.append(block)
                tapes.append(tape)
    economics = pd.concat(summaries, ignore_index=True)
    by_block = pd.concat(blocks, ignore_index=True)
    tape = pd.concat(tapes, ignore_index=True)
    economics.to_csv(REPORT / "oof_economics.csv", index=False)
    by_block.to_csv(REPORT / "oof_trade_tape_by_block.csv", index=False)
    tape.to_parquet(DATA / "non_overlapping_trade_tape_5bp.parquet", index=False, compression="zstd")
    fold_tables = [pd.read_csv(path) for path in sorted(DATA.glob("model_*_folds.csv"))]
    pd.concat(fold_tables, ignore_index=True).drop_duplicates().to_csv(REPORT / "folds.csv", index=False)
    importance = pd.concat([pd.read_parquet(path) for path in sorted(DATA.glob("model_*_importance.parquet"))], ignore_index=True)
    importance.groupby("feature", as_index=False)["importance"].sum().sort_values("importance", ascending=False).head(20).to_csv(REPORT / "lightgbm_feature_importance.csv", index=False)
    realistic = economics[(economics["view"] == "non_overlapping_trade_tape") & (economics["model"] == "lightgbm") & (economics["additional_cost_bp_per_side"] == 5.0)]
    passing = realistic[(realistic["net_edge_bp_per_trade"] > 0) & (realistic["executed_trade_count"] >= 30) & (realistic["positive_days"] >= 2) & (realistic["best_day_pnl_fraction"] < 0.8)]
    cheaper = economics[(economics["view"] == "non_overlapping_trade_tape") & (economics["model"] == "lightgbm") & (economics["additional_cost_bp_per_side"] < 5.0) & (economics["net_edge_bp_per_trade"] > 0)]
    verdict = "A" if not passing.empty else ("B" if not cheaper.empty else "C")
    best = realistic.sort_values("net_edge_bp_per_trade", ascending=False).head(1)
    selected = best.iloc[0].to_dict() if not best.empty else {}
    specification = {
        "schema": "native-executable-pnl-final-v1", "manifest_sha256": sha256(MANIFEST),
        "entry": "next 100ms observable opposite BBO", "exit": "opposite BBO at entry plus horizon", "spread_double_counted": False,
        "costs_bp_per_side": list(COSTS_BP_PER_SIDE), "score_grid_ms": 1000, "purge_seconds": 60,
        "folds": folds(), "tails": list(TAILS), "verdict": verdict,
        "features": BASE_FEATURES,
        "models": {
            "obi_only": "ridge on signed OBI L10", "linear": "median-imputed standardized ridge(alpha=10)",
            "lightgbm": {"objective": "regression_l1", "n_estimators": 150, "num_leaves": 15, "max_depth": 5, "min_child_samples": 1000, "learning_rate": 0.05},
        },
        "economic_selection": selected if verdict in {"A", "B"} else None,
    }
    (REPORT / "frozen_specification.json").write_text(json.dumps(specification, indent=2, sort_keys=True) + "\n")
    report = ["# Final native executable-PnL experiment", "", f"Verdict: **{verdict}**.", ""]
    if verdict == "C":
        report.append("Standalone Binance microstructure alpha is economically closed at the 5 bp-per-side realistic gate.")
    elif selected:
        report.append("Best realistic LightGBM tape cell:")
        report += ["", "| Side | Horizon ms | Tail | Trades | Net bp/trade | Positive days | Best-day fraction |", "|---|---:|---:|---:|---:|---:|---:|"]
        report.append(f"| {selected['side']} | {int(selected['horizon_ms'])} | {selected['tail']:.0%} | {int(selected['executed_trade_count'])} | {selected['net_edge_bp_per_trade']:.4f} | {int(selected['positive_days'])}/{int(selected['days'])} | {selected['best_day_pnl_fraction']:.3f} |")
    (REPORT / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"verdict": verdict, "best_realistic_lightgbm": selected, "economic_rows": len(economics)}, indent=2, default=float))


if __name__ == "__main__":
    run()
