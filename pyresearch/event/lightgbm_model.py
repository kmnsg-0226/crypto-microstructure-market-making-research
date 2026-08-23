"""Chronological LightGBM fill/conditional-markout selective maker."""
from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, mean_squared_error, roc_auc_score

from pyresearch.event.common import (
    FLOW_FEATURES,
    FULL_FEATURES,
    PLAN_PATH,
    QUEUE_FEATURES,
    STATIC_FEATURES,
    aggregate_economics,
    load_day,
    load_plan,
    simulate_selected_day,
)
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
OUTPUT_ROOT = ROOT / "data/research/tardis/reports/event_models/lightgbm"
SPEC_PATH = ROOT / "research/specs/lightgbm_selective_maker_frozen.json"
FEATURE_SETS = {
    "static": STATIC_FEATURES,
    "flow": STATIC_FEATURES + FLOW_FEATURES,
    "queue": STATIC_FEATURES + QUEUE_FEATURES,
    "full": FULL_FEATURES,
}
LABEL_COLUMNS = ["fill_label", "label_valid_1s", "maker_markout_1s_ticks"]


def fit_transform_parameters(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    median = np.nanmedian(values, axis=0)
    lower = np.nanpercentile(values, 25, axis=0)
    upper = np.nanpercentile(values, 75, axis=0)
    scale = upper - lower
    scale[~np.isfinite(scale) | (scale < 1e-12)] = 1.0
    median[~np.isfinite(median)] = 0.0
    return median.astype("float32"), scale.astype("float32")


def transform(values: np.ndarray, median: np.ndarray, scale: np.ndarray) -> np.ndarray:
    output = (values.astype("float32") - median) / scale
    output = np.nan_to_num(output, nan=0.0, posinf=10.0, neginf=-10.0)
    return np.clip(output, -10.0, 10.0).astype("float32", copy=False)


def calibration_rows(y: np.ndarray, probability: np.ndarray, fold: int, model: str) -> list[dict[str, Any]]:
    bins = pd.qcut(pd.Series(probability), 10, labels=False, duplicates="drop")
    result = []
    for value in sorted(bins.dropna().unique()):
        mask = bins.eq(value).to_numpy()
        result.append({
            "fold": fold,
            "model": model,
            "decile": int(value) + 1,
            "rows": int(mask.sum()),
            "mean_prediction": float(np.mean(probability[mask])),
            "realized_fill_rate": float(np.mean(y[mask])),
        })
    return result


def predictive_metrics(
    fill_y: np.ndarray,
    fill_probability: np.ndarray,
    markout_y: np.ndarray,
    markout_prediction: np.ndarray,
    markout_mask: np.ndarray,
) -> dict[str, Any]:
    actual = markout_y[markout_mask]
    predicted = markout_prediction[markout_mask]
    rank = spearmanr(actual, predicted).statistic if len(actual) > 1 else np.nan
    return {
        "fill_roc_auc": float(roc_auc_score(fill_y, fill_probability)),
        "fill_log_loss": float(log_loss(fill_y, fill_probability)),
        "fill_brier": float(brier_score_loss(fill_y, fill_probability)),
        "markout_rows": int(len(actual)),
        "markout_mae_ticks": float(mean_absolute_error(actual, predicted)),
        "markout_rmse_ticks": float(np.sqrt(mean_squared_error(actual, predicted))),
        "markout_spearman": float(rank),
        "actual_mean_markout_ticks": float(np.mean(actual)),
        "predicted_mean_markout_ticks": float(np.mean(predicted)),
    }


def _gate(economics: dict[str, Any], day: pd.DataFrame) -> dict[str, Any]:
    gate = load_plan()["development_gate"]
    checks = {
        "pooled_gross_positive": economics["gross_pnl_usdt"] > 0,
        "pooled_net_positive": economics["net_pnl_usdt"] > 0,
        "positive_folds": int((day["net_pnl_usdt"] > 0).sum()) >= int(gate["positive_validation_folds_minimum"]),
        "worst_fold_tolerance": economics["worst_day_net_pnl_usdt"] >= float(gate["worst_fold_net_pnl_usdt_minimum"]),
        "minimum_fills": economics["maker_fill_orders"] >= int(gate["maker_fill_orders_minimum_total"]),
        "zero_inventory_violations": economics["inventory_limit_violations"] == int(gate["inventory_limit_violations"]),
    }
    return {"checks": checks, "passes": bool(all(checks.values()))}


def run_development() -> dict[str, Any]:
    plan = load_plan()
    family = plan["model_families"]["lightgbm"]
    margins = plan["selector"]["model_expected_value_margins_ticks"]
    economics_rows: list[dict[str, Any]] = []
    predictive_rows: list[dict[str, Any]] = []
    calibration: list[dict[str, Any]] = []
    iteration_rows: list[dict[str, Any]] = []

    for fold_number, fold in enumerate(plan["chronological_splits"]["development_folds"], 1):
        train_frames = [load_day(date) for date in fold["train"]]
        train = pd.concat(train_frames, ignore_index=True)
        validation_date = fold["validate"][0]
        validation = load_day(validation_date)
        fill_train = train["fill_label"].to_numpy(dtype="int8")
        fill_validation = validation["fill_label"].to_numpy(dtype="int8")
        markout_train_valid = train["label_valid_1s"].eq(1).to_numpy()
        markout_validation_valid = validation["label_valid_1s"].eq(1).to_numpy()
        markout_train = train["maker_markout_1s_ticks"].to_numpy(dtype="float32")
        markout_validation = validation["maker_markout_1s_ticks"].to_numpy(dtype="float32")

        for ablation, features in FEATURE_SETS.items():
            raw_train = train[features].to_numpy(dtype="float32")
            raw_validation = validation[features].to_numpy(dtype="float32")
            median, scale = fit_transform_parameters(raw_train)
            x_train = transform(raw_train, median, scale)
            x_validation = transform(raw_validation, median, scale)
            del raw_train, raw_validation
            for parameter_index, parameters in enumerate(family["hyperparameter_budget"]):
                common = {
                    **parameters,
                    "n_estimators": family["num_boost_round_max"],
                    "random_state": family["seed"],
                    "n_jobs": 4,
                    "verbosity": -1,
                    "deterministic": True,
                    "force_col_wise": True,
                    "feature_fraction_seed": family["seed"],
                    "bagging_seed": family["seed"],
                    "data_random_seed": family["seed"],
                }
                fill_model = lgb.LGBMClassifier(objective="binary", **common)
                fill_model.fit(
                    x_train,
                    fill_train,
                    eval_set=[(x_validation, fill_validation)],
                    callbacks=[lgb.early_stopping(family["early_stopping_rounds"], verbose=False)],
                )
                markout_model = lgb.LGBMRegressor(objective="huber", **common)
                markout_model.fit(
                    x_train[markout_train_valid],
                    markout_train[markout_train_valid],
                    eval_set=[(x_validation[markout_validation_valid], markout_validation[markout_validation_valid])],
                    callbacks=[lgb.early_stopping(family["early_stopping_rounds"], verbose=False)],
                )
                probability = fill_model.predict_proba(x_validation)[:, 1]
                markout_prediction = markout_model.predict(x_validation)
                expected = probability * markout_prediction
                model_key = f"{ablation}_p{parameter_index}"
                metrics = predictive_metrics(
                    fill_validation,
                    probability,
                    markout_validation,
                    markout_prediction,
                    markout_validation_valid,
                )
                predictive_rows.append({
                    "fold": fold_number,
                    "validation_date": validation_date,
                    "ablation": ablation,
                    "parameter_index": parameter_index,
                    "features": len(features),
                    **metrics,
                })
                calibration.extend(calibration_rows(fill_validation, probability, fold_number, model_key))
                iteration_rows.append({
                    "fold": fold_number,
                    "ablation": ablation,
                    "parameter_index": parameter_index,
                    "fill_best_iteration": int(fill_model.best_iteration_),
                    "markout_best_iteration": int(markout_model.best_iteration_),
                    "train_rows": int(len(train)),
                    "train_fill_rows": int(fill_train.sum()),
                    "validation_rows": int(len(validation)),
                    "validation_fill_rows": int(fill_validation.sum()),
                })
                for margin in margins:
                    model_id = f"lightgbm_{model_key}_margin_{margin:+g}"
                    result = simulate_selected_day(
                        validation,
                        date=validation_date,
                        model_id=model_id,
                        selected=expected >= float(margin),
                    )
                    result.update({
                        "fold": fold_number,
                        "ablation": ablation,
                        "parameter_index": parameter_index,
                        "margin_ticks": float(margin),
                        "selected_fraction": float(np.mean(expected >= float(margin))),
                    })
                    economics_rows.append(result)
                del fill_model, markout_model, probability, markout_prediction, expected
                gc.collect()
            del x_train, x_validation
            gc.collect()
        del train, validation, train_frames
        gc.collect()

    day = pd.DataFrame(economics_rows)
    rankings: list[dict[str, Any]] = []
    keys = ["ablation", "parameter_index", "margin_ticks"]
    for key, group in day.groupby(keys, sort=True):
        model_id = str(group["policy"].iat[0])
        economics = aggregate_economics(group)[model_id]
        rankings.append({
            "ablation": key[0],
            "parameter_index": int(key[1]),
            "margin_ticks": float(key[2]),
            "median_fold_net_pnl_usdt": float(group["net_pnl_usdt"].median()),
            "worst_fold_net_pnl_usdt": float(group["net_pnl_usdt"].min()),
            "selected_fraction": float(group["selected_fraction"].mean()),
            "feature_count": len(FEATURE_SETS[key[0]]),
            **economics,
        })
    ranking = pd.DataFrame(rankings).sort_values(
        ["median_fold_net_pnl_usdt", "worst_fold_net_pnl_usdt", "feature_count", "parameter_index", "margin_ticks"],
        ascending=[False, False, True, True, True],
        kind="stable",
        ignore_index=True,
    )
    selected = ranking.iloc[0]
    selected_mask = (
        day["ablation"].eq(selected["ablation"])
        & day["parameter_index"].eq(int(selected["parameter_index"]))
        & day["margin_ticks"].eq(float(selected["margin_ticks"]))
    )
    selected_day = day.loc[selected_mask].copy()
    selected_id = str(selected_day["policy"].iat[0])
    economics = aggregate_economics(selected_day)[selected_id]
    gate = _gate(economics, selected_day)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT_ROOT / "fold_economics.csv", day)
    write_csv(OUTPUT_ROOT / "ranking.csv", ranking)
    write_csv(OUTPUT_ROOT / "predictive_metrics.csv", pd.DataFrame(predictive_rows))
    write_csv(OUTPUT_ROOT / "calibration.csv", pd.DataFrame(calibration))
    write_csv(OUTPUT_ROOT / "training_iterations.csv", pd.DataFrame(iteration_rows))
    payload = {
        "schema": "lightgbm-selective-maker-development-v1",
        "plan_sha256": sha256(PLAN_PATH),
        "selected": {
            "model_id": selected_id,
            "ablation": str(selected["ablation"]),
            "parameter_index": int(selected["parameter_index"]),
            "parameters": family["hyperparameter_budget"][int(selected["parameter_index"])],
            "margin_ticks": float(selected["margin_ticks"]),
            "features": FEATURE_SETS[str(selected["ablation"])],
        },
        "selected_economics": economics,
        "development_gate": gate,
        "fold_economics_sha256": sha256(OUTPUT_ROOT / "fold_economics.csv"),
        "predictive_metrics_sha256": sha256(OUTPUT_ROOT / "predictive_metrics.csv"),
        "calibration_sha256": sha256(OUTPUT_ROOT / "calibration.csv"),
    }
    write_json(OUTPUT_ROOT / "development_summary.json", payload)
    frozen = {
        "schema": "lightgbm-selective-maker-frozen-v1",
        "status": "survived_development_gate" if gate["passes"] else "rejected_development_gate",
        "plan_sha256": sha256(PLAN_PATH),
        **payload["selected"],
        "transforms": "training_fold_finite_median_IQR_clip_plus_minus_10",
        "fill_target": family["B1"],
        "markout_target": family["B2"],
        "seed": family["seed"],
        "queue_and_execution": plan["execution"],
        "development_dates": plan["chronological_splits"]["development_days"],
        "model_artifact_sha256": None,
        "code_commit_at_plan_freeze": plan["audit"]["repository_commit_before_freeze"],
        "development_gate": gate,
    }
    write_json(SPEC_PATH, frozen)
    return payload


def main() -> None:
    print(json.dumps(run_development(), sort_keys=True))


if __name__ == "__main__":
    main()
