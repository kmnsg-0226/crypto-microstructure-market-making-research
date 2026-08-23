"""Build the final passive-fill research tables and Markdown report."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.passive.analysis import hac_mean_se
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
PASSIVE_ROOT = ROOT / "data/research/tardis/passive"
REPORT_ROOT = ROOT / "data/research/tardis/reports/passive"
SPEC_PATH = ROOT / "research/specs/maker_research_spec_frozen.json"
SPEC_HASH_PATH = ROOT / "research/specs/maker_research_spec_frozen.json.sha256"
DOC_PATH = ROOT / "docs/passive_fill_adverse_selection_research.md"

STAGES = {
    "development": [
        "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01",
    ],
    "validation": ["2026-06-01"],
    "historical_holdout": ["2026-07-01", "2026-08-01"],
}
STAGE_LABELS = {
    "development": "Jan-May development",
    "validation": "June validation",
    "historical_holdout": "Jul/Aug historical holdout",
}
RESOLVED = {"full", "partial", "unfilled"}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _distribution(values: np.ndarray) -> dict[str, Any]:
    clean = values[np.isfinite(values)]
    quantiles = np.quantile(clean, [0.05, 0.25, 0.5, 0.75, 0.95])
    return {
        "observations": int(len(clean)),
        "mean_ticks": float(clean.mean()),
        "median_ticks": float(quantiles[2]),
        "negative_probability": float(np.mean(clean < 0)),
        "p05_ticks": float(quantiles[0]),
        "p25_ticks": float(quantiles[1]),
        "p75_ticks": float(quantiles[3]),
        "p95_ticks": float(quantiles[4]),
    }


def _headline_raw(stage: str, dates: list[str]) -> dict[str, Any]:
    columns = [
        "date", "decision_time_us", "side", "quote_lifetime_ms", "fill_status",
        "filled_qty", "combined_prediction_1s_ticks", "maker_markout_1s_ticks",
        "post_fill_mid_move_1s_ticks", "fill_price_advantage_ticks",
        "maker_markout_1s_bps", "maker_markout_1s_usdt_per_1000",
        "optimistic_fill_status", "optimistic_filled_qty",
        "optimistic_maker_markout_1s_ticks",
    ]
    candidate_counts: dict[tuple[str, str], int] = {}
    resolved_counts: dict[tuple[str, str], int] = {}
    full_counts: dict[tuple[str, str], int] = {}
    filled_values: dict[tuple[str, str], list[pd.DataFrame]] = {}
    for policy in ("always_quote", "alpha_filtered"):
        for side in ("bid", "ask"):
            filled_values[policy, side] = []
            candidate_counts[policy, side] = 0
            resolved_counts[policy, side] = 0
            full_counts[policy, side] = 0
    queue_values: dict[tuple[str, str], list[np.ndarray]] = {}
    queue_counts: dict[tuple[str, str, str], int] = {}
    for model in ("pessimistic_visible_queue", "optimistic_front_of_queue_upper_bound"):
        for side in ("bid", "ask"):
            queue_values[model, side] = []
            for count in ("candidate", "resolved", "full", "partial"):
                queue_counts[model, side, count] = 0

    for day_code, date in enumerate(dates):
        frame = pd.read_parquet(
            PASSIVE_ROOT / date / "labeled_probes.parquet",
            columns=columns,
            filters=[("quote_lifetime_ms", "=", 1000)],
        )
        if not frame["quote_lifetime_ms"].eq(1000).all():
            raise ValueError("Parquet lifetime filter failed")
        prediction = frame["combined_prediction_1s_ticks"]
        threshold_report = _load_json(REPORT_ROOT / "development_signal_thresholds.json")
        bearish = float(threshold_report["bearish_threshold_ticks"])
        bullish = float(threshold_report["bullish_threshold_ticks"])
        eligible = (
            (frame["side"].eq("bid") & prediction.ge(bearish))
            | (frame["side"].eq("ask") & prediction.le(bullish))
        )
        for policy, policy_mask in (
            ("always_quote", pd.Series(True, index=frame.index)),
            ("alpha_filtered", eligible),
        ):
            for side in ("bid", "ask"):
                selected = frame.loc[policy_mask & frame["side"].eq(side)]
                key = policy, side
                candidate_counts[key] += len(selected)
                resolved_counts[key] += int(selected["fill_status"].isin(RESOLVED).sum())
                full_counts[key] += int(selected["fill_status"].eq("full").sum())
                filled = selected.loc[
                    selected["filled_qty"].gt(0)
                    & selected["maker_markout_1s_ticks"].notna(),
                    [
                        "maker_markout_1s_ticks", "post_fill_mid_move_1s_ticks",
                        "fill_price_advantage_ticks", "maker_markout_1s_bps",
                        "maker_markout_1s_usdt_per_1000",
                    ],
                ].copy()
                filled["day_code"] = day_code
                filled_values[key].append(filled)

        for model, prefix in (
            ("pessimistic_visible_queue", ""),
            ("optimistic_front_of_queue_upper_bound", "optimistic_"),
        ):
            status = f"{prefix}fill_status"
            qty = f"{prefix}filled_qty"
            markout = f"{prefix}maker_markout_1s_ticks"
            for side in ("bid", "ask"):
                selected = frame.loc[frame["side"].eq(side)]
                queue_counts[model, side, "candidate"] += len(selected)
                queue_counts[model, side, "resolved"] += int(selected[status].isin(RESOLVED).sum())
                queue_counts[model, side, "full"] += int(selected[status].eq("full").sum())
                queue_counts[model, side, "partial"] += int(selected[status].eq("partial").sum())
                values = selected.loc[selected[qty].gt(0), markout].dropna().to_numpy("float64")
                queue_values[model, side].append(values)

    policy_rows = []
    policy_frames: dict[tuple[str, str], pd.DataFrame] = {}
    for policy in ("always_quote", "alpha_filtered"):
        for side in ("bid", "ask"):
            key = policy, side
            filled = pd.concat(filled_values[key], ignore_index=True)
            policy_frames[key] = filled
            stats = _distribution(filled["maker_markout_1s_ticks"].to_numpy("float64"))
            resolved = resolved_counts[key]
            policy_rows.append({
                "stage": stage,
                "policy": policy,
                "side": side,
                "quote_lifetime_ms": 1000,
                "markout_horizon_ms": 1000,
                "candidate_quotes": candidate_counts[key],
                "resolved_quotes": resolved,
                "full_fills": full_counts[key],
                "filled_with_label": len(filled),
                "full_fill_probability": full_counts[key] / resolved,
                "any_labeled_fill_probability": len(filled) / resolved,
                **stats,
            })

    headline_rows = []
    baseline = pd.concat(
        [policy_frames["always_quote", "bid"], policy_frames["always_quote", "ask"]],
        ignore_index=True,
    )
    for side, filled in (
        ("bid", policy_frames["always_quote", "bid"]),
        ("ask", policy_frames["always_quote", "ask"]),
        ("both", baseline),
    ):
        stats = _distribution(filled["maker_markout_1s_ticks"].to_numpy("float64"))
        headline_rows.append({
            "stage": stage,
            "side": side,
            "quote_lifetime_ms": 1000,
            "markout_horizon_ms": 1000,
            **stats,
            "post_fill_mid_move_mean_ticks": float(filled["post_fill_mid_move_1s_ticks"].mean()),
            "fill_price_advantage_mean_ticks": float(filled["fill_price_advantage_ticks"].mean()),
            "gross_passive_edge_mean_bps": float(filled["maker_markout_1s_bps"].mean()),
            "gross_passive_edge_mean_usdt_per_1000": float(
                filled["maker_markout_1s_usdt_per_1000"].mean()
            ),
            "hac_mean_standard_error_ticks": hac_mean_se(
                filled["maker_markout_1s_ticks"].to_numpy("float64"),
                filled["day_code"].to_numpy("int64"),
                10,
            ),
            "hac_max_lag_filled_observations": 10,
        })

    queue_rows = []
    for model in ("pessimistic_visible_queue", "optimistic_front_of_queue_upper_bound"):
        for side in ("bid", "ask", "both"):
            selected_sides = ("bid", "ask") if side == "both" else (side,)
            values = np.concatenate([
                item
                for selected_side in selected_sides
                for item in queue_values[model, selected_side]
            ])
            counts = {
                name: sum(queue_counts[model, selected_side, name] for selected_side in selected_sides)
                for name in ("candidate", "resolved", "full", "partial")
            }
            stats = _distribution(values)
            queue_rows.append({
                "stage": stage,
                "queue_model": model,
                "side": side,
                "quote_lifetime_ms": 1000,
                "markout_horizon_ms": 1000,
                "candidate_quotes": counts["candidate"],
                "resolved_quotes": counts["resolved"],
                "full_fills": counts["full"],
                "partial_fills": counts["partial"],
                "full_fill_probability": counts["full"] / counts["resolved"],
                **stats,
            })
    return {
        "headline": headline_rows,
        "filter": policy_rows,
        "queue": queue_rows,
    }


def _fill_rates() -> pd.DataFrame:
    rows = []
    for stage in STAGES:
        frame = pd.read_csv(REPORT_ROOT / stage / "fill_probability_by_day_and_obi.csv")
        frame = frame.loc[frame["signal"].eq("all")]
        for (side, lifetime), group in frame.groupby(["side", "quote_lifetime_ms"]):
            resolved = int(group["resolved_quotes"].sum())
            full = int(group["full_fills"].sum())
            partial = int(group["partial_fills"].sum())
            rows.append({
                "stage": stage,
                "side": side,
                "quote_lifetime_ms": int(lifetime),
                "candidate_quotes": int(group["candidate_quotes"].sum()),
                "resolved_quotes": resolved,
                "full_fills": full,
                "partial_fills": partial,
                "full_fill_probability": full / resolved,
                "any_fill_probability": (full + partial) / resolved,
                "average_queue_ahead_btc": float(np.average(
                    group["average_queue_ahead_btc"], weights=group["resolved_quotes"]
                )),
                "average_order_to_displayed_ratio": float(np.average(
                    group["average_order_to_displayed_ratio"], weights=group["resolved_quotes"]
                )),
            })
    return pd.DataFrame(rows)


def _obi_regimes() -> tuple[pd.DataFrame, pd.DataFrame]:
    regime_rows = []
    depth_rows = []
    for stage in STAGES:
        frame = pd.read_csv(REPORT_ROOT / stage / "maker_markout_by_day_and_obi.csv")
        selected = frame.loc[
            frame["date"].eq("ALL")
            & frame["quote_lifetime_ms"].eq(1000)
            & frame["markout_horizon_ms"].eq(1000)
        ]
        for (signal, side, regime), group in selected.groupby(
            ["signal", "side", "obi_regime"]
        ):
            observations = int(group["maker_markout_observations"].sum())
            regime_rows.append({
                "stage": stage,
                "signal": signal,
                "side": side,
                "obi_regime": regime,
                "observations": observations,
                "maker_markout_mean_ticks": float(np.average(
                    group["maker_markout_mean"], weights=group["maker_markout_observations"]
                )),
                "negative_probability": float(np.average(
                    group["maker_markout_negative_probability"],
                    weights=group["maker_markout_observations"],
                )),
            })
        regimes = pd.DataFrame(regime_rows)
        current = regimes.loc[regimes["stage"].eq(stage)]
        for signal in ("obi_l1", "obi_l5", "obi_l10"):
            values = current.loc[current["signal"].eq(signal)].set_index(
                ["side", "obi_regime"]
            )["maker_markout_mean_ticks"]
            depth_rows.append({
                "stage": stage,
                "signal": signal,
                "bid_positive_minus_negative_ticks": float(
                    values["bid", "positive"] - values["bid", "negative"]
                ),
                "ask_negative_minus_positive_ticks": float(
                    values["ask", "negative"] - values["ask", "positive"]
                ),
            })
    return pd.DataFrame(regime_rows), pd.DataFrame(depth_rows)


def _day_stability() -> pd.DataFrame:
    rows = []
    for stage in STAGES:
        fill = pd.read_csv(REPORT_ROOT / stage / "fill_probability_by_day_and_obi.csv")
        markout = pd.read_csv(REPORT_ROOT / stage / "maker_markout_by_day_and_obi.csv")
        filtered = pd.read_csv(REPORT_ROOT / stage / "quote_filter_by_day.csv")
        for date in sorted(fill["date"].unique()):
            row: dict[str, Any] = {"stage": stage, "date": date}
            for side in ("bid", "ask"):
                fill_row = fill.loc[
                    fill["date"].eq(date) & fill["signal"].eq("all")
                    & fill["side"].eq(side) & fill["quote_lifetime_ms"].eq(1000)
                ].iloc[0]
                row[f"{side}_full_fill_probability"] = (
                    fill_row["full_fills"] / fill_row["resolved_quotes"]
                )
                mark = markout.loc[
                    markout["date"].eq(date) & markout["signal"].eq("obi_l5")
                    & markout["side"].eq(side) & markout["quote_lifetime_ms"].eq(1000)
                    & markout["markout_horizon_ms"].eq(1000)
                ]
                row[f"{side}_maker_markout_mean_ticks"] = float(np.average(
                    mark["maker_markout_mean"], weights=mark["maker_markout_observations"]
                ))
                policies = filtered.loc[
                    filtered["date"].eq(date) & filtered["side"].eq(side)
                    & filtered["quote_lifetime_ms"].eq(1000)
                    & filtered["markout_horizon_ms"].eq(1000)
                ]
                means = {
                    policy: float(np.average(
                        group["maker_markout_mean"], weights=group["filled_with_label"]
                    ))
                    for policy, group in policies.groupby("policy")
                }
                row[f"{side}_alpha_filter_delta_ticks"] = (
                    means["alpha_filtered"] - means["always_quote"]
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _horizon_lifetime_sensitivity() -> pd.DataFrame:
    rows = []
    for stage in STAGES:
        frame = pd.read_csv(REPORT_ROOT / stage / "maker_markout_by_day_and_obi.csv")
        selected = frame.loc[
            frame["date"].eq("ALL") & frame["signal"].eq("obi_l5")
        ]
        for (lifetime, horizon), group in selected.groupby(
            ["quote_lifetime_ms", "markout_horizon_ms"]
        ):
            observations = int(group["maker_markout_observations"].sum())
            rows.append({
                "stage": stage,
                "quote_lifetime_ms": int(lifetime),
                "markout_horizon_ms": int(horizon),
                "observations": observations,
                "maker_markout_mean_ticks": float(np.average(
                    group["maker_markout_mean"],
                    weights=group["maker_markout_observations"],
                )),
                "post_fill_mid_move_mean_ticks": float(np.average(
                    group["post_fill_mid_move_mean_ticks"],
                    weights=group["maker_markout_observations"],
                )),
            })
    return pd.DataFrame(rows)


def _invalidations() -> pd.DataFrame:
    parts = []
    for stage in STAGES:
        frame = pd.read_csv(REPORT_ROOT / stage / "day_summary.csv")
        frame.insert(0, "stage", stage)
        parts.append(frame)
    return pd.concat(parts, ignore_index=True)


def _markdown_table(frame: pd.DataFrame, columns: list[tuple[str, str]], formats: dict[str, str]) -> str:
    header = "| " + " | ".join(label for _, label in columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, separator]
    for row in frame.itertuples(index=False):
        values = []
        data = row._asdict()
        for column, _ in columns:
            value = data[column]
            if column in formats:
                values.append(formats[column].format(value))
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def build_final_report(code_commit: str) -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    spec_hash = sha256(SPEC_PATH)
    if SPEC_HASH_PATH.read_text(encoding="utf-8").strip() != spec_hash:
        raise ValueError("maker spec hash sidecar mismatch")
    fill_rates = _fill_rates()
    regimes, depth = _obi_regimes()
    days = _day_stability()
    sensitivity = _horizon_lifetime_sensitivity()
    invalidations = _invalidations()
    headline_parts = []
    filter_parts = []
    queue_parts = []
    for stage, dates in STAGES.items():
        result = _headline_raw(stage, dates)
        headline_parts.extend(result["headline"])
        filter_parts.extend(result["filter"])
        queue_parts.extend(result["queue"])
    headline = pd.DataFrame(headline_parts)
    filters = pd.DataFrame(filter_parts)
    queues = pd.DataFrame(queue_parts)

    final_dir = REPORT_ROOT / "final"
    write_csv(final_dir / "fill_rates.csv", fill_rates)
    write_csv(final_dir / "headline_markout.csv", headline)
    write_csv(final_dir / "quote_filter_comparison.csv", filters)
    write_csv(final_dir / "queue_model_sensitivity.csv", queues)
    write_csv(final_dir / "obi_regime_markout.csv", regimes)
    write_csv(final_dir / "obi_depth_comparison.csv", depth)
    write_csv(final_dir / "day_stability.csv", days)
    write_csv(final_dir / "horizon_lifetime_sensitivity.csv", sensitivity)
    write_csv(final_dir / "invalidations.csv", invalidations)

    total = invalidations.select_dtypes(include="number").sum()
    manual = _load_json(REPORT_ROOT / "stage_a_may/manual_fill_audit.json")
    threshold = _load_json(REPORT_ROOT / "development_signal_thresholds.json")
    opening = _load_json(REPORT_ROOT / "historical_holdout/holdout_opening_audit.json")
    summary = {
        "schema": "passive-fill-adverse-selection-final-v1",
        "code_commit": code_commit,
        "maker_spec": _relative(SPEC_PATH),
        "maker_spec_sha256": spec_hash,
        "split": spec["split"],
        "candidate_quotes": int(total["candidate_quotes"]),
        "full_fills": int(total["full_fills"]),
        "partial_fills": int(total["partial_fills"]),
        "invalid_at_placement": int(total["invalid_at_placement"]),
        "snapshot_invalidations": int(total["invalid_snapshot"]),
        "day_boundary_invalidations": int(total["invalid_day_boundary"]),
        "quote_quantity_btc": spec["placement"]["quote_quantity_btc"],
        "quote_lifetimes_ms": spec["placement"]["quote_lifetimes_ms"],
        "filter_thresholds_ticks": {
            "bearish_20pct": threshold["bearish_threshold_ticks"],
            "bullish_80pct": threshold["bullish_threshold_ticks"],
        },
        "holdout_opening_audit_sha256": sha256(
            REPORT_ROOT / "historical_holdout/holdout_opening_audit.json"
        ),
        "manual_audit": {
            "examples": manual["sample_count"],
            "consistent_examples": manual["consistent_examples"],
            "all_fill_fields_match": manual["all_fill_fields_match"],
        },
        "headline_definition": "1s fixed BBO quote, pessimistic visible queue, 1s post-fill markout",
        "headline_by_stage": headline.loc[headline["side"].eq("both")].to_dict("records"),
        "conclusion": (
            "The frozen alpha is descriptively related to maker selection, but the predeclared "
            "quote filter worsened conditional markout in development, validation, and historical "
            "holdout. Conservative passive edge was negative before fees, so these data do not "
            "yet justify a full inventory-aware market-making implementation."
        ),
        "profitability_claim": False,
    }
    write_json(final_dir / "final_summary.json", summary)

    fill_display = fill_rates.copy()
    fill_display["stage"] = fill_display["stage"].map(STAGE_LABELS)
    headline_display = headline.loc[headline["side"].eq("both")].copy()
    headline_display["stage"] = headline_display["stage"].map(STAGE_LABELS)
    filter_display = filters.copy()
    baseline = filter_display.loc[filter_display["policy"].eq("always_quote")].set_index(
        ["stage", "side"]
    )
    filtered = filter_display.loc[filter_display["policy"].eq("alpha_filtered")].set_index(
        ["stage", "side"]
    )
    filter_rows = []
    for key, row in filtered.iterrows():
        base = baseline.loc[key]
        filter_rows.append({
            "stage": STAGE_LABELS[key[0]],
            "side": key[1],
            "candidate_retained": row["candidate_quotes"] / base["candidate_quotes"],
            "fill_probability_before": base["any_labeled_fill_probability"],
            "fill_probability_after": row["any_labeled_fill_probability"],
            "markout_before": base["mean_ticks"],
            "markout_after": row["mean_ticks"],
            "markout_delta": row["mean_ticks"] - base["mean_ticks"],
            "p05_delta": row["p05_ticks"] - base["p05_ticks"],
        })
    filter_delta = pd.DataFrame(filter_rows)
    regime_display = regimes.loc[
        regimes["signal"].eq("obi_l5") & regimes["obi_regime"].isin(["negative", "positive"])
    ].copy()
    regime_display["stage"] = regime_display["stage"].map(STAGE_LABELS)
    queue_display = queues.loc[queues["side"].eq("both")].copy()
    queue_display["stage"] = queue_display["stage"].map(STAGE_LABELS)
    depth_display = depth.copy()
    depth_display["stage"] = depth_display["stage"].map(STAGE_LABELS)
    day_display = days.copy()
    day_display["stage"] = day_display["stage"].map(STAGE_LABELS)
    sensitivity_display = sensitivity.pivot(
        index=["stage", "quote_lifetime_ms"],
        columns="markout_horizon_ms",
        values="maker_markout_mean_ticks",
    ).reset_index().rename(columns={
        100: "markout_100ms_ticks",
        500: "markout_500ms_ticks",
        1000: "markout_1s_ticks",
        5000: "markout_5s_ticks",
    })
    sensitivity_display["stage"] = sensitivity_display["stage"].map(STAGE_LABELS)

    markdown = f"""# Passive fill and adverse-selection research

