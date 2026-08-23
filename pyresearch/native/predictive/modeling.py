"""Blocked out-of-fold estimation for the native predictive decomposition.

Every model is fitted on a contiguous past window and scored on the next contiguous block, with
a purge longer than any target horizon between them. There is no random split anywhere, and a
row's out-of-fold prediction always comes from a model that never saw that row or any later row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import average_precision_score, roc_auc_score

from pyresearch.native.predictive import data, spec

MODELS = ("naive", "linear", "lightgbm")


@dataclass(frozen=True)
class Problem:
    name: str
    family: str  # price | fill | adverse
    target: str
    task: str  # classification | regression
    features: list[str]
    description: str
    # Rows the model may learn from. Predictions are produced for every validation row so that
    # downstream joint diagnostics can use them even where the label is censored.
    train_mask: Callable[[pd.DataFrame], np.ndarray] | None = None
    # Rows this problem may be scored on. Defaults to every labelled row; a side-restricted
    # twin of a pooled problem must not be graded on the side it never trained for.
    score_mask: Callable[[pd.DataFrame], np.ndarray] | None = None
    tags: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Preprocessing
# --------------------------------------------------------------------------------------------
@dataclass
class LinearPreprocessor:
    lower: np.ndarray
    upper: np.ndarray
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, values: np.ndarray) -> "LinearPreprocessor":
        low, high = spec.CLIP_QUANTILES
        # Clip bounds and the fill median are order statistics, so they are estimated from an
        # evenly spaced stride through the training fold rather than by sorting every row of
        # every column. The stride is deterministic and chronology-preserving, and at this
        # sample size it moves a 0.1 % quantile by far less than one feature bin.
        step = max(1, values.shape[0] // spec.PREPROCESSOR_SAMPLE_ROWS)
        sample = values[::step]
        lower = np.nanquantile(sample, low, axis=0).astype("float32")
        upper = np.nanquantile(sample, high, axis=0).astype("float32")
        median = np.nanmedian(np.clip(sample, lower, upper), axis=0).astype("float32")
        median = np.where(np.isfinite(median), median, np.float32(0.0))
        filled = np.where(np.isnan(sample), median, np.clip(sample, lower, upper))
        mean = filled.mean(axis=0, dtype="float64").astype("float32")
        scale = filled.std(axis=0, dtype="float64").astype("float32")
        scale = np.where(scale > 0, scale, np.float32(1.0))
        return cls(lower, upper, median, mean, scale)

    def transform(self, values: np.ndarray) -> np.ndarray:
        out = np.clip(values, self.lower, self.upper)
        np.copyto(out, np.broadcast_to(self.median, out.shape), where=np.isnan(out))
        out -= self.mean
        out /= self.scale
        return out


# --------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------
def classification_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    base = float(y.mean()) if y.size else np.nan
    auc = np.nan
    average_precision = np.nan
    if y.size and 0 < base < 1:
        auc = float(roc_auc_score(y, p))
        # Reported alongside ROC AUC because several targets here are heavily imbalanced, where
        # ROC AUC flatters a model that never finds the minority class.
        average_precision = float(average_precision_score(y, p))
    return {
        "n": int(y.size),
        "positives": int(y.sum()),
        "base_rate": base,
        "roc_auc": auc,
        "pr_auc": average_precision,
        "pr_auc_lift": average_precision / base if base else np.nan,
        "log_loss": float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()) if y.size else np.nan,
        "brier": float(((p - y) ** 2).mean()) if y.size else np.nan,
        "accuracy": float(((p >= 0.5) == (y > 0.5)).mean()) if y.size else np.nan,
    }


def regression_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    if y.size == 0:
        return {"n": 0}
    error = p - y
    total = ((y - y.mean()) ** 2).sum()
    spearman = np.nan
    if y.size > 2 and np.std(p) > 0 and np.std(y) > 0:
        spearman = float(
            np.corrcoef(pd.Series(p).rank().to_numpy(), pd.Series(y).rank().to_numpy())[0, 1]
        )
    return {
        "n": int(y.size),
        "mean_target": float(y.mean()),
        "median_target": float(np.median(y)),
        "mae": float(np.abs(error).mean()),
        "median_abs_error": float(np.median(np.abs(error))),
        "rmse": float(np.sqrt((error**2).mean())),
        "spearman": spearman,
        "sign_accuracy": float(((p > 0) == (y > 0)).mean()),
        "r2": float(1 - (error**2).sum() / total) if total > 0 else np.nan,
    }


def score(task: str, y: np.ndarray, p: np.ndarray) -> dict:
    return classification_metrics(y, p) if task == "classification" else regression_metrics(y, p)


def primary_metric(task: str) -> str:
    return "roc_auc" if task == "classification" else "spearman"


def block_bootstrap(
    values: np.ndarray,
    weights: np.ndarray,
    seed: int = spec.SEED,
    draws: int = spec.BOOTSTRAP_DRAWS,
) -> tuple[float, float, float]:
    """Resample whole time blocks, never rows: neighbouring 100 ms rows are near-duplicates."""
    finite = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    values = values[finite]
    weights = weights[finite]
    if values.size < 2:
        return (float("nan"),) * 3
    rng = np.random.default_rng(seed)
    means = np.empty(draws)
    count = values.size
    for draw in range(draws):
        index = rng.integers(0, count, count)
        means[draw] = np.average(values[index], weights=weights[index])
    centre = float(np.average(values, weights=weights))
    return centre, float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


# --------------------------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------------------------
def _fit_predict(
    task: str,
    model: str,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_valid: np.ndarray,
) -> tuple[np.ndarray, dict[str, float] | None]:
    if model == "naive":
        constant = float(y_train.mean()) if task == "classification" else float(
            np.median(y_train)
        )
        return np.full(x_valid.shape[0], constant, dtype="float64"), None
    if model == "linear":
        pre = LinearPreprocessor.fit(x_train)
        train = pre.transform(x_train)
        valid = pre.transform(x_valid)
        if task == "classification":
            estimator = LogisticRegression(**spec.LOGISTIC_PARAMS)
            estimator.fit(train, y_train)
            coefficients = estimator.coef_[0]
            return estimator.predict_proba(valid)[:, 1], dict(
                zip(range(len(coefficients)), coefficients.astype(float))
            )
        estimator = Ridge(**spec.RIDGE_PARAMS)
        estimator.fit(train, y_train)
        return estimator.predict(valid), dict(
            zip(range(len(estimator.coef_)), estimator.coef_.astype(float))
        )
    params = spec.LGBM_CLASSIFICATION if task == "classification" else spec.LGBM_REGRESSION
    dataset = lgb.Dataset(x_train, label=y_train, free_raw_data=True)
    booster = lgb.train(params, dataset, num_boost_round=spec.LGBM_ROUNDS)
    gains = booster.feature_importance(importance_type="gain")
    return booster.predict(x_valid), dict(zip(range(len(gains)), gains.astype(float)))


def run_problem(
    problem: Problem,
    frame: pd.DataFrame,
    folds: list[data.Fold],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fit every model on every fold and return OOF predictions, fold metrics and coefficients.

    ``frame`` must already carry the problem's features, target and the meta columns used for
    reporting. Rows are assumed sorted by ``timestamp_ns``.
    """
    timestamps = frame["timestamp_ns"].to_numpy()
    target = frame[problem.target].to_numpy(dtype="float64")
    labelled = np.isfinite(target)
    trainable = labelled.copy()
    if problem.train_mask is not None:
        trainable &= problem.train_mask(frame)
    if problem.score_mask is not None:
        # Predictions are still produced everywhere for downstream joint diagnostics; only the
        # label is withheld, so every metric and every pooled score respects the population.
        target = np.where(problem.score_mask(frame), target, np.nan)
    features = frame[problem.features].to_numpy(dtype="float32")

    oof_rows: list[pd.DataFrame] = []
    metric_rows: list[dict] = []
    weight_rows: list[dict] = []

    for fold in folds:
        train = trainable & (timestamps <= fold.train_end_ns)
        validation = (timestamps >= fold.validation_start_ns) & (
            timestamps < fold.validation_end_ns
        )
        if train.sum() < 1000 or validation.sum() == 0:
            continue
        x_train = features[train]
        y_train = target[train]
        x_valid = features[validation]

        block = pd.DataFrame(
            {
                "timestamp_ns": timestamps[validation],
                "fold": fold.index,
                "y": target[validation],
            }
        )
        for extra in ("file_index", "segment_id", "side"):
            if extra in frame.columns:
                block[extra] = frame[extra].to_numpy()[validation]
        for model in MODELS:
            prediction, weights = _fit_predict(
                problem.task, model, x_train, y_train, x_valid
            )
            block[f"pred_{model}"] = prediction
            if weights is not None:
                for index, value in weights.items():
                    weight_rows.append(
                        {
                            "problem": problem.name,
                            "model": model,
                            "fold": fold.index,
                            "feature": problem.features[index],
                            "value": value,
                        }
                    )
            scored = np.isfinite(block["y"].to_numpy())
            metrics = score(
                problem.task,
                block["y"].to_numpy()[scored],
                prediction[scored],
            )
            metric_rows.append(
                {
                    "problem": problem.name,
                    "family": problem.family,
                    "task": problem.task,
                    "model": model,
                    "fold": fold.index,
                    "train_rows": int(train.sum()),
                    "validation_rows": int(validation.sum()),
                    "validation_start_utc": data._utc(fold.validation_start_ns),
                    **problem.tags,
                    **metrics,
                }
            )
        oof_rows.append(block)

    oof = pd.concat(oof_rows, ignore_index=True) if oof_rows else pd.DataFrame()
    return oof, pd.DataFrame(metric_rows), pd.DataFrame(weight_rows)


