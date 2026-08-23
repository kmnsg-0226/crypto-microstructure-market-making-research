"""Small deterministic causal TCN for fill and conditional maker markout."""
from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, mean_squared_error, roc_auc_score
import torch
from torch import nn
from torch.nn import functional as F

from pyresearch.event.common import PLAN_PATH, aggregate_economics, load_day, load_plan, simulate_selected_day
from pyresearch.event.lightgbm_model import fit_transform_parameters, transform
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
DECLARATION_PATH = ROOT / "research/specs/deep_training_declaration.json"
OUTPUT_ROOT = ROOT / "data/research/tardis/reports/event_models/deep"
SPEC_PATH = ROOT / "research/specs/deep_selective_maker_frozen.json"
SEED = 20260816


def declaration() -> dict[str, Any]:
    value = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    if value["status"] != "frozen_before_deep_model_outcomes":
        raise ValueError("deep training declaration is not frozen")
    if value["parent_plan_sha256"] != sha256(PLAN_PATH):
        raise ValueError("deep declaration parent plan hash mismatch")
    return value


class CausalConv(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int):
        super().__init__()
        self.left_padding = (kernel_size - 1) * dilation
        self.conv = nn.Conv1d(
            in_channels, out_channels, kernel_size, dilation=dilation
        )

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return self.conv(F.pad(values, (self.left_padding, 0)))


class CausalTCN(nn.Module):
    def __init__(self, features: int, channels: list[int], kernel_size: int, dilations: list[int], dropout: float):
        super().__init__()
        layers: list[nn.Module] = []
        previous = features
        for channel, dilation in zip(channels, dilations, strict=True):
            layers.extend([
                CausalConv(previous, channel, kernel_size, dilation),
                nn.ReLU(),
                nn.Dropout(dropout),
            ])
            previous = channel
        self.network = nn.Sequential(*layers)
        self.fill_head = nn.Linear(previous, 1)
        self.markout_head = nn.Linear(previous, 1)

    def forward(self, sequence: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        encoded = self.network(sequence.transpose(1, 2))[:, :, -1]
        return self.fill_head(encoded).squeeze(-1), self.markout_head(encoded).squeeze(-1)


def parameter_count(model: nn.Module) -> int:
    return sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)