## Outcome

Under the frozen conservative L2 assumptions, the passive BBO experiment does **not** yet
justify implementing a full inventory-aware market maker. The engine found economically severe
selection: for the headline 1-second quote and 1-second post-fill horizon, mean maker markout was
negative in development, June validation, and the July/August historical holdout. The
predeclared frozen-alpha quote filter also made mean conditional markout worse on both sides in
all three splits. This is a negative research result, not a claim that maker trading is
universally impossible and not a portfolio profitability backtest.

The frozen alpha still has descriptive value: OBI strongly separates conditional fill quality.
But the empirical direction is opposite the preregistered safety hypothesis. Positive OBI was
associated with *worse* filled-bid markout and *less bad* filled-ask markout; negative OBI showed
the mirror image. That inversion survived June and the historical holdout. A reversed rule was
not tested after seeing these results.

## Audit trail and frozen method

- Maker spec: `research/specs/maker_research_spec_frozen.json`
- Maker spec SHA-256: `{spec_hash}`
- Implementation commit: `{code_commit}`
- Alpha spec SHA-256: `{spec['audit']['alpha_spec_sha256']}`
- Execution spec SHA-256: `{spec['audit']['execution_spec_sha256']}`
- L2/trade bundle SHA-256: `{spec['audit']['dataset_bundle_sha256']}`
- Holdout opening audit SHA-256: `{sha256(REPORT_ROOT / 'historical_holdout/holdout_opening_audit.json')}`
- Maker source bundle SHA-256: `{spec['audit']['maker_source_bundle_sha256']}`
- Passive engine binary SHA-256 at freeze: `{spec['audit']['passive_binary_sha256']}`

