"""Run the frozen expanded-native executable-PnL experiment.

Usage: python -m pyresearch.native.executable_pnl.pipeline
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from pyresearch import ROOT
REPORT = ROOT / "research/native_executable_pnl"
DATA = ROOT / "data/research/native_executable_pnl"
MANIFEST = REPORT / "expanded_development_manifest.json"
GRID_NS = 100_000_000
HORIZONS_MS = (250, 1000, 5000, 10000, 30000, 60000)
TAILS = (0.10, 0.05, 0.02, 0.01)
COSTS_BP_PER_SIDE = (1.0, 2.0, 3.0, 5.0)
PURGE_NS = 60_000_000_000
SCORE_STRIDE = 10  # Score one causal 100 ms snapshot per second; adjacent rows are strongly correlated.
TRAIN_STRIDE = 1

BASE_FEATURES = [
    "obi_l1", "obi_l5", "obi_l10", "weighted_obi_l10",
    "microprice_minus_mid_ticks", "spread_ticks", "bid_depth_l1", "ask_depth_l1",
    "bid_depth_l10", "ask_depth_l10", "time_since_trade_ms", "time_since_mid_change_ms",
    "segment_age_ms",
]
for _window in (100, 1000, 5000):
    BASE_FEATURES += [
        f"depth_flow_pressure_l10_{_window}ms",
        f"trade_imbalance_{_window}ms", f"signed_volume_{_window}ms",
        f"bbo_change_count_{_window}ms", f"backward_mid_abs_change_ticks_{_window}ms",
    ]
FEATURE_COLUMNS = ["timestamp_ns", "segment_id", "bid_px_1", "ask_px_1"] + BASE_FEATURES


def utc(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_paths() -> list[Path]:
    return [
        ROOT / "data/research/native_dev_v1/native_features_100ms_file0.csv.zst",
        ROOT / "data/research/native_dev_v1/native_features_100ms_file1.csv.zst",
        ROOT / "data/research/native_dev_v1/native_features_100ms_file2.csv.zst",
        DATA / "aws_closed_chain_features_100ms.csv.zst",
    ]


def build_frame() -> pd.DataFrame:
    parts = [read_source(source_id, path) for source_id, path in enumerate(source_paths())]
    return pd.concat(parts, ignore_index=True).sort_values("timestamp_ns", ignore_index=True)


def read_source(source_id: int, path: Path) -> pd.DataFrame:
    """Stream one compressed export: only a 60 s tail crosses a CSV batch boundary."""
    if not path.exists():
        raise FileNotFoundError(f"missing feature export: {path}")
    tail_rows = max(HORIZONS_MS) // 100 + 1
    carry = pd.DataFrame()
    kept: list[pd.DataFrame] = []
    raw_row = 0
    for chunk in pd.read_csv(path, compression="zstd", usecols=FEATURE_COLUMNS, chunksize=200_000):
        chunk.insert(0, "source_id", source_id)
        chunk["segment_key"] = str(source_id) + ":" + chunk["segment_id"].astype("int32").astype(str)
        chunk["raw_row"] = np.arange(raw_row, raw_row + len(chunk), dtype="int64")
        raw_row += len(chunk)
        joined = pd.concat([carry, chunk], ignore_index=True)
        labelled = add_executable_targets(joined)
        ready = labelled.iloc[:-tail_rows] if len(labelled) > tail_rows else labelled.iloc[0:0]
        kept.append(ready[ready["raw_row"] % SCORE_STRIDE == 0].copy())
        carry = joined.iloc[-tail_rows:].copy()
    kept.append(add_executable_targets(carry).query("raw_row % @SCORE_STRIDE == 0").copy())
    return pd.concat(kept, ignore_index=True)


def add_executable_targets(frame: pd.DataFrame) -> pd.DataFrame:
    for horizon in HORIZONS_MS:
        steps = horizon // 100
        entry = frame.groupby("segment_key", sort=False)[["bid_px_1", "ask_px_1", "timestamp_ns"]].shift(-1)
        exit_ = frame.groupby("segment_key", sort=False)[["bid_px_1", "ask_px_1", "timestamp_ns"]].shift(-(steps + 1))
        contiguous = (
            (entry["timestamp_ns"] == frame["timestamp_ns"] + GRID_NS)
            & (exit_["timestamp_ns"] == frame["timestamp_ns"] + (steps + 1) * GRID_NS)
        )
        long_bp = (exit_["bid_px_1"] - entry["ask_px_1"]) * 0.1 / entry["ask_px_1"] * 10_000
        short_bp = (entry["bid_px_1"] - exit_["ask_px_1"]) * 0.1 / entry["bid_px_1"] * 10_000
        frame[f"long_{horizon}ms_bp"] = long_bp.where(contiguous).astype("float32")
        frame[f"short_{horizon}ms_bp"] = short_bp.where(contiguous).astype("float32")
        frame[f"exit_{horizon}ms_ns"] = exit_["timestamp_ns"].where(contiguous).astype("Int64")
    return frame


def folds() -> list[dict[str, int]]:
    dates = pd.date_range("2026-08-19", "2026-08-23", freq="D", tz="UTC")
    return [
        {"fold": index, "start": int(dates[index].value), "end": int(dates[index + 1].value)}
        for index in range(len(dates) - 1)
    ]


def fit_predict(model: str, x_train: np.ndarray, y_train: np.ndarray, x_valid: np.ndarray):
    if model == "obi_only":
        estimator = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=10.0))
    elif model == "linear":
        estimator = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=10.0))
    else:
        estimator = lgb.LGBMRegressor(
            objective="regression_l1", n_estimators=150, learning_rate=0.05, num_leaves=15,
            max_depth=5, min_child_samples=1_000, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=2.0, random_state=0, n_jobs=4, verbosity=-1,
        )
    estimator.fit(x_train, y_train)
    return estimator.predict(x_valid), estimator


def metrics(trades: pd.DataFrame, cost: float) -> dict[str, float | int]:
    net = trades["gross_bp"].to_numpy(dtype="float64") - 2.0 * cost
    n = len(net)
    std = net.std(ddof=1) if n > 1 else np.nan
    days = pd.to_datetime(trades["timestamp_ns"], unit="ns", utc=True).dt.strftime("%Y-%m-%d")
    day_net = pd.Series(net).groupby(days).sum()
    best_day = day_net.max() if len(day_net) else np.nan
    best_five = np.sort(net)[-max(1, int(np.ceil(n * 0.05))):].sum() if n else np.nan
    total = net.sum()
    return {
        "executed_trade_count": n, "gross_executable_edge_bp_per_trade": float(net.mean() + 2 * cost) if n else np.nan,
        "additional_cost_bp_per_side": cost, "net_edge_bp_per_trade": float(net.mean()) if n else np.nan,
        "hit_rate": float((net > 0).mean()) if n else np.nan,
        "net_trade_sharpe": float(net.mean() / std * np.sqrt(n)) if std and np.isfinite(std) and std > 0 else np.nan,
        "cumulative_net_pnl_bp": float(total), "best_day_pnl_fraction": float(best_day / total) if total > 0 else np.nan,
        "best_5pct_trade_pnl_fraction": float(best_five / total) if total > 0 else np.nan,
        "positive_days": int((day_net > 0).sum()), "days": int(len(day_net)),
    }


def non_overlapping(selected: pd.DataFrame, horizon: int) -> pd.DataFrame:
    selected = selected.sort_values("timestamp_ns")
    take, available = [], -1
    for row in selected.itertuples():
        if row.timestamp_ns >= available:
            take.append(row.Index)
            available = int(row.timestamp_ns) + (horizon + 100) * 1_000_000
    return selected.loc[take].copy() if take else selected.iloc[0:0].copy()


def evaluate(oof: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows, by_block, tapes = [], [], []
    for (model, side, horizon), group in oof.groupby(["model", "side", "horizon_ms"], sort=False):
        for tail in TAILS:
            chosen = []
            for _, block in group.groupby("fold", sort=False):
                threshold = block["prediction"].quantile(1 - tail)
                chosen.append(block[block["prediction"] >= threshold])
            selected = pd.concat(chosen, ignore_index=True)
            for cost in COSTS_BP_PER_SIDE:
                predictive = metrics(selected, cost)
                predictive.update({"view": "predictive_all_eligible", "model": model, "side": side, "horizon_ms": horizon, "tail": tail, "eligible_oof_observations": len(group)})
                rows.append(predictive)
                tape = non_overlapping(selected, horizon)
                economic = metrics(tape, cost)
                economic.update({"view": "non_overlapping_trade_tape", "model": model, "side": side, "horizon_ms": horizon, "tail": tail, "eligible_oof_observations": len(group)})
                rows.append(economic)
                for fold, part in tape.groupby("fold", sort=False):
                    detail = metrics(part, cost)
                    detail.update({"model": model, "side": side, "horizon_ms": horizon, "tail": tail, "fold": fold, "cost_bp_per_side": cost, "utc_date": pd.Timestamp(part["timestamp_ns"].iloc[0], unit="ns", tz="UTC").strftime("%Y-%m-%d")})
                    by_block.append(detail)
                if cost == 5.0:
                    tapes.append(tape.assign(model=model, side=side, horizon_ms=horizon, tail=tail, cost_bp_per_side=cost))
    return pd.DataFrame(rows), pd.DataFrame(by_block), pd.concat(tapes, ignore_index=True)


def run(stage: str = "all") -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    frame_path = DATA / "executable_frame_1s.parquet"
    if stage == "frame" or not frame_path.exists():
        frame = build_frame()
        frame.to_parquet(frame_path, index=False, compression="zstd")
        if stage == "frame":
            print(json.dumps({"rows": len(frame), "frame": str(frame_path)}, indent=2))
            return
    else:
        frame = pd.read_parquet(frame_path)
    source_hashes = {str(path.relative_to(ROOT)): sha256(path) for path in source_paths()}
    selected = None
    if stage.startswith("model_"):
        _, fold_text, selected_side = stage.split("_", 2)
        selected = (int(fold_text), selected_side)
    oof_parts, importances, fold_rows = [], [], []
    for fold in folds():
        if selected is not None and fold["fold"] != selected[0]:
            continue
        valid = (frame["timestamp_ns"] >= fold["start"]) & (frame["timestamp_ns"] < fold["end"])
        for side, sign in (("long", 1.0), ("short", -1.0)):
            if selected is not None and side != selected[1]:
                continue
            features = BASE_FEATURES if side == "long" else BASE_FEATURES
            x = frame[features].to_numpy(dtype="float32", copy=True)
            if side == "short":
                for column in ("obi_l1", "obi_l5", "obi_l10", "weighted_obi_l10", "microprice_minus_mid_ticks", "depth_flow_pressure_l10_100ms", "trade_imbalance_100ms", "signed_volume_100ms", "depth_flow_pressure_l10_1000ms", "trade_imbalance_1000ms", "signed_volume_1000ms", "depth_flow_pressure_l10_5000ms", "trade_imbalance_5000ms", "signed_volume_5000ms"):
                    x[:, features.index(column)] *= -1
            for horizon in HORIZONS_MS:
                target = frame[f"{side}_{horizon}ms_bp"].to_numpy(dtype="float64")
                exit_ns = frame[f"exit_{horizon}ms_ns"].to_numpy(dtype="float64")
                train = np.isfinite(target) & (exit_ns <= fold["start"] - PURGE_NS) & ((frame["raw_row"].to_numpy() % TRAIN_STRIDE) == 0)
                score = valid & np.isfinite(target) & (exit_ns < fold["end"])
                if train.sum() < 1_000 or score.sum() == 0:
                    continue
                for model, columns in (("obi_only", ["obi_l10"]), ("linear", features), ("lightgbm", features)):
                    indices = [features.index(column) for column in columns]
                    prediction, estimator = fit_predict(model, x[train][:, indices], target[train], x[score][:, indices])
                    block = frame.loc[score, ["timestamp_ns"]].copy()
                    block["gross_bp"] = target[score]
                    block["prediction"] = prediction
                    block["fold"] = fold["fold"]
                    block["model"] = model
                    block["side"] = side
                    block["horizon_ms"] = horizon
                    oof_parts.append(block)
                    fold_rows.append({"fold": fold["fold"], "side": side, "horizon_ms": horizon, "model": model, "train_rows_1s": int(train.sum()), "validation_rows": int(score.sum()), "train_end_utc": utc(fold["start"] - PURGE_NS), "validation_start_utc": utc(fold["start"]), "validation_end_utc": utc(fold["end"])})
                    if model == "lightgbm":
                        for name, value in zip(columns, estimator.feature_importances_, strict=True):
                            importances.append({"fold": fold["fold"], "side": side, "horizon_ms": horizon, "feature": name, "importance": int(value)})
    if selected is not None:
        tag = f"model_{selected[0]}_{selected[1]}"
        pd.concat(oof_parts, ignore_index=True).to_parquet(DATA / f"{tag}_oof.parquet", index=False, compression="zstd")
        pd.DataFrame(importances).to_parquet(DATA / f"{tag}_importance.parquet", index=False, compression="zstd")
        pd.DataFrame(fold_rows).drop_duplicates().to_csv(DATA / f"{tag}_folds.csv", index=False)
        print(json.dumps({"stage": tag, "oof_rows": sum(len(part) for part in oof_parts)}, indent=2))
        return
    oof = pd.concat(oof_parts, ignore_index=True)
    summary, block, tape = evaluate(oof)
    oof.to_parquet(DATA / "oof_executable_predictions.parquet", index=False, compression="zstd")
    summary.to_csv(REPORT / "oof_economics.csv", index=False)
    block.to_csv(REPORT / "oof_trade_tape_by_block.csv", index=False)
    tape.to_parquet(DATA / "non_overlapping_trade_tape_5bp.parquet", index=False, compression="zstd")
    pd.DataFrame(fold_rows).drop_duplicates().to_csv(REPORT / "folds.csv", index=False)
    importance = pd.DataFrame(importances).groupby("feature", as_index=False)["importance"].sum().sort_values("importance", ascending=False)
    importance.head(20).to_csv(REPORT / "lightgbm_feature_importance.csv", index=False)
    realistic = summary[(summary["view"] == "non_overlapping_trade_tape") & (summary["model"] == "lightgbm") & (summary["additional_cost_bp_per_side"] == 5.0)]
    passing = realistic[(realistic["net_edge_bp_per_trade"] > 0) & (realistic["executed_trade_count"] >= 30) & (realistic["positive_days"] >= 2) & (realistic["best_day_pnl_fraction"] < 0.8)]
    verdict = "A" if len(passing) else ("B" if (summary[(summary["view"] == "non_overlapping_trade_tape") & (summary["model"] == "lightgbm") & (summary["net_edge_bp_per_trade"] > 0) & (summary["additional_cost_bp_per_side"] < 5.0)]).any().any() else "C")
    specification = {
        "schema": "native-executable-pnl-final-v1", "manifest_sha256": sha256(MANIFEST), "source_feature_sha256": source_hashes,
        "execution": {"entry": "next 100ms observable opposite BBO", "exit": "opposite BBO at entry_plus_horizon", "spread_double_counted": False, "cost_bp_per_side": list(COSTS_BP_PER_SIDE)},
        "validation": {"folds": folds(), "purge_seconds": 60, "score_stride_ms": 1000, "random_splits": False},
        "models": {"obi_only": "ridge on signed OBI L10", "linear": "ridge", "lightgbm": {"n_estimators": 150, "num_leaves": 15, "max_depth": 5, "min_child_samples": 1000}},
        "verdict": verdict,
    }
    (REPORT / "frozen_specification.json").write_text(json.dumps(specification, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"rows": len(frame), "oof_rows": len(oof), "verdict": verdict, "realistic_cells": len(realistic)}, indent=2))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) == 2 else "all")
