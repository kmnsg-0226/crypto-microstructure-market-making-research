"""Model comparison, incremental-information audit and the magnitude model.

Every model here shares the chronological fold geometry of phases 2, 3, 4A and 4B, with one
unavoidable difference: the sweep probability is only out of fold inside a validation block, so
the first scored block has no scored past to learn from and is used for training only. The
directional models therefore run on nine folds rather than ten, and that is stated rather than
hidden.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.directional import data, spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.predictive import modeling
from pyresearch.native.predictive.modeling import Problem

KEYS = ["timestamp_ns", "file_index", "segment_id", "side"]


def directional_folds() -> list:
    """Phase 2 folds, minus the first: its validation block is this phase's training seed."""
    folds = predictive_data.build_folds(
        predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy()
    )
    return [fold for fold in folds if fold.block > spec.FIRST_VALIDATION_BLOCK]


def model_frame(frame: pd.DataFrame) -> pd.DataFrame:
    step = int(spec.MODEL_GRID_MS * 1e6)
    return frame[frame["timestamp_ns"] % step == 0].reset_index(drop=True)


def add_model_targets(frame: pd.DataFrame) -> pd.DataFrame:
    """Binary direction targets. An exactly flat markout is a tie, not a wrong call."""
    for horizon in (250, 500, 1000):
        markout = frame[f"directional_markout_{horizon}ms"].to_numpy(dtype="float64")
        frame[f"directional_sign_{horizon}ms"] = np.where(
            np.isfinite(markout) & (markout != 0), (markout > 0).astype("float64"), np.nan
        )
        frame[f"directional_tie_{horizon}ms"] = np.where(
            np.isfinite(markout), (markout == 0).astype("float64"), np.nan
        )
    return frame


def classification_targets() -> list[str]:
    return ["next_move_matches_sweep_direction"] + [
        f"directional_sign_{h}ms" for h in (250, 500, 1000)
    ]


# --------------------------------------------------------------------------------------------
# Model-set comparison
# --------------------------------------------------------------------------------------------
def run_comparison(frame: pd.DataFrame, folds: list):
    """Fit every feature set on every target once, and keep the out-of-fold predictions.

    The nested audit needs the two largest of these again, so they are handed back rather than
    refitted: the same rows, the same folds and the same seed would only reproduce them.
    """
    pooled: list[dict] = []
    per_fold: list[pd.DataFrame] = []
    calibration: list[pd.DataFrame] = []
    predictions: dict[tuple[str, str], pd.DataFrame] = {}
    for target in classification_targets():
        for name in spec.MODEL_SETS:
            problem = Problem(
                name=f"{target}__{name}",
                family=target,
                target=target,
                task="classification",
                features=data.model_features(name),
                description=f"{target} from {name}",
                tags={"target": target, "feature_set": name},
            )
            oof, fold_metrics, _ = modeling.run_problem(problem, frame, folds)
            if oof.empty:
                continue
            per_fold.append(fold_metrics)
            pooled.extend(modeling.pooled_metrics(problem, oof))
            for estimator in ("linear", "lightgbm"):
                calibration.append(modeling.calibration(problem, oof, estimator))
            predictions[(target, name)] = oof
            print(f"  {target} / {name}: {len(oof):,} OOF rows")
    return (
        pd.DataFrame(pooled),
        pd.concat(per_fold, ignore_index=True) if per_fold else pd.DataFrame(),
        pd.concat(calibration, ignore_index=True) if calibration else pd.DataFrame(),
        predictions,
    )


