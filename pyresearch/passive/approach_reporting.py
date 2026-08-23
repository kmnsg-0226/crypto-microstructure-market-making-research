"""Audit and summarize the post-V1 passive approach exploration artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.passive.approach_search import (
    COMBINATION_CATALOG_PATH,
    COMBINATION_COMPARISON_PATH,
    COMBINATION_DEVELOPMENT_DAY_PATH,
    COMBINATION_DEVELOPMENT_PATH,
    COMBINATION_RANKING_PATH,
    COMBINATION_REPLICATION_DAY_PATH,
    COMBINATION_REPLICATION_PATH,
    COMBINATION_SHORTLIST_PATH,
    COMBINATION_SPEC_PATH,
    COMPARISON_PATH,
    DEVELOPMENT_DAY_PATH,
    DEVELOPMENT_SPLIT_PATH,
    POLICY_CATALOG_PATH,
    RANKING_PATH,
    REPLICATION_DAY_PATH,
    REPLICATION_SPLIT_PATH,
    REPORT_ROOT,
    SENSITIVITY_DAY_PATH,
    SENSITIVITY_PATH,
    SHORTLIST_PATH,
    SPEC_PATH,
    THRESHOLDS_PATH,
)
from pyresearch.support.evaluate import sha256, write_json


SUMMARY_PATH = REPORT_ROOT / "summary.json"
PRIMARY_KEY = [
    "stage",
    "policy",
    "side",
    "queue_model",
    "quote_lifetime_ms",
    "markout_horizon_ms",
]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_rows(path: Path, expected: int) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if len(frame) != expected:
        raise ValueError(f"{path.name}: expected {expected} rows, received {len(frame)}")
    return frame


def _metric(frame: pd.DataFrame, stage: str, policy: str, side: str = "both") -> dict[str, Any]:
    selected = frame.loc[
        frame["stage"].eq(stage)
        & frame["policy"].eq(policy)
        & frame["side"].eq(side)
    ]
    if len(selected) != 1:
        raise ValueError(f"non-unique result: {stage} {policy} {side}")
    row = selected.iloc[0]
    return {
        "candidate_retention": float(row["candidate_retention"]),
        "labeled_fills": int(row["labeled_fills"]),
        "labeled_fill_probability": float(row["labeled_fill_probability"]),
        "maker_markout_mean_ticks": float(row["maker_markout_mean_ticks"]),
        "maker_markout_negative_probability": float(
            row["maker_markout_negative_probability"]
        ),
        "gross_passive_edge_mean_bps": float(row["gross_passive_edge_mean_bps"]),
    }


def _shortlist_day_audit(
    development_day: pd.DataFrame,
    replication_day: pd.DataFrame,
    names: list[str],
) -> dict[str, Any]:
    day = pd.concat([development_day, replication_day], ignore_index=True)
    both = day.loc[day["side"].eq("both")]
    baseline = both.loc[both["policy"].eq("always_quote"), [
        "date", "maker_markout_mean_ticks"
    ]].rename(columns={"maker_markout_mean_ticks": "baseline_ticks"})
    selected = both.loc[both["policy"].isin(names)].merge(baseline, on="date")
    selected["improvement_ticks"] = (
        selected["maker_markout_mean_ticks"] - selected["baseline_ticks"]
    )
    if len(selected) != len(names) * 8:
        raise ValueError("shortlist does not have exactly eight daily observations per policy")
    return {
        "policy_days": int(len(selected)),
        "positive_improvement_policy_days": int((selected["improvement_ticks"] > 0).sum()),
        "minimum_daily_improvement_ticks": float(selected["improvement_ticks"].min()),
        "maximum_daily_improvement_ticks": float(selected["improvement_ticks"].max()),
        "mean_daily_improvement_ticks": float(selected["improvement_ticks"].mean()),
    }


def build_summary() -> dict[str, Any]:
    policy_catalog = _assert_rows(POLICY_CATALOG_PATH, 77)
    development_day = _assert_rows(DEVELOPMENT_DAY_PATH, 5 * 77 * 3)
    development = _assert_rows(DEVELOPMENT_SPLIT_PATH, 77 * 3)
    ranking = _assert_rows(RANKING_PATH, 76)
    replication_day = _assert_rows(REPLICATION_DAY_PATH, 3 * 77 * 3)
    replication = _assert_rows(REPLICATION_SPLIT_PATH, 2 * 77 * 3)
    comparison = _assert_rows(COMPARISON_PATH, 3 * 77 * 3)
    sensitivity_day = _assert_rows(SENSITIVITY_DAY_PATH, 8 * 4 * 2 * 4 * 11 * 3)
    sensitivity = _assert_rows(SENSITIVITY_PATH, 3 * 4 * 2 * 4 * 11 * 3)

    combination_catalog = _assert_rows(COMBINATION_CATALOG_PATH, 171)
    combination_development_day = _assert_rows(
        COMBINATION_DEVELOPMENT_DAY_PATH, 5 * 172 * 3
    )
    combination_development = _assert_rows(COMBINATION_DEVELOPMENT_PATH, 172 * 3)
    combination_ranking = _assert_rows(COMBINATION_RANKING_PATH, 171)
    combination_replication_day = _assert_rows(
        COMBINATION_REPLICATION_DAY_PATH, 3 * 172 * 3
    )
    combination_replication = _assert_rows(COMBINATION_REPLICATION_PATH, 2 * 172 * 3)
    combination_comparison = _assert_rows(COMBINATION_COMPARISON_PATH, 3 * 172 * 3)

    for name, frame in (
        ("phase1", comparison),
        ("phase2", combination_comparison),
        ("sensitivity", sensitivity),
    ):
        if frame.duplicated(PRIMARY_KEY).any():
            raise ValueError(f"duplicate {name} aggregate key")

    phase1_shortlist_payload = _load_json(SHORTLIST_PATH)
    phase1_shortlist = [item["name"] for item in phase1_shortlist_payload["shortlist"]]
    phase2_shortlist_payload = _load_json(COMBINATION_SHORTLIST_PATH)
    phase2_shortlist = [item["name"] for item in phase2_shortlist_payload["shortlist"]]
    if len(phase1_shortlist) != 10 or len(phase2_shortlist) != 10:
        raise ValueError("unexpected shortlist size")

    phase1_baseline = {
        stage: _metric(comparison, stage, "always_quote")
        for stage in ("development", "june_retrospective", "jul_aug_retrospective")
    }
    phase1_rank1 = ranking.iloc[0]["policy"]
    phase1_rank1_metrics = {
        stage: _metric(comparison, stage, phase1_rank1)
        for stage in phase1_baseline
    }
    for stage in phase1_baseline:
        phase1_rank1_metrics[stage]["improvement_ticks"] = (
            phase1_rank1_metrics[stage]["maker_markout_mean_ticks"]
            - phase1_baseline[stage]["maker_markout_mean_ticks"]
        )

    phase2_rank1 = combination_ranking.iloc[0]["policy"]
    phase2_rank1_metrics = {
        stage: _metric(combination_comparison, stage, phase2_rank1)
        for stage in phase1_baseline
    }
    for stage in phase1_baseline:
        phase2_rank1_metrics[stage]["improvement_ticks"] = (
            phase2_rank1_metrics[stage]["maker_markout_mean_ticks"]
            - phase1_baseline[stage]["maker_markout_mean_ticks"]
        )

    sensitivity_both = sensitivity.loc[sensitivity["side"].eq("both")]
    phase2_both = combination_comparison.loc[combination_comparison["side"].eq("both")]
    positive_phase2 = combination_comparison.loc[
        combination_comparison["maker_markout_mean_ticks"] > 0
    ]
    positive_phase2_detail = []
    for row in positive_phase2.itertuples(index=False):
        positive_phase2_detail.append({
            "stage": row.stage,
            "policy": row.policy,
            "side": row.side,
            "candidate_retention": float(row.candidate_retention),
            "labeled_fills": int(row.labeled_fills),
            "maker_markout_mean_ticks": float(row.maker_markout_mean_ticks),
            "gross_passive_edge_mean_bps": float(row.gross_passive_edge_mean_bps),
        })

    payload = {
        "schema": "passive-approach-exploration-summary-v1",
        "interpretation": {
            "post_v1_exploration": True,
            "all_later_dates_already_seen": True,
            "new_unseen_validation_claimed": False,
            "profitability_claimed": False,
        },
        "scale": {
            "phase1_policies_including_baseline": int(len(policy_catalog)),
            "phase2_combination_policies": int(len(combination_catalog)),
            "distinct_primary_approaches_including_one_baseline": int(
                len(policy_catalog) + len(combination_catalog)
            ),
            "phase1_split_side_cells": int(len(comparison)),
            "phase2_split_side_cells": int(len(combination_comparison)),
            "sensitivity_split_side_cells": int(len(sensitivity)),
            "sensitivity_combined_side_cells": int(len(sensitivity_both)),
        },
        "phase1": {
            "rank1_policy": phase1_rank1,
            "baseline": phase1_baseline,
            "rank1_metrics": phase1_rank1_metrics,
            "shortlist_daily_audit": _shortlist_day_audit(
                development_day, replication_day, phase1_shortlist
            ),
            "positive_absolute_combined_side_cells": int(
                (comparison.loc[comparison["side"].eq("both"), "maker_markout_mean_ticks"] > 0).sum()
            ),
        },
        "phase2": {
            "rank1_policy": phase2_rank1,
            "baseline": phase1_baseline,
            "rank1_metrics": phase2_rank1_metrics,
            "shortlist_daily_audit": _shortlist_day_audit(
                combination_development_day,
                combination_replication_day,
                phase2_shortlist,
            ),
            "positive_absolute_combined_side_cells": int(
                (phase2_both["maker_markout_mean_ticks"] > 0).sum()
            ),
            "positive_absolute_all_side_cells": int(len(positive_phase2)),
            "positive_absolute_all_side_details": positive_phase2_detail,
        },
        "sensitivity": {
            "positive_absolute_combined_side_cells": int(
                (sensitivity_both["maker_markout_mean_ticks"] > 0).sum()
            ),
            "positive_absolute_all_side_cells": int(
                (sensitivity["maker_markout_mean_ticks"] > 0).sum()
            ),
            "best_combined_side_mean_ticks": float(
                sensitivity_both["maker_markout_mean_ticks"].max()
            ),
            "worst_combined_side_mean_ticks": float(
                sensitivity_both["maker_markout_mean_ticks"].min()
            ),
        },
        "audit_sha256": {
            path.name: sha256(path)
            for path in (
                SPEC_PATH,
                COMBINATION_SPEC_PATH,
                THRESHOLDS_PATH,
                SHORTLIST_PATH,
                COMBINATION_SHORTLIST_PATH,
                COMPARISON_PATH,
                SENSITIVITY_PATH,
                COMBINATION_COMPARISON_PATH,
            )
        },
    }
    write_json(SUMMARY_PATH, payload)
    return payload


def main() -> None:
    print(json.dumps(build_summary(), sort_keys=True))


if __name__ == "__main__":
    main()