The split was Jan-May development, June validation, and Jul/Aug historical holdout. Jul/Aug had
already been seen in earlier alpha/taker work, so it is only a holdout for this maker-fill
methodology. The methodology was frozen before June and before opening Jul/Aug maker outcomes.
The strongest genuine forward OOS remains the still-collecting native Binance sample.

Every 100 ms, the experiment independently placed a `{spec['placement']['quote_quantity_btc']}`
BTC buy at best bid and sell at best ask for 100, 500, 1,000, or 5,000 ms. Price was fixed and
never chased. The headline queue model joins behind all displayed BBO quantity; only opposite
aggressor trades at the exact quote consume that queue; cancellations never help; additions are
behind; and a strict trade-through fills the remainder. Aggregated L2 cannot establish exact
FIFO, so this is explicitly an approximation.

Events are ordered by Tardis `local_timestamp` and then preserved source order. A new probe is
placed after same-local market events; it expires before an exact-expiry event. A replacement
snapshot cancels a surviving order. Normalized CSV has no disconnect messages, so replacement
snapshots are the available reconnect proxy. Vendor local time is capture-order metadata, not
assumed real trading latency.

## Scale and data validity

Across eight dates there were `{int(total['candidate_quotes']):,}` candidate orders,
`{int(total['full_fills']):,}` full fills, and `{int(total['partial_fills']):,}` partial fills.
There were `{int(total['invalid_at_placement']):,}` invalid-at-placement candidates,
`{int(total['invalid_snapshot']):,}` snapshot invalidations, and
`{int(total['invalid_day_boundary']):,}` day-boundary invalidations. Gap/snapshot survival was
never fabricated.