# --------------------------------------------------------------------------------------------
# Incremental information audit
# --------------------------------------------------------------------------------------------
def nested_audit(frame: pd.DataFrame, folds: list, existing: dict | None = None) -> pd.DataFrame:
    """Nested comparisons: does the sweep probability survive the named controls?"""
    existing = existing or {}
    rows = []
    for target in classification_targets():
        blocks = {
            name: existing[(target, name)]
            for name in ("book_flow", "book_flow_plus_sweep")
            if (target, name) in existing
        }
        for name, columns in (
            ("controls", spec.CONTROL_FEATURES),
            ("controls_plus_sweep", spec.CONTROL_FEATURES + spec.SWEEP_ONLY),
        ):
            problem = Problem(
                name=f"{target}__{name}",
                family="nested",
                target=target,
                task="classification",
                features=[c for c in columns if c in frame.columns],
                description=name,
                tags={"target": target, "nest": name},
            )
            oof, _, _ = modeling.run_problem(problem, frame, folds)
            if not oof.empty:
                blocks[name] = oof
        for base, extended in (
            ("controls", "controls_plus_sweep"),
            ("book_flow", "book_flow_plus_sweep"),
        ):
            if base not in blocks or extended not in blocks:
                continue
            rows.extend(_delta(target, base, extended, blocks[base], blocks[extended]))
    return pd.DataFrame(rows)


def _delta(target, base_name, extended_name, base, extended) -> list[dict]:
    merged = base.merge(
        extended, on=[c for c in KEYS if c in base.columns] + ["fold"], suffixes=("_a", "_b")
    )
    out = []
    for estimator in ("linear", "lightgbm"):
        y = merged["y_a"].to_numpy(dtype="float64")
        keep = np.isfinite(y)
        y = y[keep]
        first = merged[f"pred_{estimator}_a"].to_numpy(dtype="float64")[keep]
        second = merged[f"pred_{estimator}_b"].to_numpy(dtype="float64")[keep]
        a = modeling.classification_metrics(y, first)
        b = modeling.classification_metrics(y, second)
        block_id = (
            merged["timestamp_ns"].to_numpy()[keep]
            // int(spec.BOOTSTRAP_BLOCK_MINUTES * 60 * 1e9)
        )
        deltas, sizes = [], []
        for _, index in pd.Series(np.arange(y.size)).groupby(block_id):
            take = index.to_numpy()
            if take.size < 200 or len(np.unique(y[take])) < 2:
                continue
            deltas.append(
                modeling.classification_metrics(y[take], second[take])["roc_auc"]
                - modeling.classification_metrics(y[take], first[take])["roc_auc"]
            )
            sizes.append(take.size)
        values = np.array(deltas, dtype="float64")
        weights = np.array(sizes, dtype="float64")
        centre, low, high = modeling.block_bootstrap(values, weights)
        out.append(
            {
                "target": target,
                "estimator": estimator,
                "base": base_name,
                "extended": extended_name,
                "n": int(y.size),
                "base_roc_auc": a["roc_auc"],
                "extended_roc_auc": b["roc_auc"],
                "delta_roc_auc": b["roc_auc"] - a["roc_auc"],
                "base_log_loss": a["log_loss"],
                "extended_log_loss": b["log_loss"],
                "delta_log_loss": b["log_loss"] - a["log_loss"],
                "base_brier": a["brier"],
                "extended_brier": b["brier"],
                "delta_brier": b["brier"] - a["brier"],
                "blocks": int(values.size),
                "block_mean_delta_auc": centre,
                "block_median_delta_auc": float(np.median(values)) if values.size else np.nan,
                "block_worst_delta_auc": float(values.min()) if values.size else np.nan,
                "frac_blocks_positive_delta": float((values > 0).mean())
                if values.size
                else np.nan,
                "block_bootstrap_p05": low,
                "block_bootstrap_p95": high,
            }
        )
    return out


