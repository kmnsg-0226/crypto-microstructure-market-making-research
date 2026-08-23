"""Broad post-V1 passive quote-filter exploration on frozen probe labels.

This module deliberately separates development ranking from retrospective
replication.  It does not alter fill labels, fit alpha, simulate inventory, or
claim that already-seen June/July/August dates are a new unseen holdout.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.execution.engine import frozen_prediction
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/passive_approach_exploration_spec.json"
COMBINATION_SPEC_PATH = ROOT / "research/specs/passive_approach_combinations_spec.json"
V1_SPEC_PATH = ROOT / "research/specs/maker_research_spec_frozen.json"
FEATURE_ROOT = ROOT / "data/research/tardis"
PASSIVE_ROOT = FEATURE_ROOT / "passive"
REPORT_ROOT = FEATURE_ROOT / "reports/passive/approach_exploration"
TRANSFORMS_PATH = FEATURE_ROOT / "reports/development/development_transforms.json"
MODELS_PATH = FEATURE_ROOT / "reports/development/fitted_models.json"
V1_THRESHOLDS_PATH = FEATURE_ROOT / "reports/passive/development_signal_thresholds.json"
THRESHOLDS_PATH = REPORT_ROOT / "thresholds.json"
POLICY_CATALOG_PATH = REPORT_ROOT / "policy_catalog.csv"
DEVELOPMENT_DAY_PATH = REPORT_ROOT / "development_policy_day_metrics.csv"
DEVELOPMENT_SPLIT_PATH = REPORT_ROOT / "development_policy_metrics.csv"
RANKING_PATH = REPORT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = REPORT_ROOT / "shortlist_frozen_before_replication.json"
REPLICATION_DAY_PATH = REPORT_ROOT / "replication_policy_day_metrics.csv"
REPLICATION_SPLIT_PATH = REPORT_ROOT / "replication_policy_metrics.csv"
COMPARISON_PATH = REPORT_ROOT / "all_policy_split_comparison.csv"
SENSITIVITY_DAY_PATH = REPORT_ROOT / "shortlist_sensitivity_day.csv"
SENSITIVITY_PATH = REPORT_ROOT / "shortlist_sensitivity.csv"
COMBINATION_ROOT = REPORT_ROOT / "combinations"
COMBINATION_CATALOG_PATH = COMBINATION_ROOT / "policy_catalog.csv"
COMBINATION_DEVELOPMENT_DAY_PATH = COMBINATION_ROOT / "development_policy_day_metrics.csv"
COMBINATION_DEVELOPMENT_PATH = COMBINATION_ROOT / "development_policy_metrics.csv"
COMBINATION_RANKING_PATH = COMBINATION_ROOT / "development_ranking.csv"
COMBINATION_SHORTLIST_PATH = COMBINATION_ROOT / "shortlist_descriptive.json"
COMBINATION_REPLICATION_DAY_PATH = COMBINATION_ROOT / "replication_policy_day_metrics.csv"
COMBINATION_REPLICATION_PATH = COMBINATION_ROOT / "replication_policy_metrics.csv"
COMBINATION_COMPARISON_PATH = COMBINATION_ROOT / "split_comparison.csv"

DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]
JUNE_DATES = ["2026-06-01"]
LATER_DATES = ["2026-07-01", "2026-08-01"]
ALL_DATES = DEVELOPMENT_DATES + JUNE_DATES + LATER_DATES
STAGE_DATES = {
    "development": DEVELOPMENT_DATES,
    "june_retrospective": JUNE_DATES,
    "jul_aug_retrospective": LATER_DATES,
}
SIGNALS = (
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks",
    "normalized_ofi_1s",
    "ti_1s",
    "combined_prediction_1s_ticks",
)
FROZEN_DECILE_SIGNALS = {
    "obi_l1": "obi_l1",
    "obi_l5": "obi_l5",
    "obi_l10": "obi_l10",
    "weighted_obi_l10": "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks": "weighted_mid_minus_mid_ticks",
    "ti_1s": "ti_1s",
}
RESOLVED = {"full", "partial", "unfilled"}
HORIZON_NAMES = {100: "100ms", 500: "500ms", 1000: "1s", 5000: "5s"}


@dataclass(frozen=True)
class Policy:
    name: str
    family: str
    kind: str
    signal: str = ""
    description: str = ""


@dataclass(frozen=True)
class CombinedPolicy:
    name: str
    family: str
    left: Policy
    right: Policy
    description: str = "logical AND of two frozen phase-1 filters"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _threshold_record(lo: float, median: float, hi: float, source: str) -> dict[str, Any]:
    if not lo <= median <= hi:
        raise ValueError(f"non-monotone exploration thresholds: {lo}, {median}, {hi}")
    return {"q20": lo, "q50": median, "q80": hi, "source": source}


def build_thresholds() -> dict[str, Any]:
    """Write outcome-free development thresholds before any approach sweep."""
    spec = _load_json(SPEC_PATH)
    if sha256(V1_SPEC_PATH) != spec["audit"]["maker_v1_spec_sha256"]:
        raise ValueError("maker V1 spec changed before approach exploration")
    transforms = _load_json(TRANSFORMS_PATH)
    thresholds: dict[str, dict[str, Any]] = {}
    for output_name, transform_name in FROZEN_DECILE_SIGNALS.items():
        values = transforms["decile_thresholds"][transform_name]
        thresholds[output_name] = _threshold_record(
            float(values[1]),
            float(values[4]),
            float(values[7]),
            "existing_frozen_development_deciles",
        )

    model = _load_json(MODELS_PATH)["models"]["combined:1000"]
    model_transforms = transforms
    prediction_values: list[np.ndarray] = []
    normalized_ofi_values: list[np.ndarray] = []
    queue_values: list[np.ndarray] = []
    spread_values: list[np.ndarray] = []
    input_hashes: dict[str, str] = {}
    source_columns = list(dict.fromkeys(
        list(model["features"])
        + ["normalized_ofi_1s", "best_bid_qty", "best_ask_qty", "spread_ticks"]
    ))
    for date in DEVELOPMENT_DATES:
        path = FEATURE_ROOT / date / "features_100ms.parquet"
        frame = pd.read_parquet(path, columns=source_columns)
        prediction = frozen_prediction(frame, model, model_transforms)
        prediction_values.append(prediction[np.isfinite(prediction)])
        ofi = frame["normalized_ofi_1s"].to_numpy(dtype="float64")
        normalized_ofi_values.append(ofi[np.isfinite(ofi)])
        for column in ("best_bid_qty", "best_ask_qty"):
            values = frame[column].to_numpy(dtype="float64")
            queue_values.append(values[np.isfinite(values)])
        spread = frame["spread_ticks"].to_numpy(dtype="float64")
        spread_values.append(spread[np.isfinite(spread)])
        input_hashes[date] = sha256(path)

    def fitted(name: str, chunks: list[np.ndarray]) -> None:
        values = np.concatenate(chunks)
        quantiles = np.quantile(values, [0.2, 0.5, 0.8], method="linear")
        thresholds[name] = _threshold_record(
            float(quantiles[0]),
            float(quantiles[1]),
            float(quantiles[2]),
            "development_distribution_only_no_maker_outcomes",
        )
        thresholds[name]["finite_rows"] = int(len(values))

    fitted("combined_prediction_1s_ticks", prediction_values)
    fitted("normalized_ofi_1s", normalized_ofi_values)
    fitted("queue_ahead_initial", queue_values)
    fitted("spread_ticks", spread_values)

    v1_thresholds = _load_json(V1_THRESHOLDS_PATH)
    prediction_threshold = thresholds["combined_prediction_1s_ticks"]
    if not np.isclose(prediction_threshold["q20"], v1_thresholds["bearish_threshold_ticks"]):
        raise ValueError("exploration prediction q20 differs from V1 outcome-free threshold")
    if not np.isclose(prediction_threshold["q80"], v1_thresholds["bullish_threshold_ticks"]):
        raise ValueError("exploration prediction q80 differs from V1 outcome-free threshold")

    payload = {
        "schema": "passive-approach-thresholds-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "maker_v1_spec_sha256": sha256(V1_SPEC_PATH),
        "alpha_models_sha256": sha256(MODELS_PATH),
        "alpha_transforms_sha256": sha256(TRANSFORMS_PATH),
        "fit_dates": DEVELOPMENT_DATES,
        "maker_outcomes_read": False,
        "feature_sha256": input_hashes,
        "thresholds": thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def policies() -> list[Policy]:
    result = [Policy("always_quote", "baseline", "baseline", description="quote both sides")]
    rules = (
        ("trend_tail20", "trend_tail", "bid high / ask low, outer 20%"),
        ("contrarian_tail20", "contrarian_tail", "bid low / ask high, outer 20%"),
        ("trend_half", "trend_half", "bid above median / ask below median"),
        ("contrarian_half", "contrarian_half", "bid below median / ask above median"),
        ("trend_broad80", "trend_broad", "exclude low-tail bid and high-tail ask"),
        ("contrarian_broad80", "contrarian_broad", "exclude high-tail bid and low-tail ask"),
        ("central60", "central", "quote both sides only in middle 60%"),
    )
    for signal in SIGNALS:
        for suffix, kind, description in rules:
            result.append(Policy(
                f"{signal}__{suffix}",
                "single_signal",
                kind,
                signal,
                description,
            ))

    for prefix, family in (("obi_depth", "obi_consensus"), ("micro", "micro_consensus")):
        for suffix, kind in (
            ("trend_tail", "consensus_trend_tail"),
            ("contrarian_tail", "consensus_contrarian_tail"),
            ("trend_half", "consensus_trend_half"),
            ("contrarian_half", "consensus_contrarian_half"),
        ):
            result.append(Policy(
                f"{prefix}_majority__{suffix}", family, kind,
                description="two-of-three directional consensus",
            ))
    for suffix, kind in (
        ("trend_tail", "joint_trend_tail"),
        ("contrarian_tail", "joint_contrarian_tail"),
        ("trend_broad", "joint_trend_broad"),
        ("contrarian_broad", "joint_contrarian_broad"),
    ):
        result.append(Policy(
            f"prediction_obi_l5_joint__{suffix}",
            "joint_signal",
            kind,
            description="combined prediction and OBI L5 must agree",
        ))

    result.extend([
        Policy("queue_ahead__bottom20", "liquidity", "queue_low"),
        Policy("queue_ahead__top20", "liquidity", "queue_high"),
        Policy("queue_ahead__middle60", "liquidity", "queue_middle"),
        Policy("spread__one_tick", "liquidity", "spread_one"),
        Policy("spread__wider_than_one_tick", "liquidity", "spread_wide"),
        Policy("utc_session__00_08", "time", "session_00_08"),
        Policy("utc_session__08_16", "time", "session_08_16"),
        Policy("utc_session__16_24", "time", "session_16_24"),
    ])
    names = [policy.name for policy in result]
    if len(names) != len(set(names)):
        raise ValueError("duplicate passive approach policy name")
    return result


def _single_mask(
    kind: str,
    values: np.ndarray,
    is_bid: np.ndarray,
    threshold: dict[str, Any],
) -> np.ndarray:
    finite = np.isfinite(values)
    low = float(threshold["q20"])
    median = float(threshold["q50"])
    high = float(threshold["q80"])
    if kind == "trend_tail":
        return finite & np.where(is_bid, values >= high, values <= low)
    if kind == "contrarian_tail":
        return finite & np.where(is_bid, values <= low, values >= high)
    if kind == "trend_half":
        return finite & np.where(is_bid, values >= median, values <= median)
    if kind == "contrarian_half":
        return finite & np.where(is_bid, values <= median, values >= median)
    if kind == "trend_broad":
        return finite & np.where(is_bid, values >= low, values <= high)
    if kind == "contrarian_broad":
        return finite & np.where(is_bid, values <= high, values >= low)
    if kind == "central":
        return finite & (values >= low) & (values <= high)
    raise ValueError(f"unknown single-signal policy kind: {kind}")


def policy_mask(
    policy: Policy | CombinedPolicy,
    arrays: dict[str, np.ndarray],
    thresholds: dict[str, dict[str, Any]],
) -> np.ndarray:
    if isinstance(policy, CombinedPolicy):
        return policy_mask(policy.left, arrays, thresholds) & policy_mask(
            policy.right, arrays, thresholds
        )
    is_bid = arrays["is_bid"]
    if policy.kind == "baseline":
        return np.ones(len(is_bid), dtype=bool)
    if policy.signal:
        return _single_mask(policy.kind, arrays[policy.signal], is_bid, thresholds[policy.signal])

    if policy.kind.startswith("consensus_"):
        signals = (
            ("obi_l1", "obi_l5", "obi_l10")
            if policy.family == "obi_consensus"
            else ("combined_prediction_1s_ticks", "normalized_ofi_1s", "ti_1s")
        )
        use_tail = policy.kind.endswith("tail")
        high_votes = np.zeros(len(is_bid), dtype=np.int8)
        low_votes = np.zeros(len(is_bid), dtype=np.int8)
        all_finite = np.ones(len(is_bid), dtype=bool)
        for signal in signals:
            values = arrays[signal]
            threshold = thresholds[signal]
            boundary_high = threshold["q80"] if use_tail else threshold["q50"]
            boundary_low = threshold["q20"] if use_tail else threshold["q50"]
            finite = np.isfinite(values)
            all_finite &= finite
            high_votes += finite & (values >= boundary_high)
            low_votes += finite & (values <= boundary_low)
        trend = "contrarian" not in policy.kind
        return all_finite & np.where(
            is_bid,
            high_votes >= 2 if trend else low_votes >= 2,
            low_votes >= 2 if trend else high_votes >= 2,
        )

    if policy.kind.startswith("joint_"):
        mode = policy.kind.removeprefix("joint_")
        prediction = _single_mask(
            mode,
            arrays["combined_prediction_1s_ticks"],
            is_bid,
            thresholds["combined_prediction_1s_ticks"],
        )
        obi = _single_mask(
            mode,
            arrays["obi_l5"],
            is_bid,
            thresholds["obi_l5"],
        )
        return prediction & obi

    queue = arrays["queue_ahead_initial"]
    if policy.kind == "queue_low":
        return np.isfinite(queue) & (queue <= thresholds["queue_ahead_initial"]["q20"])
    if policy.kind == "queue_high":
        return np.isfinite(queue) & (queue >= thresholds["queue_ahead_initial"]["q80"])
    if policy.kind == "queue_middle":
        return (
            np.isfinite(queue)
            & (queue >= thresholds["queue_ahead_initial"]["q20"])
            & (queue <= thresholds["queue_ahead_initial"]["q80"])
        )
    spread = arrays["spread_ticks"]
    if policy.kind == "spread_one":
        return np.isfinite(spread) & (spread <= 1.0 + 1e-9)
    if policy.kind == "spread_wide":
        return np.isfinite(spread) & (spread > 1.0 + 1e-9)
    hour = ((arrays["decision_time_us"] // 3_600_000_000) % 24).astype(np.int8)
    if policy.kind == "session_00_08":
        return hour < 8
    if policy.kind == "session_08_16":
        return (hour >= 8) & (hour < 16)
    if policy.kind == "session_16_24":
        return hour >= 16
    raise ValueError(f"unknown passive approach policy: {policy}")


def _columns(horizons: list[int], include_optimistic: bool) -> list[str]:
    columns = [
        "date", "decision_time_us", "side", "quote_lifetime_ms", "fill_status",
        "filled_qty", "queue_ahead_initial", "spread_ticks", *SIGNALS,
    ]
    for horizon in horizons:
        name = HORIZON_NAMES[horizon]
        columns.extend([
            f"maker_markout_{name}_ticks",
            f"maker_markout_{name}_bps",
            f"post_fill_mid_move_{name}_ticks",
        ])
    columns.append("fill_price_advantage_ticks")
    if include_optimistic:
        columns.extend(["optimistic_fill_status", "optimistic_filled_qty"])
        for horizon in horizons:
            name = HORIZON_NAMES[horizon]
            columns.append(f"optimistic_maker_markout_{name}_ticks")
    return columns


def _read_day(
    date: str,
    lifetime_ms: int,
    horizons: list[int],
    *,
    include_optimistic: bool,
) -> pd.DataFrame:
    frame = pd.read_parquet(
        PASSIVE_ROOT / date / "labeled_probes.parquet",
        columns=_columns(horizons, include_optimistic),
        filters=[("quote_lifetime_ms", "=", lifetime_ms)],
    )
    if len(frame) != 1_728_000 or not frame["quote_lifetime_ms"].eq(lifetime_ms).all():
        raise ValueError(f"unexpected passive approach day/lifetime: {date} {lifetime_ms}")
    if frame["date"].nunique() != 1 or frame["date"].iat[0] != date:
        raise ValueError(f"passive approach date mismatch: {date}")
    return frame


def _arrays(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    result = {
        "is_bid": frame["side"].eq("bid").to_numpy(),
        "decision_time_us": frame["decision_time_us"].to_numpy(dtype="int64"),
        "queue_ahead_initial": frame["queue_ahead_initial"].to_numpy(dtype="float64"),
        "spread_ticks": frame["spread_ticks"].to_numpy(dtype="float64"),
    }
    for signal in SIGNALS:
        result[signal] = frame[signal].to_numpy(dtype="float64")
    return result


def _metric_row(
    *,
    date: str,
    policy: Policy | CombinedPolicy,
    side: str,
    eligible: np.ndarray,
    is_bid: np.ndarray,
    resolved_status: np.ndarray,
    full_status: np.ndarray,
    partial_status: np.ndarray,
    filled_qty: np.ndarray,
    markout: np.ndarray,
    markout_bps: np.ndarray | None,
    post_move: np.ndarray | None,
    advantage: np.ndarray | None,
    lifetime_ms: int,
    horizon_ms: int,
    queue_model: str,
) -> dict[str, Any]:
    side_mask = np.ones(len(eligible), dtype=bool)
    if side == "bid":
        side_mask = is_bid
    elif side == "ask":
        side_mask = ~is_bid
    selected = eligible & side_mask
    resolved = selected & resolved_status
    full = selected & full_status
    partial = selected & partial_status
    labeled = selected & (filled_qty > 0) & np.isfinite(markout)
    values = markout[labeled]
    count = len(values)
    quantiles = (
        np.quantile(values, [0.05, 0.25, 0.5, 0.75, 0.95])
        if count else np.full(5, np.nan)
    )
    candidate_count = int(selected.sum())
    resolved_count = int(resolved.sum())
    return {
        "date": date,
        "policy": policy.name,
        "family": policy.family,
        "side": side,
        "queue_model": queue_model,
        "quote_lifetime_ms": lifetime_ms,
        "markout_horizon_ms": horizon_ms,
        "candidate_quotes": candidate_count,
        "resolved_quotes": resolved_count,
        "full_fills": int(full.sum()),
        "partial_fills": int(partial.sum()),
        "labeled_fills": count,
        "full_fill_probability": float(full.sum() / resolved_count) if resolved_count else np.nan,
        "labeled_fill_probability": count / resolved_count if resolved_count else np.nan,
        "maker_markout_mean_ticks": float(values.mean()) if count else np.nan,
        "maker_markout_median_ticks": float(quantiles[2]),
        "maker_markout_negative_probability": float(np.mean(values < 0)) if count else np.nan,
        "maker_markout_p05_ticks": float(quantiles[0]),
        "maker_markout_p25_ticks": float(quantiles[1]),
        "maker_markout_p75_ticks": float(quantiles[3]),
        "maker_markout_p95_ticks": float(quantiles[4]),
        "post_fill_mid_move_mean_ticks": (
            float(post_move[labeled].mean()) if count and post_move is not None else np.nan
        ),
        "fill_price_advantage_mean_ticks": (
            float(advantage[labeled].mean()) if count and advantage is not None else np.nan
        ),
        "gross_passive_edge_mean_bps": (
            float(markout_bps[labeled].mean()) if count and markout_bps is not None else np.nan
        ),
        "markout_sum_ticks": float(values.sum()),
        "negative_markouts": int((values < 0).sum()),
        "markout_bps_sum": (
            float(markout_bps[labeled].sum()) if count and markout_bps is not None else np.nan
        ),
        "post_move_sum_ticks": (
            float(post_move[labeled].sum()) if count and post_move is not None else np.nan
        ),
        "advantage_sum_ticks": (
            float(advantage[labeled].sum()) if count and advantage is not None else np.nan
        ),
    }


def _evaluate_frame(
    date: str,
    frame: pd.DataFrame,
    selected_policies: list[Policy | CombinedPolicy],
    threshold_payload: dict[str, Any],
    *,
    lifetime_ms: int,
    horizon_ms: int,
    queue_model: str,
) -> list[dict[str, Any]]:
    arrays = _arrays(frame)
    thresholds = threshold_payload["thresholds"]
    prefix = "" if queue_model == "pessimistic_visible_queue" else "optimistic_"
    name = HORIZON_NAMES[horizon_ms]
    status = frame[f"{prefix}fill_status"].fillna("").to_numpy(dtype=str)
    resolved_status = np.isin(status, list(RESOLVED))
    full_status = status == "full"
    partial_status = np.char.startswith(status, "partial")
    filled_qty = frame[f"{prefix}filled_qty"].fillna(0).to_numpy(dtype="float64")
    markout = frame[f"{prefix}maker_markout_{name}_ticks"].to_numpy(dtype="float64")
    markout_bps = (
        frame[f"maker_markout_{name}_bps"].to_numpy(dtype="float64")
        if not prefix else None
    )
    post_move = (
        frame[f"post_fill_mid_move_{name}_ticks"].to_numpy(dtype="float64")
        if not prefix else None
    )
    advantage = (
        frame["fill_price_advantage_ticks"].to_numpy(dtype="float64")
        if not prefix else None
    )
    rows = []
    for policy in selected_policies:
        eligible = policy_mask(policy, arrays, thresholds)
        for side in ("both", "bid", "ask"):
            rows.append(_metric_row(
                date=date,
                policy=policy,
                side=side,
                eligible=eligible,
                is_bid=arrays["is_bid"],
                resolved_status=resolved_status,
                full_status=full_status,
                partial_status=partial_status,
                filled_qty=filled_qty,
                markout=markout,
                markout_bps=markout_bps,
                post_move=post_move,
                advantage=advantage,
                lifetime_ms=lifetime_ms,
                horizon_ms=horizon_ms,
                queue_model=queue_model,
            ))
    return rows


def _aggregate_metrics(day: pd.DataFrame, stage: str) -> pd.DataFrame:
    rows = []
    dimensions = [
        "policy", "family", "side", "queue_model", "quote_lifetime_ms",
        "markout_horizon_ms",
    ]
    for keys, group in day.groupby(dimensions, sort=True, observed=True):
        data = dict(zip(dimensions, keys))
        candidate = int(group["candidate_quotes"].sum())
        resolved = int(group["resolved_quotes"].sum())
        full = int(group["full_fills"].sum())
        partial = int(group["partial_fills"].sum())
        labeled = int(group["labeled_fills"].sum())
        weight = group["labeled_fills"].to_numpy(dtype="float64")
        rows.append({
            "stage": stage,
            **data,
            "days": int(group["date"].nunique()),
            "candidate_quotes": candidate,
            "resolved_quotes": resolved,
            "full_fills": full,
            "partial_fills": partial,
            "labeled_fills": labeled,
            "full_fill_probability": full / resolved if resolved else np.nan,
            "labeled_fill_probability": labeled / resolved if resolved else np.nan,
            "maker_markout_mean_ticks": (
                float(group["markout_sum_ticks"].sum() / labeled) if labeled else np.nan
            ),
            "maker_markout_negative_probability": (
                float(group["negative_markouts"].sum() / labeled) if labeled else np.nan
            ),
            "day_weighted_median_ticks": (
                float(np.average(group["maker_markout_median_ticks"], weights=weight))
                if labeled else np.nan
            ),
            "day_weighted_p05_ticks": (
                float(np.average(group["maker_markout_p05_ticks"], weights=weight))
                if labeled else np.nan
            ),
            "day_weighted_p95_ticks": (
                float(np.average(group["maker_markout_p95_ticks"], weights=weight))
                if labeled else np.nan
            ),
            "post_fill_mid_move_mean_ticks": (
                float(group["post_move_sum_ticks"].sum() / labeled)
                if labeled and group["post_move_sum_ticks"].notna().all() else np.nan
            ),
            "fill_price_advantage_mean_ticks": (
                float(group["advantage_sum_ticks"].sum() / labeled)
                if labeled and group["advantage_sum_ticks"].notna().all() else np.nan
            ),
            "gross_passive_edge_mean_bps": (
                float(group["markout_bps_sum"].sum() / labeled)
                if labeled and group["markout_bps_sum"].notna().all() else np.nan
            ),
        })
    result = pd.DataFrame(rows)
    baseline = result.loc[result["policy"].eq("always_quote")].set_index(
        ["side", "queue_model", "quote_lifetime_ms", "markout_horizon_ms"]
    )
    retention = []
    for row in result.itertuples(index=False):
        key = (row.side, row.queue_model, row.quote_lifetime_ms, row.markout_horizon_ms)
        base_candidates = baseline.loc[key, "candidate_quotes"]
        retention.append(row.candidate_quotes / base_candidates)
    result["candidate_retention"] = retention
    return result


def _development_ranking(
    day: pd.DataFrame,
    aggregate: pd.DataFrame,
    *,
    minimum_candidate_retention: float = 0.05,
    minimum_labeled_fills: int = 10_000,
) -> pd.DataFrame:
    both = day.loc[day["side"].eq("both")]
    baseline = both.loc[both["policy"].eq("always_quote")].set_index("date")
    rows = []
    aggregate_both = aggregate.loc[aggregate["side"].eq("both")].set_index("policy")
    for policy, group in both.groupby("policy", sort=True):
        if policy == "always_quote":
            continue
        deltas = []
        p05_deltas = []
        for row in group.itertuples(index=False):
            base = baseline.loc[row.date]
            deltas.append(row.maker_markout_mean_ticks - base.maker_markout_mean_ticks)
            p05_deltas.append(row.maker_markout_p05_ticks - base.maker_markout_p05_ticks)
        pooled = aggregate_both.loc[policy]
        base_pooled = aggregate_both.loc["always_quote"]
        eligible = (
            pooled.candidate_retention >= minimum_candidate_retention
            and pooled.labeled_fills >= minimum_labeled_fills
        )
        rows.append({
            "policy": policy,
            "family": group["family"].iat[0],
            "eligible": eligible,
            "candidate_retention": pooled.candidate_retention,
            "labeled_fills": int(pooled.labeled_fills),
            "development_mean_ticks": pooled.maker_markout_mean_ticks,
            "pooled_mean_improvement_ticks": (
                pooled.maker_markout_mean_ticks - base_pooled.maker_markout_mean_ticks
            ),
            "improved_development_days": int(np.sum(np.asarray(deltas) > 0)),
            "worst_day_improvement_ticks": float(np.min(deltas)),
            "best_day_improvement_ticks": float(np.max(deltas)),
            "mean_day_improvement_ticks": float(np.mean(deltas)),
            "day_weighted_p05_improvement_ticks": (
                pooled.day_weighted_p05_ticks - base_pooled.day_weighted_p05_ticks
            ),
            "all_development_days_improved": bool(np.all(np.asarray(deltas) > 0)),
            "absolute_development_mean_positive": bool(pooled.maker_markout_mean_ticks > 0),
        })
    result = pd.DataFrame(rows)
    result.sort_values(
        [
            "eligible", "improved_development_days", "worst_day_improvement_ticks",
            "pooled_mean_improvement_ticks", "day_weighted_p05_improvement_ticks",
            "candidate_retention", "policy",
        ],
        ascending=[False, False, False, False, False, False, True],
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    result.insert(0, "rank", np.arange(1, len(result) + 1))
    return result


def run_development() -> dict[str, Any]:
    threshold_payload = build_thresholds()
    selected_policies = policies()
    write_csv(
        POLICY_CATALOG_PATH,
        pd.DataFrame(asdict(policy) for policy in selected_policies),
    )
    rows = []
    for date in DEVELOPMENT_DATES:
        frame = _read_day(date, 1000, [1000], include_optimistic=False)
        rows.extend(_evaluate_frame(
            date,
            frame,
            selected_policies,
            threshold_payload,
            lifetime_ms=1000,
            horizon_ms=1000,
            queue_model="pessimistic_visible_queue",
        ))
    day = pd.DataFrame(rows)
    write_csv(DEVELOPMENT_DAY_PATH, day)
    aggregate = _aggregate_metrics(day, "development")
    write_csv(DEVELOPMENT_SPLIT_PATH, aggregate)
    ranking = _development_ranking(day, aggregate)
    write_csv(RANKING_PATH, ranking)
    shortlist_size = int(_load_json(SPEC_PATH)["development_ranking"]["shortlist_size"])
    shortlist = ranking.loc[ranking["eligible"]].head(shortlist_size)
    if len(shortlist) != shortlist_size:
        raise RuntimeError("too few eligible passive exploration policies for shortlist")
    policy_map = {policy.name: policy for policy in selected_policies}
    payload = {
        "schema": "passive-approach-shortlist-v1",
        "created_from_development_only": True,
        "replication_outcomes_read": False,
        "spec_sha256": sha256(SPEC_PATH),
        "thresholds_sha256": sha256(THRESHOLDS_PATH),
        "development_day_metrics_sha256": sha256(DEVELOPMENT_DAY_PATH),
        "development_metrics_sha256": sha256(DEVELOPMENT_SPLIT_PATH),
        "development_ranking_sha256": sha256(RANKING_PATH),
        "selection_rule": _load_json(SPEC_PATH)["development_ranking"],
        "shortlist": [
            {
                **asdict(policy_map[row.policy]),
                "development_rank": int(row.rank),
                "development_mean_ticks": float(row.development_mean_ticks),
                "development_improvement_ticks": float(row.pooled_mean_improvement_ticks),
                "improved_development_days": int(row.improved_development_days),
                "worst_day_improvement_ticks": float(row.worst_day_improvement_ticks),
            }
            for row in shortlist.itertuples(index=False)
        ],
    }
    write_json(SHORTLIST_PATH, payload)
    return {
        "policies": len(selected_policies),
        "development_dates": DEVELOPMENT_DATES,
        "shortlist": [item["name"] for item in payload["shortlist"]],
        "best_development_policy": payload["shortlist"][0],
        "shortlist_sha256": sha256(SHORTLIST_PATH),
    }


def _load_shortlist() -> list[Policy]:
    if not SHORTLIST_PATH.exists():
        raise FileNotFoundError("run development approach ranking before replication")
    payload = _load_json(SHORTLIST_PATH)
    if not payload["created_from_development_only"] or payload["replication_outcomes_read"]:
        raise ValueError("invalid passive approach shortlist audit")
    if payload["spec_sha256"] != sha256(SPEC_PATH):
        raise ValueError("passive approach spec changed after shortlist freeze")
    if payload["thresholds_sha256"] != sha256(THRESHOLDS_PATH):
        raise ValueError("passive approach thresholds changed after shortlist freeze")
    policy_map = {policy.name: policy for policy in policies()}
    return [policy_map[item["name"]] for item in payload["shortlist"]]


def _run_sensitivity(
    threshold_payload: dict[str, Any],
    shortlist: list[Policy],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = [policies()[0], *shortlist]
    rows = []
    for date in ALL_DATES:
        for lifetime in (100, 500, 1000, 5000):
            frame = _read_day(
                date,
                lifetime,
                [100, 500, 1000, 5000],
                include_optimistic=True,
            )
            for queue_model in (
                "pessimistic_visible_queue",
                "optimistic_front_of_queue_upper_bound",
            ):
                for horizon in (100, 500, 1000, 5000):
                    rows.extend(_evaluate_frame(
                        date,
                        frame,
                        selected,
                        threshold_payload,
                        lifetime_ms=lifetime,
                        horizon_ms=horizon,
                        queue_model=queue_model,
                    ))
    day = pd.DataFrame(rows)
    write_csv(SENSITIVITY_DAY_PATH, day)
    parts = []
    for stage, dates in STAGE_DATES.items():
        parts.append(_aggregate_metrics(day.loc[day["date"].isin(dates)], stage))
    aggregate = pd.concat(parts, ignore_index=True)
    write_csv(SENSITIVITY_PATH, aggregate)
    return day, aggregate


def run_replication() -> dict[str, Any]:
    shortlist = _load_shortlist()
    threshold_payload = _load_json(THRESHOLDS_PATH)
    selected_policies = policies()
    rows = []
    for date in JUNE_DATES + LATER_DATES:
        frame = _read_day(date, 1000, [1000], include_optimistic=False)
        rows.extend(_evaluate_frame(
            date,
            frame,
            selected_policies,
            threshold_payload,
            lifetime_ms=1000,
            horizon_ms=1000,
            queue_model="pessimistic_visible_queue",
        ))
    day = pd.DataFrame(rows)
    write_csv(REPLICATION_DAY_PATH, day)
    aggregate_parts = []
    for stage, dates in (
        ("june_retrospective", JUNE_DATES),
        ("jul_aug_retrospective", LATER_DATES),
    ):
        aggregate_parts.append(_aggregate_metrics(day.loc[day["date"].isin(dates)], stage))
    aggregate = pd.concat(aggregate_parts, ignore_index=True)
    write_csv(REPLICATION_SPLIT_PATH, aggregate)
    development = pd.read_csv(DEVELOPMENT_SPLIT_PATH)
    comparison = pd.concat([development, aggregate], ignore_index=True)
    write_csv(COMPARISON_PATH, comparison)
    _, sensitivity = _run_sensitivity(threshold_payload, shortlist)
    return {
        "policies": len(selected_policies),
        "retrospective_dates": JUNE_DATES + LATER_DATES,
        "shortlist": [policy.name for policy in shortlist],
        "sensitivity_rows": len(sensitivity),
        "new_unseen_validation_claimed": False,
    }


def combination_policies() -> list[CombinedPolicy]:
    result: list[CombinedPolicy] = []
    queue_conditions = (
        ("bottom20", "queue_low"),
        ("middle60", "queue_middle"),
        ("top20", "queue_high"),
    )
    sessions = (
        ("00_08", "session_00_08"),
        ("08_16", "session_08_16"),
        ("16_24", "session_16_24"),
    )
    directions = (
        ("trend_tail20", "trend_tail"),
        ("contrarian_tail20", "contrarian_tail"),
        ("trend_half", "trend_half"),
        ("contrarian_half", "contrarian_half"),
    )
    for signal in SIGNALS:
        for direction_name, direction_kind in directions:
            signal_policy = Policy(
                f"{signal}__{direction_name}",
                "single_signal",
                direction_kind,
                signal,
            )
            for queue_name, queue_kind in queue_conditions:
                result.append(CombinedPolicy(
                    f"{signal}__{direction_name}__queue_{queue_name}",
                    "single_signal_x_queue",
                    signal_policy,
                    Policy(f"queue_{queue_name}", "liquidity", queue_kind),
                ))
        for direction_name, direction_kind in directions[:2]:
            signal_policy = Policy(
                f"{signal}__{direction_name}",
                "single_signal",
                direction_kind,
                signal,
            )
            for session_name, session_kind in sessions:
                result.append(CombinedPolicy(
                    f"{signal}__{direction_name}__utc_{session_name}",
                    "single_signal_x_time",
                    signal_policy,
                    Policy(f"utc_{session_name}", "time", session_kind),
                ))

    composite_bases = (
        (
            "obi_depth_majority",
            "obi_consensus",
            "consensus_trend_tail",
            "consensus_contrarian_tail",
        ),
        (
            "micro_majority",
            "micro_consensus",
            "consensus_trend_tail",
            "consensus_contrarian_tail",
        ),
        (
            "prediction_obi_l5_joint",
            "joint_signal",
            "joint_trend_tail",
            "joint_contrarian_tail",
        ),
    )
    for base_name, family, trend_kind, contrarian_kind in composite_bases:
        for direction_name, direction_kind in (
            ("trend_tail", trend_kind),
            ("contrarian_tail", contrarian_kind),
        ):
            base = Policy(f"{base_name}__{direction_name}", family, direction_kind)
            for queue_name, queue_kind in queue_conditions:
                result.append(CombinedPolicy(
                    f"{base_name}__{direction_name}__queue_{queue_name}",
                    "composite_signal_x_queue",
                    base,
                    Policy(f"queue_{queue_name}", "liquidity", queue_kind),
                ))

    for queue_name, queue_kind in queue_conditions:
        for session_name, session_kind in sessions:
            result.append(CombinedPolicy(
                f"queue_{queue_name}__utc_{session_name}",
                "queue_x_time",
                Policy(f"queue_{queue_name}", "liquidity", queue_kind),
                Policy(f"utc_{session_name}", "time", session_kind),
            ))
    names = [policy.name for policy in result]
    if len(result) != 171 or len(names) != len(set(names)):
        raise ValueError("unexpected phase-2 combination policy catalog")
    return result


def _combination_catalog_frame(selected: list[CombinedPolicy]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "name": item.name,
            "family": item.family,
            "left_name": item.left.name,
            "left_kind": item.left.kind,
            "left_signal": item.left.signal,
            "right_name": item.right.name,
            "right_kind": item.right.kind,
            "description": item.description,
        }
        for item in selected
    )


def run_combinations() -> dict[str, Any]:
    spec = _load_json(COMBINATION_SPEC_PATH)
    if spec["audit"]["phase1_spec_sha256"] != sha256(SPEC_PATH):
        raise ValueError("phase-1 exploration spec changed before combination sweep")
    if not spec["audit"]["phase1_results_seen"]:
        raise ValueError("combination sweep must disclose that phase-1 results were seen")
    threshold_payload = _load_json(THRESHOLDS_PATH)
    combined = combination_policies()
    selected: list[Policy | CombinedPolicy] = [policies()[0], *combined]
    write_csv(COMBINATION_CATALOG_PATH, _combination_catalog_frame(combined))

    development_rows = []
    for date in DEVELOPMENT_DATES:
        frame = _read_day(date, 1000, [1000], include_optimistic=False)
        development_rows.extend(_evaluate_frame(
            date,
            frame,
            selected,
            threshold_payload,
            lifetime_ms=1000,
            horizon_ms=1000,
            queue_model="pessimistic_visible_queue",
        ))
    development_day = pd.DataFrame(development_rows)
    write_csv(COMBINATION_DEVELOPMENT_DAY_PATH, development_day)
    development = _aggregate_metrics(development_day, "development")
    write_csv(COMBINATION_DEVELOPMENT_PATH, development)
    rank_spec = spec["descriptive_ranking"]
    ranking = _development_ranking(
        development_day,
        development,
        minimum_candidate_retention=float(rank_spec["minimum_candidate_retention"]),
        minimum_labeled_fills=int(rank_spec["minimum_labeled_fills"]),
    )
    write_csv(COMBINATION_RANKING_PATH, ranking)
    shortlist = ranking.loc[ranking["eligible"]].head(int(rank_spec["shortlist_size"]))
    shortlist_payload = {
        "schema": "passive-approach-combination-shortlist-v1",
        "descriptive_not_confirmatory": True,
        "all_dates_already_seen": True,
        "combination_spec_sha256": sha256(COMBINATION_SPEC_PATH),
        "development_metrics_sha256": sha256(COMBINATION_DEVELOPMENT_PATH),
        "development_ranking_sha256": sha256(COMBINATION_RANKING_PATH),
        "shortlist": [
            {
                "name": row.policy,
                "rank": int(row.rank),
                "candidate_retention": float(row.candidate_retention),
                "labeled_fills": int(row.labeled_fills),
                "development_mean_ticks": float(row.development_mean_ticks),
                "development_improvement_ticks": float(row.pooled_mean_improvement_ticks),
            }
            for row in shortlist.itertuples(index=False)
        ],
    }
    write_json(COMBINATION_SHORTLIST_PATH, shortlist_payload)

    replication_rows = []
    for date in JUNE_DATES + LATER_DATES:
        frame = _read_day(date, 1000, [1000], include_optimistic=False)
        replication_rows.extend(_evaluate_frame(
            date,
            frame,
            selected,
            threshold_payload,
            lifetime_ms=1000,
            horizon_ms=1000,
            queue_model="pessimistic_visible_queue",
        ))
    replication_day = pd.DataFrame(replication_rows)
    write_csv(COMBINATION_REPLICATION_DAY_PATH, replication_day)
    replication = pd.concat(
        [
            _aggregate_metrics(
                replication_day.loc[replication_day["date"].isin(dates)], stage
            )
            for stage, dates in (
                ("june_retrospective", JUNE_DATES),
                ("jul_aug_retrospective", LATER_DATES),
            )
        ],
        ignore_index=True,
    )
    write_csv(COMBINATION_REPLICATION_PATH, replication)
    write_csv(
        COMBINATION_COMPARISON_PATH,
        pd.concat([development, replication], ignore_index=True),
    )
    return {
        "combination_policies": len(combined),
        "primary_policies_including_baseline": len(selected),
        "shortlist": shortlist["policy"].tolist(),
        "new_unseen_validation_claimed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("thresholds", "development", "replication", "combinations")
    )
    args = parser.parse_args()
    if args.command == "thresholds":
        result = build_thresholds()
    elif args.command == "development":
        result = run_development()
    elif args.command == "replication":
        result = run_replication()
    else:
        result = run_combinations()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