def _ordered(frame: pd.DataFrame, features: list[str], median: np.ndarray, scale: np.ndarray) -> dict[str, Any]:
    work = frame.reset_index(drop=True).copy()
    work["_original_index"] = np.arange(len(work), dtype="int64")
    work.sort_values(
        ["date", "side", "decision_local_time_us", "opportunity_id"],
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    boundary = (
        work["date"].ne(work["date"].shift())
        | work["side"].ne(work["side"].shift())
        | work["feature_segment_id"].ne(work["feature_segment_id"].shift())
    ).to_numpy()
    row = np.arange(len(work), dtype="int64")
    starts = np.maximum.accumulate(np.where(boundary, row, 0))
    return {
        "x": transform(work[features].to_numpy(dtype="float32"), median, scale),
        "fill": work["fill_label"].to_numpy(dtype="float32"),
        "markout": work["maker_markout_1s_ticks"].to_numpy(dtype="float32"),
        "markout_valid": work["label_valid_1s"].eq(1).to_numpy(),
        "segment_start": starts,
        "original_index": work["_original_index"].to_numpy(dtype="int64"),
    }


def make_sequences(
    values: np.ndarray,
    indices: np.ndarray,
    segment_start: np.ndarray,
    sequence_length: int,
) -> torch.Tensor:
    # The frozen two-layer TCN has receptive field
    # 1 + (3 - 1) * (1 + 2) = 7 events. Materializing older events cannot
    # affect the final causal output and only wastes CPU/RAM.
    effective_length = min(sequence_length, 7)
    offsets = np.arange(effective_length - 1, -1, -1, dtype="int64")
    positions = indices[:, None] - offsets[None, :]
    valid = positions >= segment_start[indices, None]
    safe = np.maximum(positions, 0)
    sequences = values[safe].copy()
    sequences[~valid] = 0.0
    return torch.from_numpy(sequences)


def evenly_spaced(size: int, cap: int) -> np.ndarray:
    if size <= cap:
        return np.arange(size, dtype="int64")
    return np.linspace(0, size - 1, cap, dtype="int64")


def multitask_loss(
    fill_logits: torch.Tensor,
    markout_prediction: torch.Tensor,
    fill_target: torch.Tensor,
    markout_target: torch.Tensor,
    markout_valid: torch.Tensor,
) -> torch.Tensor:
    loss = F.binary_cross_entropy_with_logits(fill_logits, fill_target)
    if markout_valid.any():
        loss = loss + F.huber_loss(
            markout_prediction[markout_valid], markout_target[markout_valid], delta=1.0
        )
    return loss


def loss_value(
    model: CausalTCN,
    data: dict[str, Any],
    indices: np.ndarray,
    sequence_length: int,
    batch_size: int,
) -> float:
    model.eval()
    total = 0.0
    rows = 0
    with torch.no_grad():
        for start in range(0, len(indices), batch_size):
            selected = indices[start:start + batch_size]
            sequence = make_sequences(data["x"], selected, data["segment_start"], sequence_length)
            fill_logits, markout = model(sequence)
            fill_y = torch.from_numpy(data["fill"][selected])
            valid = torch.from_numpy(data["markout_valid"][selected])
            markout_y = torch.from_numpy(data["markout"][selected])
            loss = multitask_loss(fill_logits, markout, fill_y, markout_y, valid)
            total += float(loss) * len(selected)
            rows += len(selected)
    return total / rows


def train_model(
    train: dict[str, Any],
    validation: dict[str, Any],
    *,
    sequence_length: int,
    family: dict[str, Any],
) -> tuple[CausalTCN, list[dict[str, Any]]]:
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(4)
    model = CausalTCN(
        train["x"].shape[1], family["channels"], family["kernel_size"],
        family["dilations"], family["dropout"],
    )
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=family["learning_rate"], weight_decay=family["weight_decay"]
    )
    train_indices = evenly_spaced(len(train["x"]), family["training_sample_cap_per_fold"])
    validation_indices = evenly_spaced(len(validation["x"]), family["validation_sample_cap_per_day"])
    rng = np.random.default_rng(SEED)
    best_state: dict[str, torch.Tensor] | None = None
    best_loss = np.inf
    stale = 0
    history: list[dict[str, Any]] = []
    for epoch in range(1, family["epochs_max"] + 1):
        shuffled = train_indices.copy()
        rng.shuffle(shuffled)
        model.train()
        total = 0.0
        rows = 0
        for start in range(0, len(shuffled), family["batch_size"]):
            selected = shuffled[start:start + family["batch_size"]]
            sequence = make_sequences(train["x"], selected, train["segment_start"], sequence_length)
            fill_y = torch.from_numpy(train["fill"][selected])
            valid = torch.from_numpy(train["markout_valid"][selected])
            optimizer.zero_grad(set_to_none=True)
            fill_logits, markout = model(sequence)
            markout_y = torch.from_numpy(train["markout"][selected])
            loss = multitask_loss(fill_logits, markout, fill_y, markout_y, valid)
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(selected)
            rows += len(selected)
        validation_loss = loss_value(
            model, validation, validation_indices, sequence_length, family["batch_size"]
        )
        history.append({
            "epoch": epoch,
            "train_loss": total / rows,
            "validation_loss": validation_loss,
            "train_rows": int(len(train_indices)),
            "validation_rows": int(len(validation_indices)),
        })
        print(json.dumps({"sequence_length": sequence_length, **history[-1]}), flush=True)
        if validation_loss < best_loss - 1e-8:
            best_loss = validation_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= family["early_stopping_patience"]:
                break
    if best_state is None:
        raise RuntimeError("deep model produced no best state")
    model.load_state_dict(best_state)
    return model, history