## Fill probability by side and lifetime

{_markdown_table(
    fill_display,
    [
        ('stage', 'split'), ('side', 'side'), ('quote_lifetime_ms', 'life ms'),
        ('full_fills', 'full fills'), ('partial_fills', 'partial'),
        ('full_fill_probability', 'full fill %'),
    ],
    {'full_fills': '{:,.0f}', 'partial_fills': '{:,.0f}',
     'full_fill_probability': '{:.2%}'},
)}

Fill probability rises sharply with lifetime and is highly day-dependent. For the headline
1-second quotes it was about 16.7-17.2% by side in development, 15.3% in June, and 11.7-11.9%
in the historical holdout. This is a probe fill rate, not continuous quoting capacity.

## Headline adverse selection

Maker markout is measured directly from fill price, so it already includes passive price
advantage. The decomposition is `fill-price advantage + post-fill mid move`; no extra
half-spread is added.

{_markdown_table(
    headline_display,
    [
        ('stage', 'split'), ('observations', 'labeled fills'), ('mean_ticks', 'maker mean ticks'),
        ('post_fill_mid_move_mean_ticks', 'post-fill move ticks'),
        ('fill_price_advantage_mean_ticks', 'fill-price advantage ticks'),
        ('negative_probability', 'P(markout < 0)'),
        ('hac_mean_standard_error_ticks', 'HAC SE ticks'),
        ('gross_passive_edge_mean_bps', 'gross bps'),
    ],
    {'observations': '{:,.0f}', 'mean_ticks': '{:.2f}',
     'post_fill_mid_move_mean_ticks': '{:.2f}',
     'fill_price_advantage_mean_ticks': '{:.2f}',
     'negative_probability': '{:.2%}', 'hac_mean_standard_error_ticks': '{:.2f}',
     'gross_passive_edge_mean_bps': '{:.3f}'},
)}

