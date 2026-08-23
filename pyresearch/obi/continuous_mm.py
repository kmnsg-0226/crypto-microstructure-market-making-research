"""Frozen neutral versus OBI-aware continuous-inventory market making."""
from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.search import REPORT_ROOT, _model_inputs, derive_arrays, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/continuous_inventory_mm_spec.json"
PASSIVE_ROOT = ROOT / "data/research/tardis/passive"
OUTPUT_ROOT = REPORT_ROOT / "continuous_inventory_mm"
DEVELOPMENT_DAY_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
DEVELOPMENT_SUMMARY_PATH = OUTPUT_ROOT / "development_summary.json"
VALIDATION_DAY_PATH = OUTPUT_ROOT / "validation_day_metrics.csv"
VALIDATION_SUMMARY_PATH = OUTPUT_ROOT / "validation_summary.json"
VALIDATION_RECEIPT_PATH = OUTPUT_ROOT / "validation_single_use_receipt.json"
REPLICATION_DAY_PATH = OUTPUT_ROOT / "replication_day_metrics.csv"
REPLICATION_SUMMARY_PATH = OUTPUT_ROOT / "replication_summary.json"
REPLICATION_RECEIPT_PATH = OUTPUT_ROOT / "replication_single_use_receipt.json"
POLICIES = ("neutral", "obi_aware")
SCHEDULE_COLUMNS = [
    "date",
    "decision_time_us",
    "placement_local_time_us",
    "feature_segment_id",
    "side",
    "quote_price",
    "quote_qty",
    "quote_lifetime_ms",
    "expiry_local_time_us",
    "weighted_obi_l10",
    "fill_status",
    "first_fill_local_time_us",
    "full_fill_local_time_us",
    "filled_qty",
]
VALID_ORDER_STATUSES = {"full", "partial", "unfilled"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_spec() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    if spec["status"] != "frozen_before_continuous_inventory_mm_outcomes":
        raise ValueError("continuous-MM spec is not frozen")
    if not spec["audit"]["directional_loop_search_stopped"]:
        raise ValueError("directional search must remain stopped")
    if set(spec["policies"]) != set(POLICIES):
        raise ValueError("continuous-MM policy catalog changed")
    return spec


def quote_permission(
    *,
    policy: str,
    side: str,
    inventory_btc: float,
    obi: float,
    spec: dict[str, Any],
) -> tuple[bool, str | None]:
    inventory = spec["inventory"]
    qty = float(spec["market"]["quote_qty_btc"])
    soft = float(inventory["soft_limit_abs_btc"])
    hard = float(inventory["hard_limit_abs_btc"])
    tolerance = float(inventory["quantity_tolerance_btc"])
    if side == "bid":
        if inventory_btc + qty > hard + tolerance:
            return False, "inventory"
        if inventory_btc >= soft - tolerance:
            return False, "inventory"
        reduces_inventory = inventory_btc < -tolerance
    elif side == "ask":
        if inventory_btc - qty < -hard - tolerance:
            return False, "inventory"
        if inventory_btc <= -soft + tolerance:
            return False, "inventory"
        reduces_inventory = inventory_btc > tolerance
    else:
        raise ValueError(f"unknown quote side: {side}")
    if policy == "neutral" or not np.isfinite(obi):
        return True, None
    if policy != "obi_aware":
        raise ValueError(f"unknown MM policy: {policy}")
    threshold = float(spec["policies"]["obi_aware"]["absolute_threshold"])
    if side == "ask" and obi >= threshold and not reduces_inventory:
        return False, "obi"
    if side == "bid" and obi <= -threshold and not reduces_inventory:
        return False, "obi"
    return True, None


def _apply_fill(state: dict[str, Any], event: tuple[Any, ...], spec: dict[str, Any]) -> None:
    _, _, side, qty, price, fill_status = event
    notional = float(qty) * float(price)
    if side == "bid":
        state["inventory_btc"] += float(qty)
        state["cash_gross_usdt"] -= notional
        state["buy_filled_qty_btc"] += float(qty)
    elif side == "ask":
        state["inventory_btc"] -= float(qty)
        state["cash_gross_usdt"] += notional
        state["sell_filled_qty_btc"] += float(qty)
    else:
        raise ValueError(f"unknown fill side: {side}")
    maker_fee = float(spec["costs"]["maker_fee_bps"]) / 10_000.0
    state["maker_turnover_usdt"] += notional
    state["maker_fees_usdt"] += notional * maker_fee
    state["maker_filled_qty_btc"] += float(qty)
    state[f"{fill_status}_fill_orders"] += 1
    state["maximum_absolute_inventory_btc"] = max(
        state["maximum_absolute_inventory_btc"],
        abs(state["inventory_btc"]),
    )
    hard = float(spec["inventory"]["hard_limit_abs_btc"])
    tolerance = float(spec["inventory"]["quantity_tolerance_btc"])
    if abs(state["inventory_btc"]) > hard + tolerance:
        state["inventory_limit_violations"] += 1


def simulate_schedule(
    schedule: pd.DataFrame,
    *,
    date: str,
    policy: str,
    last_bid: float,
    last_ask: float,
    spec: dict[str, Any],
    selection_column: str | None = None,
    risk_reducing_override: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "inventory_btc": 0.0,
        "cash_gross_usdt": 0.0,
        "maker_turnover_usdt": 0.0,
        "maker_fees_usdt": 0.0,
        "maker_filled_qty_btc": 0.0,
        "buy_filled_qty_btc": 0.0,
        "sell_filled_qty_btc": 0.0,
        "full_fill_orders": 0,
        "partial_fill_orders": 0,
        "maker_quote_attempts": 0,
        "maximum_absolute_inventory_btc": 0.0,
        "inventory_limit_violations": 0,
        "suppressed_by_inventory": 0,
        "suppressed_by_obi": 0,
        "suppressed_by_selector": 0,
        "active_order_conflicts": 0,
        "invalid_placements": 0,
    }
    available = {"bid": -1, "ask": -1}
    pending: list[tuple[Any, ...]] = []
    sequence = 0
    expected_qty = float(spec["market"]["quote_qty_btc"])
    tolerance = float(spec["inventory"]["quantity_tolerance_btc"])

    for row in schedule.itertuples(index=False):
        placement = int(row.placement_local_time_us)
        while pending and int(pending[0][0]) <= placement:
            _apply_fill(state, heapq.heappop(pending), spec)
        side = str(row.side)
        if placement < available[side]:
            state["active_order_conflicts"] += 1
            continue
        fill_status = str(row.fill_status)
        if fill_status not in VALID_ORDER_STATUSES:
            state["invalid_placements"] += 1
            continue
        if abs(float(row.quote_qty) - expected_qty) > tolerance:
            raise ValueError("passive quote quantity differs from frozen MM quantity")
        allowed, reason = quote_permission(
            policy=policy,
            side=side,
            inventory_btc=float(state["inventory_btc"]),
            obi=float(row.weighted_obi_l10),
            spec=spec,
        )
        if not allowed:
            state[f"suppressed_by_{reason}"] += 1
            continue
        reduces_inventory = (
            side == "bid" and float(state["inventory_btc"]) < -tolerance
        ) or (
            side == "ask" and float(state["inventory_btc"]) > tolerance
        )
        if selection_column is not None and not bool(getattr(row, selection_column)):
            if not (risk_reducing_override and reduces_inventory):
                state["suppressed_by_selector"] += 1
                continue
        state["maker_quote_attempts"] += 1
        if fill_status == "full":
            if not np.isfinite(row.first_fill_local_time_us):
                raise ValueError("full fill lacks first-fill local time")
            if not np.isfinite(row.full_fill_local_time_us):
                raise ValueError("full fill lacks completion local time")
            event_time = int(row.first_fill_local_time_us)
            order_end = int(row.full_fill_local_time_us)
            fill_qty = float(row.filled_qty)
        elif fill_status == "partial":
            if not np.isfinite(row.first_fill_local_time_us):
                raise ValueError("partial fill lacks first-fill local time")
            event_time = int(row.first_fill_local_time_us)
            order_end = int(row.expiry_local_time_us)
            fill_qty = float(row.filled_qty)
        else:
            event_time = -1
            order_end = int(row.expiry_local_time_us)
            fill_qty = 0.0
        if order_end < placement:
            raise ValueError("passive order ends before placement")
        available[side] = order_end
        if fill_qty > tolerance:
            if event_time < placement or event_time > order_end:
                raise ValueError("passive fill event falls outside order lifetime")
            sequence += 1
            heapq.heappush(
                pending,
                (
                    event_time,
                    sequence,
                    side,
                    fill_qty,
                    float(row.quote_price),
                    fill_status,
                ),
            )

    while pending:
        _apply_fill(state, heapq.heappop(pending), spec)

    pre_liquidation_inventory = float(state["inventory_btc"])
    liquidation_qty = abs(pre_liquidation_inventory)
    liquidation_notional = 0.0
    liquidation_fee = 0.0
    if pre_liquidation_inventory > tolerance:
        liquidation_notional = pre_liquidation_inventory * float(last_bid)
        state["cash_gross_usdt"] += liquidation_notional
        state["inventory_btc"] = 0.0
    elif pre_liquidation_inventory < -tolerance:
        liquidation_notional = -pre_liquidation_inventory * float(last_ask)
        state["cash_gross_usdt"] -= liquidation_notional
        state["inventory_btc"] = 0.0
    if liquidation_qty > tolerance:
        liquidation_fee = (
            liquidation_notional
            * float(spec["costs"]["day_end_taker_fee_bps"])
            / 10_000.0
        )
    total_fees = float(state["maker_fees_usdt"]) + liquidation_fee
    gross_pnl = float(state["cash_gross_usdt"])
    net_pnl = gross_pnl - total_fees
    total_turnover = float(state["maker_turnover_usdt"]) + liquidation_notional
    one_way_notional = total_turnover / 2.0
    net_bps = net_pnl / one_way_notional * 10_000.0 if one_way_notional else np.nan
    return {
        "date": date,
        "policy": policy,
        "maker_quote_attempts": int(state["maker_quote_attempts"]),
        "full_fill_orders": int(state["full_fill_orders"]),
        "partial_fill_orders": int(state["partial_fill_orders"]),
        "maker_fill_orders": int(
            state["full_fill_orders"] + state["partial_fill_orders"]
        ),
        "maker_filled_qty_btc": float(state["maker_filled_qty_btc"]),
        "buy_filled_qty_btc": float(state["buy_filled_qty_btc"]),
        "sell_filled_qty_btc": float(state["sell_filled_qty_btc"]),
        "maker_turnover_usdt": float(state["maker_turnover_usdt"]),
        "maker_fees_usdt": float(state["maker_fees_usdt"]),
        "pre_liquidation_inventory_btc": pre_liquidation_inventory,
        "forced_liquidation_qty_btc": liquidation_qty,
        "forced_liquidation_notional_usdt": liquidation_notional,
        "forced_liquidation_fee_usdt": liquidation_fee,
        "gross_pnl_usdt": gross_pnl,
        "net_pnl_usdt": net_pnl,
        "total_turnover_usdt": total_turnover,
        "one_way_notional_usdt": one_way_notional,
        "net_bps_on_one_way_notional": net_bps,
        "maximum_absolute_inventory_btc": float(
            state["maximum_absolute_inventory_btc"]
        ),
        "inventory_limit_violations": int(state["inventory_limit_violations"]),
        "suppressed_by_inventory": int(state["suppressed_by_inventory"]),
        "suppressed_by_obi": int(state["suppressed_by_obi"]),
        "suppressed_by_selector": int(state["suppressed_by_selector"]),
        "active_order_conflicts": int(state["active_order_conflicts"]),
        "invalid_placements": int(state["invalid_placements"]),
    }


def load_schedule(date: str, spec: dict[str, Any]) -> tuple[pd.DataFrame, float, float]:
    frame = load_day(date)
    model, transforms = _model_inputs()
    signals, context = derive_arrays(frame, model=model, transforms=transforms)
    day_start = int(context["sample_time_us"][0])
    lifetime = int(spec["market"]["quote_lifetime_ms"])
    schedule = pd.read_parquet(
        PASSIVE_ROOT / date / "labeled_probes.parquet",
        columns=SCHEDULE_COLUMNS,
        filters=[("quote_lifetime_ms", "=", lifetime)],
    )
    cadence_us = int(spec["market"]["quote_decision_cadence_ms"]) * 1000
    decision = schedule["decision_time_us"].to_numpy(dtype="int64")
    on_cadence = (decision - day_start) % cadence_us == 0
    schedule = schedule.loc[on_cadence].copy()
    schedule.sort_values(
        ["placement_local_time_us", "decision_time_us", "side"],
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    expected_rows = 86_400 * 2
    if len(schedule) != expected_rows:
        raise ValueError(f"unexpected MM quote schedule size: {len(schedule)}")
    key_counts = schedule.groupby(["decision_time_us", "side"], sort=False).size()
    if len(key_counts) != expected_rows or not key_counts.eq(1).all():
        raise ValueError("MM quote schedule does not have one row per decision and side")
    decision_index = (
        (schedule["decision_time_us"].to_numpy(dtype="int64") - day_start) // 100_000
    ).astype("int64")
    passive_obi = schedule["weighted_obi_l10"].to_numpy(dtype="float64")
    if not np.allclose(
        passive_obi,
        signals["weighted_obi_l10"][decision_index],
        equal_nan=True,
        rtol=0,
        atol=1e-9,
    ):
        raise ValueError("MM passive OBI differs from causal feature source")
    valid = context["valid_book_state"] & np.isfinite(context["best_bid_price"]) & np.isfinite(
        context["best_ask_price"]
    )
    last_valid = np.flatnonzero(valid)
    if not len(last_valid):
        raise ValueError("MM day has no valid final liquidation BBO")
    last = int(last_valid[-1])
    return (
        schedule,
        float(context["best_bid_price"][last]),
        float(context["best_ask_price"][last]),
    )


def evaluate_day(date: str, spec: dict[str, Any]) -> pd.DataFrame:
    schedule, last_bid, last_ask = load_schedule(date, spec)
    return pd.DataFrame([
        simulate_schedule(
            schedule,
            date=date,
            policy=policy,
            last_bid=last_bid,
            last_ask=last_ask,
            spec=spec,
        )
        for policy in POLICIES
    ])


def aggregate_stage(day: pd.DataFrame) -> dict[str, Any]:
    result = {}
    for policy, group in day.groupby("policy", sort=True):
        one_way = float(group["one_way_notional_usdt"].sum())
        net = float(group["net_pnl_usdt"].sum())
        result[policy] = {
            "dates": int(group["date"].nunique()),
            "maker_quote_attempts": int(group["maker_quote_attempts"].sum()),
            "maker_fill_orders": int(group["maker_fill_orders"].sum()),
            "full_fill_orders": int(group["full_fill_orders"].sum()),
            "partial_fill_orders": int(group["partial_fill_orders"].sum()),
            "maker_filled_qty_btc": float(group["maker_filled_qty_btc"].sum()),
            "maker_turnover_usdt": float(group["maker_turnover_usdt"].sum()),
            "gross_pnl_usdt": float(group["gross_pnl_usdt"].sum()),
            "total_fees_usdt": float(
                group["maker_fees_usdt"].sum()
                + group["forced_liquidation_fee_usdt"].sum()
            ),
            "net_pnl_usdt": net,
            "net_bps_on_one_way_notional": net / one_way * 10_000.0
            if one_way else np.nan,
            "positive_days": int((group["net_pnl_usdt"] > 0).sum()),
            "worst_day_net_pnl_usdt": float(group["net_pnl_usdt"].min()),
            "best_day_net_pnl_usdt": float(group["net_pnl_usdt"].max()),
            "maximum_absolute_inventory_btc": float(
                group["maximum_absolute_inventory_btc"].max()
            ),
            "inventory_limit_violations": int(
                group["inventory_limit_violations"].sum()
            ),
            "forced_liquidation_days": int(
                (group["forced_liquidation_qty_btc"] > 0).sum()
            ),
        }
    return result


def validation_gate(
    day: pd.DataFrame,
    policies: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    gate = spec["frozen_validation_gate"]
    obi_day = day.loc[day["policy"] == "obi_aware"]
    obi = policies["obi_aware"]
    neutral = policies["neutral"]
    checks = {
        "obi_aware_net_positive_each_day": bool((obi_day["net_pnl_usdt"] > 0).all()),
        "obi_aware_pooled_net_positive": bool(obi["net_pnl_usdt"] > 0),
        "obi_aware_improves_neutral_pooled_net": bool(
            obi["net_pnl_usdt"] > neutral["net_pnl_usdt"]
        ),
        "obi_aware_minimum_fill_activity": bool(
            obi["maker_fill_orders"]
            >= int(gate["obi_aware_maker_fill_orders_minimum_total"])
        ),
        "zero_inventory_limit_violations": bool(
            obi["inventory_limit_violations"]
            == int(gate["inventory_limit_violations"])
        ),
    }
    return {"checks": checks, "passes": bool(all(checks.values()))}


def _stage_paths(stage: str) -> tuple[Path, Path]:
    return {
        "development": (DEVELOPMENT_DAY_PATH, DEVELOPMENT_SUMMARY_PATH),
        "validation": (VALIDATION_DAY_PATH, VALIDATION_SUMMARY_PATH),
        "replication": (REPLICATION_DAY_PATH, REPLICATION_SUMMARY_PATH),
    }[stage]


def run_stage(stage: str) -> dict[str, Any]:
    spec = audit_spec()
    if stage == "validation":
        if VALIDATION_RECEIPT_PATH.exists():
            raise RuntimeError("MM validation receipt exists; verify instead of reevaluating")
        if not DEVELOPMENT_SUMMARY_PATH.exists():
            raise RuntimeError("MM development stage must run before frozen validation")
    elif stage == "replication":
        if REPLICATION_RECEIPT_PATH.exists():
            raise RuntimeError("MM replication receipt exists; verify instead of reevaluating")
        receipt = _load_json(VALIDATION_RECEIPT_PATH)
        if not receipt["gate_passes"]:
            raise RuntimeError("MM validation failed; replication split must remain unopened")
    split_key = {
        "development": "development_no_tuning",
        "validation": "frozen_validation",
        "replication": "replication_only_if_validation_passes",
    }[stage]
    dates = list(spec["chronological_splits"][split_key])
    day = pd.concat([evaluate_day(date, spec) for date in dates], ignore_index=True)
    day_path, summary_path = _stage_paths(stage)
    write_csv(day_path, day)
    policies = aggregate_stage(day)
    summary: dict[str, Any] = {
        "schema": f"continuous-inventory-mm-{stage}-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "stage": stage,
        "dates": dates,
        "policies": policies,
        "day_metrics_sha256": sha256(day_path),
    }
    if stage == "validation":
        summary["frozen_validation_gate"] = validation_gate(day, policies, spec)
    write_json(summary_path, summary)
    if stage in {"validation", "replication"}:
        receipt_path = (
            VALIDATION_RECEIPT_PATH if stage == "validation" else REPLICATION_RECEIPT_PATH
        )
        gate_passes = (
            summary["frozen_validation_gate"]["passes"]
            if stage == "validation"
            else True
        )
        write_json(receipt_path, {
            "schema": f"continuous-inventory-mm-{stage}-single-use-receipt-v1",
            "spec_sha256": sha256(SPEC_PATH),
            "day_metrics_sha256": sha256(day_path),
            "summary_sha256": sha256(summary_path),
            "evaluation_count": 1,
            "gate_passes": gate_passes,
        })
    return summary


def verify_validation_receipt() -> dict[str, Any]:
    receipt = _load_json(VALIDATION_RECEIPT_PATH)
    checks = {
        "spec": receipt["spec_sha256"] == sha256(SPEC_PATH),
        "day_metrics": receipt["day_metrics_sha256"] == sha256(VALIDATION_DAY_PATH),
        "summary": receipt["summary_sha256"] == sha256(VALIDATION_SUMMARY_PATH),
        "single_use": receipt["evaluation_count"] == 1,
    }
    return {"checks": checks, "valid": bool(all(checks.values())), "receipt": receipt}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("audit", "development", "validation", "replication", "verify"),
    )
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_spec()
    elif args.command == "verify":
        result = verify_validation_receipt()
    else:
        result = run_stage(args.command)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