def predict_all(
    model: CausalTCN,
    data: dict[str, Any],
    sequence_length: int,
    batch_size: int = 2048,
) -> tuple[np.ndarray, np.ndarray]:
    probability_ordered = np.empty(len(data["x"]), dtype="float32")
    markout_ordered = np.empty(len(data["x"]), dtype="float32")
    model.eval()
    with torch.no_grad():
        for start in range(0, len(data["x"]), batch_size):
            indices = np.arange(start, min(start + batch_size, len(data["x"])), dtype="int64")
            sequence = make_sequences(data["x"], indices, data["segment_start"], sequence_length)
            logits, markout = model(sequence)
            probability_ordered[indices] = torch.sigmoid(logits).numpy()
            markout_ordered[indices] = markout.numpy()
    probability = np.empty_like(probability_ordered)
    markout = np.empty_like(markout_ordered)
    probability[data["original_index"]] = probability_ordered
    markout[data["original_index"]] = markout_ordered
    return probability, markout


def _predictive(frame: pd.DataFrame, probability: np.ndarray, prediction: np.ndarray) -> dict[str, Any]:
    fill = frame["fill_label"].to_numpy(dtype="int8")
    valid = frame["label_valid_1s"].eq(1).to_numpy()
    actual = frame["maker_markout_1s_ticks"].to_numpy(dtype="float32")[valid]
    predicted = prediction[valid]
    return {
        "fill_roc_auc": float(roc_auc_score(fill, probability)),
        "fill_log_loss": float(log_loss(fill, probability)),
        "fill_brier": float(brier_score_loss(fill, probability)),
        "markout_rows": int(len(actual)),
        "markout_mae_ticks": float(mean_absolute_error(actual, predicted)),
        "markout_rmse_ticks": float(np.sqrt(mean_squared_error(actual, predicted))),
        "markout_spearman": float(spearmanr(actual, predicted).statistic),
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
    declared = declaration()
    family = plan["model_families"]["deep"]
    features = declared["compact_event_features"]
    base_columns = list(dict.fromkeys(features + [
        "date", "side", "decision_local_time_us", "opportunity_id", "feature_segment_id",
        "fill_label", "label_valid_1s", "maker_markout_1s_ticks",
    ]))
    economics_rows: list[dict[str, Any]] = []
    predictive_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    parameter_counts: dict[str, int] = {}
    for fold_number, fold in enumerate(plan["chronological_splits"]["development_folds"], 1):
        training_frame = pd.concat(
            [load_day(date, base_columns) for date in fold["train"]], ignore_index=True
        )
        validation_date = fold["validate"][0]
        validation_frame = load_day(validation_date)
        median, scale = fit_transform_parameters(training_frame[features].to_numpy(dtype="float32"))
        train_data = _ordered(training_frame, features, median, scale)
        validation_data = _ordered(validation_frame, features, median, scale)
        cached_probability: np.ndarray | None = None
        cached_markout: np.ndarray | None = None
        cached_history: list[dict[str, Any]] | None = None
        cached_count: int | None = None
        for sequence_length in family["sequence_lengths_development"]:
            if cached_probability is None:
                model, history = train_model(
                    train_data, validation_data,
                    sequence_length=sequence_length, family=family,
                )
                count = parameter_count(model)
                probability, markout = predict_all(model, validation_data, sequence_length)
                cached_probability = probability.copy()
                cached_markout = markout.copy()
                cached_history = history
                cached_count = count
                del model
            else:
                # Both frozen candidates exceed the exact seven-event receptive
                # field, so with the same seed and samples they are the same model.
                probability = cached_probability.copy()
                markout = cached_markout.copy()
                history = list(cached_history or [])
                count = int(cached_count)
            parameter_counts[str(sequence_length)] = count
            for row in history:
                history_rows.append({
                    "fold": fold_number,
                    "sequence_length": sequence_length,
                    "reused_receptive_field_equivalent": sequence_length != family["sequence_lengths_development"][0],
                    **row,
                })
            expected = probability * markout
            predictive_rows.append({
                "fold": fold_number,
                "validation_date": validation_date,
                "sequence_length": sequence_length,
                "parameter_count": count,
                **_predictive(validation_frame, probability, markout),
            })
            for margin in plan["selector"]["model_expected_value_margins_ticks"]:
                model_id = f"deep_tcn_{sequence_length}_margin_{margin:+g}"
                result = simulate_selected_day(
                    validation_frame,
                    date=validation_date,
                    model_id=model_id,
                    selected=expected >= float(margin),
                )
                result.update({
                    "fold": fold_number,
                    "sequence_length": sequence_length,
                    "margin_ticks": float(margin),
                    "selected_fraction": float(np.mean(expected >= float(margin))),
                })
                economics_rows.append(result)
            del probability, markout, expected
            gc.collect()
        del training_frame, validation_frame, train_data, validation_data
        gc.collect()

    day = pd.DataFrame(economics_rows)
    rankings = []
    for key, group in day.groupby(["sequence_length", "margin_ticks"], sort=True):
        model_id = str(group["policy"].iat[0])
        economics = aggregate_economics(group)[model_id]
        rankings.append({
            "sequence_length": int(key[0]),
            "margin_ticks": float(key[1]),
            "median_fold_net_pnl_usdt": float(group["net_pnl_usdt"].median()),
            "worst_fold_net_pnl_usdt": float(group["net_pnl_usdt"].min()),
            "selected_fraction": float(group["selected_fraction"].mean()),
            "parameter_count": parameter_counts[str(int(key[0]))],
            **economics,
        })
    ranking = pd.DataFrame(rankings).sort_values(
        ["median_fold_net_pnl_usdt", "worst_fold_net_pnl_usdt", "parameter_count", "sequence_length", "margin_ticks"],
        ascending=[False, False, True, True, True], kind="stable", ignore_index=True,
    )
    selected = ranking.iloc[0]
    selected_day = day.loc[
        day["sequence_length"].eq(int(selected["sequence_length"]))
        & day["margin_ticks"].eq(float(selected["margin_ticks"]))
    ].copy()
    selected_id = str(selected_day["policy"].iat[0])
    economics = aggregate_economics(selected_day)[selected_id]
    gate = _gate(economics, selected_day)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT_ROOT / "fold_economics.csv", day)
    write_csv(OUTPUT_ROOT / "ranking.csv", ranking)
    write_csv(OUTPUT_ROOT / "predictive_metrics.csv", pd.DataFrame(predictive_rows))
    write_csv(OUTPUT_ROOT / "training_history.csv", pd.DataFrame(history_rows))
    payload = {
        "schema": "deep-selective-maker-development-v1",
        "plan_sha256": sha256(PLAN_PATH),
        "training_declaration_sha256": sha256(DECLARATION_PATH),
        "selected": {
            "model_id": selected_id,
            "sequence_length": int(selected["sequence_length"]),
            "margin_ticks": float(selected["margin_ticks"]),
            "features": features,
            "parameter_count": int(selected["parameter_count"]),
        },
        "selected_economics": economics,
        "development_gate": gate,
        "fold_economics_sha256": sha256(OUTPUT_ROOT / "fold_economics.csv"),
        "predictive_metrics_sha256": sha256(OUTPUT_ROOT / "predictive_metrics.csv"),
    }
    write_json(OUTPUT_ROOT / "development_summary.json", payload)
    frozen = {
        "schema": "deep-selective-maker-frozen-v1",
        "status": "survived_development_gate" if gate["passes"] else "rejected_development_gate",
        "plan_sha256": sha256(PLAN_PATH),
        "training_declaration_sha256": sha256(DECLARATION_PATH),
        **payload["selected"],
        "architecture": family,
        "transforms": "training_fold_finite_median_IQR_clip_plus_minus_10",
        "seed": SEED,
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