The 1-second markout distribution is adverse, not merely noisy: negative-markout probability is
88.5% in development, 90.9% in June, and 89.3% in holdout. HAC/Newey-West standard errors use
10 sequential filled-observation lags for the 1-second lifetime/horizon and reset at day
boundaries. Day-level tables are also retained because 100 ms probes overlap heavily.

## Quote-lifetime and markout-horizon sensitivity

Each cell is the combined bid/ask mean maker markout in ticks. Rows change fixed quote lifetime;
columns change the post-fill evaluation horizon.

{_markdown_table(
    sensitivity_display,
    [
        ('stage', 'split'), ('quote_lifetime_ms', 'quote life ms'),
        ('markout_100ms_ticks', '100ms markout'),
        ('markout_500ms_ticks', '500ms markout'),
        ('markout_1s_ticks', '1s markout'), ('markout_5s_ticks', '5s markout'),
    ],
    {'markout_100ms_ticks': '{:.2f}', 'markout_500ms_ticks': '{:.2f}',
     'markout_1s_ticks': '{:.2f}', 'markout_5s_ticks': '{:.2f}'},
)}

Longer horizons generally make adverse selection worse. Quote lifetime materially changes who
gets filled, but it does not restore a positive mean in any split/horizon cell.

## OBI-conditioned fill quality