def residual_diagnostic(frame: pd.DataFrame, folds: list) -> pd.DataFrame:
    """Sweep probability residualised against the controls, as a diagnostic only.

    Residualising says nothing causal. It is here to show how much of the sweep score is a
    linear function of variables that were already available.
    """
    from sklearn.linear_model import Ridge

    columns = [c for c in spec.CONTROL_FEATURES if c in frame.columns]
    stamps = frame["timestamp_ns"].to_numpy()
    target = frame["sweep_p"].to_numpy(dtype="float64")
    features = frame[columns].to_numpy(dtype="float32")
    rows = []
    for fold in folds:
        train = stamps <= fold.train_end_ns
        validation = (stamps >= fold.validation_start_ns) & (stamps < fold.validation_end_ns)
        if train.sum() < 1000 or validation.sum() == 0:
            continue
        pre = modeling.LinearPreprocessor.fit(features[train])
        model = Ridge(alpha=1.0)
        model.fit(pre.transform(features[train]), target[train])
        fitted = model.predict(pre.transform(features[validation]))
        actual = target[validation]
        residual = actual - fitted
        row = {
            "fold": fold.index,
            "validation_rows": int(validation.sum()),
            "r2_of_sweep_on_controls": float(
                1 - ((actual - fitted) ** 2).sum() / ((actual - actual.mean()) ** 2).sum()
            ),
            "corr_sweep_fitted": float(np.corrcoef(actual, fitted)[0, 1]),
        }
        for name in ("next_move_matches_sweep_direction", "directional_sign_500ms"):
            outcome = frame[name].to_numpy(dtype="float64")[validation]
            keep = np.isfinite(outcome)
            if keep.sum() > 100:
                row[f"corr_residual_{name}"] = float(
                    np.corrcoef(residual[keep], outcome[keep])[0, 1]
                )
                row[f"corr_sweep_{name}"] = float(
                    np.corrcoef(actual[keep], outcome[keep])[0, 1]
                )
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Magnitude model
# --------------------------------------------------------------------------------------------
def magnitude_models(frame: pd.DataFrame, folds: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    pooled: list[dict] = []
    buckets: list[pd.DataFrame] = []
    for horizon in (spec.PRIMARY_HORIZON_MS, spec.SECONDARY_HORIZON_MS):
        target = f"directional_markout_{horizon}ms"
        for name in ("book_flow", "book_flow_plus_sweep"):
            problem = Problem(
                name=f"{target}__{name}",
                family="magnitude",
                target=target,
                task="regression",
                features=data.model_features(name),
                description=f"{target} from {name}",
                tags={"target": target, "feature_set": name, "horizon_ms": str(horizon)},
            )
            oof, _, _ = modeling.run_problem(problem, frame, folds)
            if oof.empty:
                continue
            pooled.extend(modeling.pooled_metrics(problem, oof))
            buckets.append(_prediction_buckets(problem, oof, horizon, name))
            print(f"  magnitude {target} / {name}: {len(oof):,} OOF rows")
    return (
        pd.DataFrame(pooled),
        pd.concat(buckets, ignore_index=True) if buckets else pd.DataFrame(),
    )


def _prediction_buckets(problem, oof, horizon, feature_set) -> pd.DataFrame:
    """Realised movement by predicted-movement decile, with the tails shown explicitly."""
    rows = []
    for estimator in ("linear", "lightgbm"):
        scored = oof[np.isfinite(oof["y"].to_numpy())].copy()
        prediction = scored[f"pred_{estimator}"].to_numpy(dtype="float64")
        try:
            bucket = pd.qcut(prediction, 10, labels=False, duplicates="drop")
        except ValueError:
            continue
        scored = scored.assign(bucket=bucket)
        for value, part in scored.groupby("bucket", sort=True):
            realised = part["y"].to_numpy(dtype="float64")
            rows.append(
                {
                    "target": problem.target,
                    "horizon_ms": horizon,
                    "feature_set": feature_set,
                    "estimator": estimator,
                    "bucket": int(value),
                    "observations": len(part),
                    "mean_prediction_ticks": float(part[f"pred_{estimator}"].mean()),
                    "mean_realised_ticks": float(realised.mean()),
                    "median_realised_ticks": float(np.median(realised)),
                    "p5_realised_ticks": float(np.quantile(realised, 0.05)),
                    "p95_realised_ticks": float(np.quantile(realised, 0.95)),
                    "frac_realised_positive": float((realised > 0).mean()),
                    "sign_accuracy": float(
                        ((part[f"pred_{estimator}"].to_numpy() > 0) == (realised > 0)).mean()
                    ),
                }
            )
    return pd.DataFrame(rows)
