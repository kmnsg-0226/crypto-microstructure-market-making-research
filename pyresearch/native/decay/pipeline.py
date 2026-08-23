"""Phase 6 pipeline.

    python -m pyresearch.native.decay.pipeline frame        # build and gate the decay frame
    python -m pyresearch.native.decay.pipeline analyse      # every descriptive table
    python -m pyresearch.native.decay.pipeline verdict      # classification and project verdict
    python -m pyresearch.native.decay.pipeline all
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from pyresearch.native.decay import analysis, data, spec

SLIM = [
    "file_index",
    "segment_id",
    "timestamp_ns",
    "mid_ticks",
    "fold",
    "seconds_into_validation_block",
    "corpus_seconds",
    "demean_block",
    "utc_day",
    "segment_key",
    "has_all_signals",
]


def slim_frame() -> pd.DataFrame:
    frame = data.read_frame()
    columns = (
        SLIM
        + list(spec.SIGNALS)
        + [f"markout_{h}s_ticks" for h in spec.HORIZONS_S]
        + [f"anchor_{h}s" for h in spec.HORIZONS_S]
    )
    return frame[columns]


def stage_frame() -> None:
    frame = data.build_frame()
    agreement = data.check_frozen_agreement(frame)
    phase5a = data.check_phase5a_agreement(frame)
    passes, problems = data.agreement_passes(agreement, phase5a)
    report = {
        "target_agreement_vs_frozen_phase1": agreement,
        "target_agreement_vs_phase5a_reconstruction": phase5a,
        "agreement_passes": passes,
        "problems": problems,
        "rule": "regenerated 1 s and 5 s must equal the frozen phase 1 columns exactly; "
        "disagreement stops the phase",
        "rows_total": int(frame.shape[0]),
        "rows_with_every_signal": int(frame["has_all_signals"].sum()),
    }
    data.write_json("target_agreement.json", report)
    if not passes:
        raise SystemExit(f"target agreement failed, phase stops: {problems}")
    data.write_frame(frame)
    print(json.dumps(report, indent=2))


def stage_analyse() -> None:
    frame = slim_frame()
    records, deciles, spreads, sides, stability, mono = [], [], [], [], [], []
    for signal in spec.SIGNALS:
        for horizon in spec.HORIZONS_S:
            view = analysis.prepare(frame, signal, horizon)
            if view is None:
                continue
            records.append(analysis.signal_horizon_record(view))
            deciles.append(analysis.decile_table(view))
            mono.append(analysis.monotonicity_record(view))
            sides.append(analysis.side_table(view))
            for kind in ("cumulative", "incremental"):
                record = analysis.spread_record(view, kind)
                if record:
                    spreads.append(record)
            for by in ("utc_day", "segment_key"):
                stability.append(analysis.stability_table(view, by))
            if signal in spec.FULL_CORPUS_SECONDARY_FOR:
                wide = analysis.prepare(frame, signal, horizon, full_corpus=True)
                records.append(analysis.signal_horizon_record(wide))
                deciles.append(analysis.decile_table(wide))
                spreads.append(analysis.spread_record(wide, "cumulative"))
                if wide.pivot is not None:
                    spreads.append(analysis.spread_record(wide, "incremental"))
            print(f"  {signal} h={horizon}s rows={view.rows}", flush=True)

    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    decay = pd.DataFrame([r for r in records if r])
    spread = pd.DataFrame([r for r in spreads if r])
    decay.to_csv(spec.REPORT_DIR / "decay_profile.csv", index=False)
    pd.concat([d for d in deciles if not d.empty], ignore_index=True).to_csv(
        spec.REPORT_DIR / "signal_deciles.csv", index=False
    )
    spread.to_csv(spec.REPORT_DIR / "cumulative_incremental.csv", index=False)
    pd.concat([d for d in sides if not d.empty], ignore_index=True).to_csv(
        spec.REPORT_DIR / "up_down_legs.csv", index=False
    )
    pd.concat([d for d in stability if not d.empty], ignore_index=True).to_csv(
        spec.REPORT_DIR / "stability.csv", index=False
    )
    pd.DataFrame(mono).to_csv(spec.REPORT_DIR / "decile_monotonicity.csv", index=False)
    analysis.hurdle_table(spread).to_csv(spec.REPORT_DIR / "cost_hurdle.csv", index=False)
    effective_sample(decay).to_csv(spec.REPORT_DIR / "effective_sample.csv", index=False)
    reconciliation(spread).to_csv(spec.REPORT_DIR / "reconciliation.csv", index=False)
    print("analysis written")


def effective_sample(decay: pd.DataFrame) -> pd.DataFrame:
    part = decay[decay["population"] == "oof_scored_purged"]
    columns = [
        "signal",
        "horizon_s",
        "purge_s",
        "rows",
        "anchors",
        "bootstrap_block_s",
        "bootstrap_blocks",
        "demean_blocks",
    ]
    out = part[columns].copy()
    out["rows_per_anchor"] = out["rows"] / out["anchors"].replace(0, np.nan)
    return out.sort_values(["signal", "horizon_s"])


def reconciliation(spread: pd.DataFrame) -> pd.DataFrame:
    """cumulative(h) must equal pivot(h population) + incremental(5 s -> h), exactly.

    The pivot term is the 5 s spread measured on the horizon's own evaluated population, not on
    the 5 s population: a longer horizon carries a longer purge and loses rows at every segment
    edge, so the two populations differ and only the same-population identity is exact.
    """
    rows = []
    for signal in spread["signal"].unique():
        part = spread[(spread["signal"] == signal)
                      & (spread["population"] == "oof_scored_purged")]
        cumulative = part[part["kind"] == "cumulative"].set_index("horizon_s")
        incremental = part[part["kind"] == "incremental"].set_index("horizon_s")
        for horizon in incremental.index:
            for label in ("raw", "demeaned"):
                total = cumulative.loc[horizon, f"{label}_spread_ticks"]
                increment = incremental.loc[horizon, f"{label}_spread_ticks"]
                pivot = incremental.loc[horizon, f"{label}_pivot_spread_ticks"]
                rows.append(
                    {
                        "signal": signal,
                        "horizon_s": horizon,
                        "adjustment": label,
                        "cumulative_ticks": total,
                        "pivot_on_this_population_ticks": pivot,
                        "incremental_after_pivot_ticks": increment,
                        "residual_ticks": total - (pivot + increment),
                        "pivot_on_5s_population_ticks": cumulative.loc[
                            spec.PIVOT_S, f"{label}_spread_ticks"
                        ],
                        "rows": incremental.loc[horizon, "rows"],
                        "rows_at_5s": cumulative.loc[spec.PIVOT_S, "rows"],
                    }
                )
    return pd.DataFrame(rows)


def stage_verdict() -> None:
    spread = pd.read_csv(spec.REPORT_DIR / "cumulative_incremental.csv")
    hurdles = pd.read_csv(spec.REPORT_DIR / "cost_hurdle.csv")
    classifications = [analysis.classify(spread, s) for s in spec.SIGNALS]
    pd.DataFrame(classifications).to_csv(
        spec.REPORT_DIR / "classification.csv", index=False
    )

    cheapest = min(spec.COST_HURDLE_BPS)
    clears_round_trip = hurdles[hurdles["clears_round_trip"]]
    clears_one_way = hurdles[hurdles["clears_one_way"]]
    any_positive = any(
        c["resolved_positive_incremental_horizons_s"] for c in classifications
    )
    any_negative = any(
        c["resolved_negative_incremental_horizons_s"] for c in classifications
    )
    stable_medium = any(c["stable_positive_medium_horizons_s"] for c in classifications)

    medium = spread[(spread["kind"] == "incremental") & (spread["horizon_s"] >= 120)]
    unresolvable = bool(
        len(medium)
        and (
            (medium["demeaned_spread_p95"] - medium["demeaned_spread_p05"])
            > medium["demeaned_spread_ticks"].abs()
        ).all()
        and not medium["demeaned_resolved"].any()
    )

    if any_negative and not any_positive:
        verdict = "D"
    elif not clears_round_trip.empty and stable_medium:
        verdict = "C"
    elif any_positive:
        verdict = "B"
    elif unresolvable:
        verdict = "E"
    else:
        verdict = "A"

    payload = {
        "verdict": verdict,
        "verdict_rule": spec.VERDICT_RULES[verdict],
        "cheapest_one_way_hurdle_bps": cheapest,
        "cells_clearing_one_way": int(len(clears_one_way)),
        "cells_clearing_round_trip": int(len(clears_round_trip)),
        "cells_total": int(len(hurdles)),
        "max_break_even_all_in_cost_bps": float(
            hurdles["break_even_all_in_cost_bps"].max()
        ),
        "any_resolved_positive_incremental": any_positive,
        "any_resolved_negative_incremental": any_negative,
        "stable_positive_medium_horizon": stable_medium,
        "medium_horizon_unresolvable": unresolvable,
        "classifications": {c["signal"]: c["classification"] for c in classifications},
    }
    data.write_json("verdict.json", payload)
    data.write_json(
        "methodology.json",
        spec.methodology(
            data.input_hashes(),
            {"verdict": verdict, "classifications": payload["classifications"]},
        ),
    )
    print(json.dumps(payload, indent=2))


STAGES = {"frame": stage_frame, "analyse": stage_analyse, "verdict": stage_verdict}


def main(argv: list[str]) -> None:
    stage = argv[1] if len(argv) > 1 else "all"
    todo = list(STAGES) if stage == "all" else [stage]
    for name in todo:
        print(f"== {name}", flush=True)
        STAGES[name]()


if __name__ == "__main__":
    main(sys.argv)