The following uses frozen development OBI-L5 deciles, grouped as negative (1-3) and positive
(8-10), with 1-second quote life and 1-second markout.

{_markdown_table(
    regime_display,
    [
        ('stage', 'split'), ('side', 'side'), ('obi_regime', 'OBI regime'),
        ('observations', 'fills'), ('maker_markout_mean_ticks', 'mean ticks'),
        ('negative_probability', 'P(negative)'),
    ],
    {'observations': '{:,.0f}', 'maker_markout_mean_ticks': '{:.2f}',
     'negative_probability': '{:.2%}'},
)}

The preregistered hypothesis was not supported. In development, positive-minus-negative OBI
changed filled-bid markout by -24.40 ticks; negative-minus-positive changed filled-ask markout by
-22.51 ticks. The same signs remained in June (-10.85/-17.74) and holdout (-9.59/-17.07).
This is evidence that OBI identifies selection regimes, but not evidence for the originally
proposed quote-side rule.

## L1 versus L5 versus L10

{_markdown_table(
    depth_display,
    [
        ('stage', 'split'), ('signal', 'signal'),
        ('bid_positive_minus_negative_ticks', 'bid pos-neg ticks'),
        ('ask_negative_minus_positive_ticks', 'ask neg-pos ticks'),
    ],
    {'bid_positive_minus_negative_ticks': '{:.2f}',
     'ask_negative_minus_positive_ticks': '{:.2f}'},
)}

