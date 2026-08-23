"""Leakage-controlled univariate, decile, and small OLS research reports."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterable

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "crypto-hft-mpl"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "crypto-hft-cache"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import rankdata

from pyresearch.support.feature_pipeline import WINDOW_NAMES, load_spec


STAGE_SPLIT_KEY = {
    "development": "development",
    "validation": "validation",
    "oos": "oos",
}


@dataclass(frozen=True)
class EvaluationArtifacts:
    univariate: pd.DataFrame
    deciles: pd.DataFrame
    day_stability: pd.DataFrame
    model_metrics: pd.DataFrame
    model_coefficients: pd.DataFrame


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def write_json(path: Path, payload: Any) -> None:
    data = (json.dumps(_json_ready(payload), indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = frame.to_csv(index=False, float_format="%.12g", lineterminator="\n").encode()
    if path.exists() and path.read_bytes() == data:
        return
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def validate_stage_dates(stage: str, dates: Iterable[str], spec: dict[str, Any]) -> None:
    observed = sorted(set(dates))
    if stage == "preliminary":
        expected = ["2026-05-01"]
    else:
        if stage not in STAGE_SPLIT_KEY:
            raise ValueError(f"unknown evaluation stage: {stage}")
        expected = sorted(spec["split"][STAGE_SPLIT_KEY[stage]])
    if observed != expected:
        raise ValueError(f"{stage} requires dates {expected}, received {observed}")
    if stage in {"validation", "oos"} and spec.get("status") != "frozen_after_development":
        raise ValueError(f"{stage} requires a frozen research specification")


def assert_split_embargo(frame: pd.DataFrame, spec: dict[str, Any]) -> dict[str, int]:
    day_us = 86_400_000_000
    day_start = (frame["sample_time_us"] // day_us) * day_us
    violations: dict[str, int] = {}
    for horizon_ms in spec["label_horizons_ms"]:
        label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
        has_label = frame[label].notna()
        crosses = frame["sample_time_us"] + horizon_ms * 1000 >= day_start + day_us
        violations[str(horizon_ms)] = int((has_label & crosses).sum())
    if any(violations.values()):
        raise ValueError(f"split embargo violations: {violations}")
    return violations


def required_columns(spec: dict[str, Any]) -> list[str]:
    signals = list(spec["canonical_signals"])
    labels = [f"markout_{WINDOW_NAMES[x]}_ticks" for x in spec["label_horizons_ms"]]
    model_features = sorted(
        {feature for features in spec["models"]["specifications"].values() for feature in features}
    )
    return sorted(
        set(
            [
                "date",
                "sample_time_us",
                "feature_segment_id",
                "valid_book_state",
                "spread_ticks",
                "mid_ticks_x2",
            ]
            + signals
            + labels
            + model_features
        )
    )


def load_inputs(paths: list[Path], spec: dict[str, Any]) -> pd.DataFrame:
    columns = required_columns(spec)
    frames = [pd.read_parquet(path, columns=columns) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame.sort_values(["date", "sample_time_us"], kind="stable", inplace=True, ignore_index=True)
    if frame.duplicated(["date", "sample_time_us"]).any():
        raise ValueError("duplicate date/sample_time rows")
    assert_split_embargo(frame, spec)
    return frame


def fit_transforms(frame: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    thresholds: dict[str, list[float]] = {}
    for signal in spec["canonical_signals"]:
        values = frame[signal].to_numpy(dtype="float64")
        values = values[np.isfinite(values)]
        if values.size == 0:
            raise ValueError(f"no development observations for {signal}")
        thresholds[signal] = np.quantile(values, np.arange(1, 10) / 10.0).tolist()

    model_features = sorted(
        {feature for features in spec["models"]["specifications"].values() for feature in features}
    )
    scaling: dict[str, dict[str, float]] = {}
    for feature in model_features:
        values = frame[feature].to_numpy(dtype="float64")
        values = values[np.isfinite(values)]
        mean = float(values.mean())
        std = float(values.std(ddof=0))
        if not np.isfinite(std) or std == 0:
            raise ValueError(f"zero-variance development model feature: {feature}")
        scaling[feature] = {"mean": mean, "population_std": std, "count": int(values.size)}

    daily = daily_regimes(frame)
    regime_thresholds = {
        "spread_day_median": float(daily["median_spread_ticks"].median()),
        "volatility_day_median": float(daily["mid_return_std_ticks"].median()),
    }
    return {
        "schema": "microstructure-development-transforms-v1",
        "fit_split": "development",
        "fit_dates": sorted(frame["date"].unique().tolist()),
        "decile_thresholds": thresholds,
        "standardization": scaling,
        "regime_thresholds": regime_thresholds,
    }


def assign_bins(values: pd.Series, thresholds: list[float]) -> pd.Series:
    raw = values.to_numpy(dtype="float64")
    bins = np.searchsorted(np.asarray(thresholds, dtype="float64"), raw, side="right") + 1
    result = pd.Series(bins, index=values.index, dtype="Int64")
    result.loc[~np.isfinite(raw)] = pd.NA
    return result


def daily_regimes(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for date, day in frame.groupby("date", sort=True):
        valid = day["valid_book_state"].eq(1) & day["feature_segment_id"].notna()
        selected = day.loc[valid]
        mid_ticks = selected["mid_ticks_x2"].to_numpy(dtype="float64") / 2.0
        segments = selected["feature_segment_id"].to_numpy()
        changes = np.diff(mid_ticks)
        same = segments[1:] == segments[:-1]
        returns = changes[same]
        timestamp = pd.Timestamp(date, tz="UTC")
        rows.append(
            {
                "date": date,
                "weekday": timestamp.day_name(),
                "weekend": bool(timestamp.dayofweek >= 5),
                "valid_samples": int(valid.sum()),
                "median_spread_ticks": float(selected["spread_ticks"].median()),
                "mid_return_std_ticks": float(np.std(returns, ddof=0)) if returns.size else np.nan,
            }
        )
    return pd.DataFrame(rows)


def apply_regimes(frame: pd.DataFrame, transforms: dict[str, Any]) -> pd.DataFrame:
    daily = daily_regimes(frame)
    limits = transforms["regime_thresholds"]
    daily["spread_regime"] = np.where(
        daily["median_spread_ticks"] > limits["spread_day_median"], "high", "low"
    )
    daily["volatility_regime"] = np.where(
        daily["mid_return_std_ticks"] > limits["volatility_day_median"], "high", "low"
    )
    return daily


def _finite_pair(frame: pd.DataFrame, x_name: str, y_name: str) -> pd.DataFrame:
    pair = frame[["date", "sample_time_us", "feature_segment_id", x_name, y_name]].dropna()
    values = pair[[x_name, y_name]].to_numpy(dtype="float64")
    finite = np.isfinite(values).all(axis=1)
    return pair.loc[finite]


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan
    return _pearson(rankdata(x, method="average"), rankdata(y, method="average"))


def hac_slope(pair: pd.DataFrame, x_name: str, y_name: str, max_lag: int) -> dict[str, float]:
    x = pair[x_name].to_numpy(dtype="float64")
    y = pair[y_name].to_numpy(dtype="float64")
    if x.size < 3 or np.std(x) == 0:
        return {"slope": np.nan, "hac_se": np.nan, "hac_t": np.nan, "effect_per_sd": np.nan}
    design = np.column_stack((np.ones(x.size), x))
    inv_xx = np.linalg.inv(design.T @ design)
    coefficients = inv_xx @ design.T @ y
    residual = y - design @ coefficients
    scores = design * residual[:, None]
    meat = scores.T @ scores
    date = pair["date"].to_numpy()
    segment = pair["feature_segment_id"].to_numpy()
    timestamp = pair["sample_time_us"].to_numpy(dtype="int64")
    run_start = np.ones(x.size, dtype=bool)
    run_start[1:] = (
        (date[1:] != date[:-1])
        | (segment[1:] != segment[:-1])
        | (timestamp[1:] - timestamp[:-1] != 100_000)
    )
    run = np.cumsum(run_start)
    for lag in range(1, max_lag + 1):
        same = run[lag:] == run[:-lag]
        if not same.any():
            continue
        covariance = scores[lag:][same].T @ scores[:-lag][same]
        weight = 1.0 - lag / (max_lag + 1.0)
        meat += weight * (covariance + covariance.T)
    covariance = inv_xx @ meat @ inv_xx
    se = float(np.sqrt(max(covariance[1, 1], 0.0)))
    slope = float(coefficients[1])
    return {
        "slope": slope,
        "hac_se": se,
        "hac_t": slope / se if se > 0 else np.nan,
        "effect_per_sd": slope * float(np.std(x, ddof=0)),
    }


def univariate_report(frame: pd.DataFrame, spec: dict[str, Any], stage: str) -> pd.DataFrame:
    rows = []
    for signal in spec["canonical_signals"]:
        for horizon_ms in spec["label_horizons_ms"]:
            label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
            pair = _finite_pair(frame, signal, label)
            x = pair[signal].to_numpy(dtype="float64")
            y = pair[label].to_numpy(dtype="float64")
            hac = hac_slope(pair, signal, label, horizon_ms // spec["grid_ms"])
            rows.append(
                {
                    "stage": stage,
                    "signal": signal,
                    "horizon_ms": horizon_ms,
                    "samples": int(len(pair)),
                    "signal_mean": float(x.mean()),
                    "signal_std": float(x.std(ddof=0)),
                    "markout_mean_ticks": float(y.mean()),
                    "markout_std_ticks": float(y.std(ddof=0)),
                    "pearson_ic": _pearson(x, y),
                    "spearman_ic": _spearman(x, y),
                    "slope_ticks_per_signal_unit": hac["slope"],
                    "effect_per_signal_sd_ticks": hac["effect_per_sd"],
                    "hac_slope_se": hac["hac_se"],
                    "hac_slope_t": hac["hac_t"],
                    "hac_max_lag_100ms_steps": horizon_ms // spec["grid_ms"],
                }
            )
    return pd.DataFrame(rows).sort_values(["signal", "horizon_ms"], ignore_index=True)


def decile_report(
    frame: pd.DataFrame,
    spec: dict[str, Any],
    transforms: dict[str, Any],
    stage: str,
    by_day: bool = False,
) -> pd.DataFrame:
    rows = []
    group_keys = ["date"] if by_day else []
    for signal in spec["canonical_signals"]:
        bins = assign_bins(frame[signal], transforms["decile_thresholds"][signal])
        for horizon_ms in spec["label_horizons_ms"]:
            label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
            work = pd.DataFrame({"date": frame["date"], "bin": bins, "markout": frame[label]}).dropna()
            iterator = work.groupby("date", sort=True) if group_keys else [(None, work)]
            for group_value, selected in iterator:
                grouped = selected.groupby("bin", observed=True)["markout"]
                summary = grouped.agg(["count", "mean", "median", "std"]).reset_index()
                means = summary.set_index("bin")["mean"]
                lowest_bin = int(summary["bin"].min())
                highest_bin = int(summary["bin"].max())
                # Frozen quantiles can coincide for discrete signals such as TI (large atoms
                # at -1/+1). Retain the declared bin IDs and compare the lowest/highest bins
                # actually populated instead of silently returning no spread.
                top_bottom = means[highest_bin] - means[lowest_bin]
                monotonicity = _spearman(
                    summary["bin"].to_numpy(dtype="float64"), summary["mean"].to_numpy(dtype="float64")
                )
                for item in summary.itertuples(index=False):
                    rows.append(
                        {
                            "stage": stage,
                            "date": group_value if by_day else "ALL",
                            "signal": signal,
                            "horizon_ms": horizon_ms,
                            "bin": int(item.bin),
                            "samples": int(item.count),
                            "mean_markout_ticks": float(item.mean),
                            "median_markout_ticks": float(item.median),
                            "naive_standard_error_ticks": (
                                float(item.std / np.sqrt(item.count)) if item.count > 1 else np.nan
                            ),
                            "bin_mean_monotonicity": monotonicity,
                            "top_minus_bottom_ticks": top_bottom,
                            "lowest_observed_bin": lowest_bin,
                            "highest_observed_bin": highest_bin,
                            "observed_bin_count": int(len(summary)),
                        }
                    )
    return pd.DataFrame(rows).sort_values(
        ["signal", "horizon_ms", "date", "bin"], ignore_index=True
    )


def day_stability_report(
    frame: pd.DataFrame,
    spec: dict[str, Any],
    transforms: dict[str, Any],
    stage: str,
) -> pd.DataFrame:
    regimes = apply_regimes(frame, transforms).set_index("date")
    daily_deciles = decile_report(frame, spec, transforms, stage, by_day=True)
    spreads = (
        daily_deciles.groupby(["date", "signal", "horizon_ms"], sort=True)["top_minus_bottom_ticks"]
        .first()
        .to_dict()
    )
    rows = []
    for date, day in frame.groupby("date", sort=True):
        regime = regimes.loc[date]
        for signal in spec["canonical_signals"]:
            for horizon_ms in spec["label_horizons_ms"]:
                label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
                pair = _finite_pair(day, signal, label)
                x = pair[signal].to_numpy(dtype="float64")
                y = pair[label].to_numpy(dtype="float64")
                rows.append(
                    {
                        "stage": stage,
                        "date": date,
                        "signal": signal,
                        "horizon_ms": horizon_ms,
                        "samples": int(len(pair)),
                        "pearson_ic": _pearson(x, y),
                        "spearman_ic": _spearman(x, y),
                        "top_minus_bottom_ticks": spreads.get((date, signal, horizon_ms), np.nan),
                        "spread_regime": regime["spread_regime"],
                        "volatility_regime": regime["volatility_regime"],
                        "weekday": regime["weekday"],
                        "weekend": bool(regime["weekend"]),
                    }
                )
    result = pd.DataFrame(rows)
    signs = (
        result.assign(sign=np.sign(result["pearson_ic"]))
        .groupby(["signal", "horizon_ms"], sort=True)["sign"]
        .agg(lambda values: float(max((values > 0).mean(), (values < 0).mean())))
        .rename("dominant_sign_fraction")
        .reset_index()
    )
    return result.merge(signs, on=["signal", "horizon_ms"], how="left").sort_values(
        ["signal", "horizon_ms", "date"], ignore_index=True
    )


def fit_models(frame: pd.DataFrame, spec: dict[str, Any], transforms: dict[str, Any]) -> dict[str, Any]:
    fitted: dict[str, Any] = {}
    for model_name, features in spec["models"]["specifications"].items():
        for horizon_ms in spec["models"]["horizons_ms"]:
            label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
            selected = frame[features + [label]].dropna()
            x = np.column_stack(
                [
                    (selected[feature].to_numpy(dtype="float64") - transforms["standardization"][feature]["mean"])
                    / transforms["standardization"][feature]["population_std"]
                    for feature in features
                ]
            )
            design = np.column_stack((np.ones(len(selected)), x))
            y = selected[label].to_numpy(dtype="float64")
            coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
            key = f"{model_name}:{horizon_ms}"
            fitted[key] = {
                "model": model_name,
                "horizon_ms": horizon_ms,
                "features": features,
                "training_samples": int(len(selected)),
                "intercept": float(coefficients[0]),
                "standardized_coefficients": {
                    feature: float(value) for feature, value in zip(features, coefficients[1:])
                },
            }
    return fitted


def evaluate_models(
    frame: pd.DataFrame,
    fitted: dict[str, Any],
    transforms: dict[str, Any],
    stage: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    metrics = []
    coefficients = []
    for key in sorted(fitted):
        model = fitted[key]
        features = model["features"]
        horizon_ms = model["horizon_ms"]
        label = f"markout_{WINDOW_NAMES[horizon_ms]}_ticks"
        selected = frame[features + [label]].dropna()
        prediction = np.full(len(selected), model["intercept"], dtype="float64")
        for feature in features:
            standardized = (
                selected[feature].to_numpy(dtype="float64")
                - transforms["standardization"][feature]["mean"]
            ) / transforms["standardization"][feature]["population_std"]
            prediction += standardized * model["standardized_coefficients"][feature]
            coefficients.append(
                {
                    "model": model["model"],
                    "horizon_ms": horizon_ms,
                    "feature": feature,
                    "standardized_coefficient_ticks": model["standardized_coefficients"][feature],
                }
            )
        actual = selected[label].to_numpy(dtype="float64")
        residual = actual - prediction
        total = np.sum((actual - actual.mean()) ** 2)
        nonzero = actual != 0
        metrics.append(
            {
                "stage": stage,
                "model": model["model"],
                "horizon_ms": horizon_ms,
                "samples": int(len(actual)),
                "prediction_ic": _pearson(prediction, actual),
                "r_squared": 1.0 - float(np.sum(residual**2) / total) if total > 0 else np.nan,
                "mae_ticks": float(np.mean(np.abs(residual))),
                "directional_accuracy_nonzero": (
                    float(np.mean(np.sign(prediction[nonzero]) == np.sign(actual[nonzero])))
                    if nonzero.any()
                    else np.nan
                ),
                "prediction_std_ticks": float(np.std(prediction, ddof=0)),
                "actual_std_ticks": float(np.std(actual, ddof=0)),
            }
        )
    return (
        pd.DataFrame(metrics).sort_values(["model", "horizon_ms"], ignore_index=True),
        pd.DataFrame(coefficients).drop_duplicates().sort_values(
            ["model", "horizon_ms", "feature"], ignore_index=True
        ),
    )


def feature_summary(frame: pd.DataFrame, spec: dict[str, Any], stage: str) -> pd.DataFrame:
    rows = []
    for signal in spec["canonical_signals"]:
        values = frame[signal].dropna().to_numpy(dtype="float64")
        rows.append(
            {
                "stage": stage,
                "feature": signal,
                "samples": int(values.size),
                "mean": float(values.mean()),
                "std": float(values.std(ddof=0)),
                "minimum": float(values.min()),
                "p01": float(np.quantile(values, 0.01)),
                "p50": float(np.quantile(values, 0.50)),
                "p99": float(np.quantile(values, 0.99)),
                "maximum": float(values.max()),
            }
        )
    return pd.DataFrame(rows)


def _save_plot(path: Path, figure: plt.Figure) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.tight_layout()
    figure.savefig(path, dpi=140, metadata={"Software": "crypto_hft_like_bot"})
    plt.close(figure)


def make_plots(output: Path, artifacts: EvaluationArtifacts) -> None:
    plot_dir = output / "plots"
    key = artifacts.univariate[artifacts.univariate["signal"].isin(["obi_l1", "obi_l5", "obi_l10"])]
    figure, axis = plt.subplots(figsize=(7, 4))
    for signal, selected in key.groupby("signal", sort=True):
        axis.plot(selected["horizon_ms"], selected["pearson_ic"], marker="o", label=signal)
    axis.axhline(0, color="black", linewidth=0.7)
    axis.set(xlabel="markout horizon (ms)", ylabel="Pearson IC", title="OBI depth IC decay")
    axis.legend()
    _save_plot(plot_dir / "obi_depth_ic_by_horizon.png", figure)

    comparison = artifacts.univariate[
        artifacts.univariate["signal"].isin(["ti_1s", "obi_l10", "normalized_ofi_1s"])
    ]
    figure, axis = plt.subplots(figsize=(7, 4))
    for signal, selected in comparison.groupby("signal", sort=True):
        axis.plot(selected["horizon_ms"], selected["pearson_ic"], marker="o", label=signal)
    axis.axhline(0, color="black", linewidth=0.7)
    axis.set(xlabel="markout horizon (ms)", ylabel="Pearson IC", title="TI vs OBI vs OFI")
    axis.legend()
    _save_plot(plot_dir / "ti_obi_ofi_ic_by_horizon.png", figure)

    decile = artifacts.deciles[
        (artifacts.deciles["signal"] == "obi_l10") & (artifacts.deciles["date"] == "ALL")
    ]
    figure, axis = plt.subplots(figsize=(7, 4))
    for horizon, selected in decile.groupby("horizon_ms", sort=True):
        axis.plot(selected["bin"], selected["mean_markout_ticks"], marker="o", label=f"{horizon}ms")
    axis.axhline(0, color="black", linewidth=0.7)
    axis.set(xlabel="frozen development decile", ylabel="mean markout (ticks)", title="OBI L10 deciles")
    axis.legend()
    _save_plot(plot_dir / "obi_l10_deciles.png", figure)

    daily = artifacts.day_stability[
        (artifacts.day_stability["signal"] == "obi_l10")
        & (artifacts.day_stability["horizon_ms"] == 1000)
    ]
    figure, axis = plt.subplots(figsize=(8, 4))
    axis.bar(daily["date"], daily["pearson_ic"])
    axis.axhline(0, color="black", linewidth=0.7)
    axis.tick_params(axis="x", rotation=35)
    axis.set(ylabel="Pearson IC", title="OBI L10 1s IC by day")
    _save_plot(plot_dir / "obi_l10_1s_ic_by_day.png", figure)

    figure, axis = plt.subplots(figsize=(8, 4))
    axis.bar(daily["date"], daily["top_minus_bottom_ticks"])
    axis.axhline(0, color="black", linewidth=0.7)
    axis.tick_params(axis="x", rotation=35)
    axis.set(ylabel="top-minus-bottom markout (ticks)", title="OBI L10 1s frozen-bin spread by day")
    _save_plot(plot_dir / "obi_l10_1s_top_bottom_by_day.png", figure)

    models = artifacts.model_metrics[artifacts.model_metrics["horizon_ms"] == 1000]
    figure, axis = plt.subplots(figsize=(8, 4))
    axis.bar(models["model"], models["prediction_ic"])
    axis.axhline(0, color="black", linewidth=0.7)
    axis.tick_params(axis="x", rotation=35)
    axis.set(ylabel="predicted/realized IC", title="Simple OLS baselines at 1s")
    _save_plot(plot_dir / "model_prediction_ic_1s.png", figure)


def make_predicted_realized_plot(
    output: Path,
    frame: pd.DataFrame,
    fitted: dict[str, Any],
    transforms: dict[str, Any],
) -> None:
    model = fitted["combined:1000"]
    label = "markout_1s_ticks"
    features = model["features"]
    selected = frame[features + [label]].dropna()
    prediction = np.full(len(selected), model["intercept"], dtype="float64")
    for feature in features:
        prediction += (
            (
                selected[feature].to_numpy(dtype="float64")
                - transforms["standardization"][feature]["mean"]
            )
            / transforms["standardization"][feature]["population_std"]
            * model["standardized_coefficients"][feature]
        )
    realized = selected[label].to_numpy(dtype="float64")
    stride = max(1, len(selected) // 10_000)
    predicted_sample = prediction[::stride]
    realized_sample = realized[::stride]
    lower = float(min(np.quantile(predicted_sample, 0.01), np.quantile(realized_sample, 0.01)))
    upper = float(max(np.quantile(predicted_sample, 0.99), np.quantile(realized_sample, 0.99)))
    figure, axis = plt.subplots(figsize=(6, 6))
    axis.scatter(predicted_sample, realized_sample, s=4, alpha=0.15)
    axis.plot([lower, upper], [lower, upper], color="black", linewidth=0.8)
    axis.set(
        xlim=(lower, upper),
        ylim=(lower, upper),
        xlabel="frozen combined-model prediction (ticks)",
        ylabel="realized 1s markout (ticks)",
        title="Predicted vs realized 1s markout (deterministic sample)",
    )
    _save_plot(output / "plots" / "combined_1s_predicted_vs_realized.png", figure)


def data_quality(frame: pd.DataFrame, spec: dict[str, Any], paths: list[Path], stage: str) -> dict[str, Any]:
    labels = [f"markout_{WINDOW_NAMES[x]}_ticks" for x in spec["label_horizons_ms"]]
    return {
        "schema": "microstructure-data-quality-v1",
        "stage": stage,
        "dates": sorted(frame["date"].unique().tolist()),
        "input_files": [{"path": str(path), "sha256": sha256(path)} for path in paths],
        "rows": int(len(frame)),
        "valid_book_rows": int(frame["valid_book_state"].eq(1).sum()),
        "invalid_book_rows": int(frame["valid_book_state"].ne(1).sum()),
        "feature_segments": int(
            frame[["date", "feature_segment_id"]].dropna().drop_duplicates().shape[0]
        ),
        "valid_labels": {column: int(frame[column].notna().sum()) for column in labels},
        "split_embargo_violations": assert_split_embargo(frame, spec),
    }


def run_evaluation(
    stage: str,
    paths: list[Path],
    spec_path: Path,
    output: Path,
    transforms_path: Path | None,
) -> EvaluationArtifacts:
    spec = load_spec(spec_path)
    frame = load_inputs(paths, spec)
    validate_stage_dates(stage, frame["date"].unique(), spec)

    if stage == "preliminary":
        transforms = fit_transforms(frame, spec)
        fitted = fit_models(frame, spec, transforms)
    elif stage == "development":
        transforms = fit_transforms(frame, spec)
        if transforms_path is None:
            raise ValueError("development stage requires --transforms")
        write_json(transforms_path, transforms)
        fitted = fit_models(frame, spec, transforms)
        write_json(output / "fitted_models.json", {"schema": "microstructure-ols-v1", "models": fitted})
    else:
        if transforms_path is None or not transforms_path.exists():
            raise ValueError(f"{stage} requires development transforms")
        transforms = json.loads(transforms_path.read_text())
        model_path = transforms_path.parent / "fitted_models.json"
        if not model_path.exists():
            model_path = output.parent / "development" / "fitted_models.json"
        fitted = json.loads(model_path.read_text())["models"]

    univariate = univariate_report(frame, spec, stage)
    deciles = decile_report(frame, spec, transforms, stage)
    stability = day_stability_report(frame, spec, transforms, stage)
    model_metrics, coefficients = evaluate_models(frame, fitted, transforms, stage)
    artifacts = EvaluationArtifacts(univariate, deciles, stability, model_metrics, coefficients)

    quality = data_quality(frame, spec, paths, stage)
    write_json(output / "data_quality_report.json", quality)
    write_csv(output / "feature_summary.csv", feature_summary(frame, spec, stage))
    write_csv(output / "univariate_signal_report.csv", univariate)
    write_csv(output / "decile_report.csv", deciles)
    write_csv(output / "horizon_decay_report.csv", univariate[
        ["stage", "signal", "horizon_ms", "samples", "pearson_ic", "spearman_ic", "effect_per_signal_sd_ticks", "hac_slope_t"]
    ])
    write_csv(output / "day_stability_report.csv", stability)
    write_csv(output / "simple_model_report.csv", model_metrics)
    write_csv(output / "simple_model_coefficients.csv", coefficients)
    depth = univariate[univariate["signal"].isin(["obi_l1", "obi_l5", "obi_l10"])]
    write_csv(output / "depth_incremental_value_report.csv", depth)
    write_csv(output / "incremental_value_report.csv", model_metrics)
    write_json(
        output / "run_summary.json",
        {
            "schema": "microstructure-evaluation-summary-v1",
            "stage": stage,
            "spec_path": str(spec_path),
            "spec_sha256": sha256(spec_path),
            "transforms_path": str(transforms_path) if transforms_path else None,
            "transforms_sha256": sha256(transforms_path) if transforms_path and transforms_path.exists() else None,
            "data_quality": quality,
            "report_rows": {
                "univariate": len(univariate),
                "decile": len(deciles),
                "day_stability": len(stability),
                "model_metrics": len(model_metrics),
            },
            "inference": "Newey-West HAC slope SE with Bartlett weights and lag=horizon/grid; naive decile SE separately labeled",
        },
    )
    make_plots(output, artifacts)
    make_predicted_realized_plot(output, frame, fitted, transforms)
    return artifacts


def freeze_spec(draft: Path, frozen: Path) -> str:
    spec = load_spec(draft)
    spec["status"] = "frozen_after_development"
    spec["experiment_id"] = "btc-tardis-first-days-2026-v1"
    write_json(frozen, spec)
    digest = sha256(frozen)
    frozen.with_suffix(frozen.suffix + ".sha256").write_text(f"{digest}  {frozen.name}\n")
    return digest


def git_commit_or_worktree() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def write_oos_audit(
    path: Path,
    spec_path: Path,
    transforms_path: Path,
    manifest_paths: list[Path],
    dates: list[str],
) -> None:
    write_json(
        path,
        {
            "schema": "microstructure-oos-audit-v1",
            "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "dates": dates,
            "frozen_spec": str(spec_path),
            "frozen_spec_sha256": sha256(spec_path),
            "development_transforms_sha256": sha256(transforms_path),
            "code_commit_before_final_research_commit": git_commit_or_worktree(),
            "dataset_manifests": [
                {"path": str(item), "sha256": sha256(item)} for item in manifest_paths
            ],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["preliminary", "development", "validation", "oos"], required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transforms", type=Path)
    args = parser.parse_args()
    run_evaluation(args.stage, args.input, args.spec, args.output, args.transforms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