def pooled_metrics(problem: Problem, oof: pd.DataFrame) -> list[dict]:
    """Pooled score plus the dependence-aware block summary for every model."""
    rows = []
    if oof.empty:
        return rows
    scored = oof[np.isfinite(oof["y"].to_numpy())]
    if scored.empty:
        return rows
    minutes = spec.BOOTSTRAP_BLOCK_MINUTES
    block_id = (scored["timestamp_ns"].to_numpy() // int(minutes * 60 * 1e9)).astype("int64")
    for model in MODELS:
        prediction = scored[f"pred_{model}"].to_numpy(dtype="float64")
        y = scored["y"].to_numpy(dtype="float64")
        pooled = score(problem.task, y, prediction)
        metric = primary_metric(problem.task)

        per_block: list[float] = []
        sizes: list[int] = []
        for _, index in pd.Series(np.arange(len(scored))).groupby(block_id):
            take = index.to_numpy()
            if take.size < 200:
                continue
            value = score(problem.task, y[take], prediction[take]).get(metric, np.nan)
            per_block.append(value)
            sizes.append(take.size)
        blocks = np.array(per_block, dtype="float64")
        weights = np.array(sizes, dtype="float64")
        centre, low, high = block_bootstrap(blocks, weights)
        reference = 0.5 if problem.task == "classification" else 0.0
        rows.append(
            {
                "problem": problem.name,
                "family": problem.family,
                "task": problem.task,
                "model": model,
                **problem.tags,
                **pooled,
                "primary_metric": metric,
                "blocks": int(np.isfinite(blocks).sum()),
                "block_mean": centre,
                "block_median": float(np.nanmedian(blocks)) if blocks.size else np.nan,
                "block_worst": float(np.nanmin(blocks)) if blocks.size else np.nan,
                "block_best": float(np.nanmax(blocks)) if blocks.size else np.nan,
                "block_frac_expected_sign": (
                    float(np.nanmean(blocks > reference)) if blocks.size else np.nan
                ),
                "block_bootstrap_p05": low,
                "block_bootstrap_p95": high,
            }
        )
    return rows


def calibration(problem: Problem, oof: pd.DataFrame, model: str) -> pd.DataFrame:
    """Predicted-versus-realised table, the only honest way to read a probability model."""
    scored = oof[np.isfinite(oof["y"].to_numpy())].copy()
    if scored.empty:
        return pd.DataFrame()
    prediction = scored[f"pred_{model}"]
    try:
        bucket = pd.qcut(
            prediction, spec.CALIBRATION_BINS, labels=False, duplicates="drop"
        )
    except ValueError:
        return pd.DataFrame()
    grouped = scored.assign(bucket=bucket).groupby("bucket", observed=True)
    table = grouped.agg(
        observations=("y", "size"),
        predicted=(f"pred_{model}", "mean"),
        realised=("y", "mean"),
    ).reset_index()
    table.insert(0, "model", model)
    table.insert(0, "problem", problem.name)
    return table