L1, L5, and L10 tell nearly the same descriptive story; depth choice changes the regime
contrasts by only a few ticks and never changes their signs. This experiment provides no
material evidence that L5/L10 is required over L1 for this maker-selection diagnostic.

## Frozen-alpha quote filter

The filter thresholds were fixed from development prediction values alone: bid predictions had
to be at least `{threshold['bearish_threshold_ticks']:.4f}` ticks and ask predictions at most
`{threshold['bullish_threshold_ticks']:.4f}` ticks. No maker outcome selected these thresholds.

{_markdown_table(
    filter_delta,
    [
        ('stage', 'split'), ('side', 'side'), ('candidate_retained', 'candidates retained'),
        ('fill_probability_before', 'fill before'), ('fill_probability_after', 'fill after'),
        ('markout_before', 'mean before'), ('markout_after', 'mean after'),
        ('markout_delta', 'mean delta ticks'), ('p05_delta', 'p05 delta ticks'),
    ],
    {'candidate_retained': '{:.2%}', 'fill_probability_before': '{:.2%}',
     'fill_probability_after': '{:.2%}', 'markout_before': '{:.2f}',
     'markout_after': '{:.2f}', 'markout_delta': '{:.2f}', 'p05_delta': '{:.2f}'},
)}

The filter reduced fill opportunity and worsened mean markout on both sides in every split and
on every individual day. It therefore fails the predeclared quote-filter success test. Testing
an inverted rule now would be post-selection and requires a new preregistration plus forward
data.

## Queue-model sensitivity

{_markdown_table(
    queue_display,
    [
        ('stage', 'split'), ('queue_model', 'queue model'),
        ('full_fill_probability', 'full fill %'), ('observations', 'labeled fills'),
        ('mean_ticks', 'mean maker ticks'), ('negative_probability', 'P(negative)'),
    ],
    {'full_fill_probability': '{:.2%}', 'observations': '{:,.0f}',
     'mean_ticks': '{:.2f}', 'negative_probability': '{:.2%}'},
)}

The optimistic model is a deliberately non-FIFO front-of-queue upper bound, not the headline.
It greatly increases fill rates and reduces adverse selection, showing that queue position is a
first-order uncertainty. Even its mean 1-second markout remains negative in every split.

## Maker fee and economic envelope

At the headline horizon, mean pre-fee edge was -0.878 bps in development, -0.854 bps in June,
and -0.867 bps in holdout. Since gross edge is already negative, no nonnegative maker fee can
preserve it; the required break-even rebate is approximately 0.85-0.88 bps. The configured
sensitivity values are -1, 0, +1, and +2 bps, with negative meaning a rebate. This is edge per
filled notional, not portfolio PnL, maker Sharpe, or a universal exchange-fee claim.

## Day-by-day stability

{_markdown_table(
    day_display,
    [
        ('stage', 'split'), ('date', 'date'),
        ('bid_full_fill_probability', 'bid fill'), ('ask_full_fill_probability', 'ask fill'),
        ('bid_maker_markout_mean_ticks', 'bid mean'),
        ('ask_maker_markout_mean_ticks', 'ask mean'),
        ('bid_alpha_filter_delta_ticks', 'bid filter delta'),
        ('ask_alpha_filter_delta_ticks', 'ask filter delta'),
    ],
    {'bid_full_fill_probability': '{:.2%}', 'ask_full_fill_probability': '{:.2%}',
     'bid_maker_markout_mean_ticks': '{:.2f}', 'ask_maker_markout_mean_ticks': '{:.2f}',
     'bid_alpha_filter_delta_ticks': '{:.2f}', 'ask_alpha_filter_delta_ticks': '{:.2f}'},
)}

Fill rates vary widely, especially February versus January/August, but maker markout and filter
deltas are negative on all eight days. Pooling is not hiding a sign reversal.

## Manual audit, determinism, and tests

Stage A used May 1. Placement CSV, 6,912,000-row compressed probe output, and labeled Parquet
were each regenerated twice; both probe and label outputs were byte-identical. The manual audit
sampled 21 examples: three each of filled bid, filled ask, unfilled bid, unfilled ask, partial,
trade-through, and snapshot-invalidated probes. All 21 matched raw-trade replay for filled
quantity, first/full local and exchange timestamps, and fill reason. Each trace includes
placement, queue, relevant trades/L2 rows, fill decision, fill-grid mid, and 1-second future mid.

The final verification ran 11 C++ tests and 39 Python tests. Synthetic cases cover bid/ask queue
consumption, partial/full/no fill, trade-through, fixed-price behavior, exact expiry ordering,
markout direction/decomposition, gap recovery, deterministic replay/merge, leakage gates, and
split isolation. Integration artifact checks cover snapshot invalidation and Stage A byte
determinism.

Reproduction commands:

```bash
.venv/bin/cmake -S cpp -B build/cpp
.venv/bin/cmake --build build/cpp -j 4
./build/cpp/crypto_l2_tests
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pyresearch.passive.pipeline stage-a
.venv/bin/python -m pyresearch.passive.pipeline development
.venv/bin/python -m pyresearch.passive.pipeline validation
.venv/bin/python -m pyresearch.passive.pipeline holdout
.venv/bin/python -m pyresearch.passive.reporting --code-commit {code_commit}
```

## Files and unresolved limitations

Added/modified implementation files are the passive C++ queue/index and CLI, CMake/tests, the
`passive_research` placement/label/audit/analysis/pipeline/reporting modules, maker draft/frozen
specifications, Python tests, README, and this document. Machine outputs live under
`data/research/tardis/reports/passive/` and day-level probe artifacts under
`data/research/tardis/passive/`.

Limitations remain material: aggregated L2 cannot reveal individual-order FIFO; cross-channel
ordering is vendor capture order rather than exchange matching-engine order; downloadable CSV
does not expose disconnect records; snapshot resets only proxy reconnects; the optimistic queue
bound is intentionally unrealistic; independent 100 ms probes overlap and are not a continuous
order process; there is no inventory, reservation price, dynamic quote width, portfolio PnL,
maker Sharpe, or live fill calibration. Most importantly, the historical first-of-month sample
is only eight days and Jul/Aug is not globally untouched. The native collector must run longer
before a preregistered forward test.

The defensible conclusion is narrow: frozen OBI is associated with passive-fill selection, but
the direction defeats the predeclared quote filter and conservative maker edge is negative
before fees. A full alpha-aware inventory-constrained market maker is not justified by this
milestone alone.
"""
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(markdown, encoding="utf-8")
    return summary


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-commit", required=True)
    args = parser.parse_args()
    print(json.dumps(build_final_report(args.code_commit), sort_keys=True))


if __name__ == "__main__":
    main()
