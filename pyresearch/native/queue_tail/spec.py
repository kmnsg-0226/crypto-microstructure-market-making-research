"""Pre-registered specification for the queue-dynamics and catastrophic-tail phase.

Every threshold, horizon, feature group, fold rule and model parameter is fixed here before any
result is seen. The catastrophic thresholds in particular were chosen from the phase 3 markout
distribution shape, not from any model score, and are not moved afterwards.
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_queue_tail_v1"
DATA_DIR = ROOT / "data/research/native_queue_tail_v1"

SCHEMA = "crypto-hft-native-queue-tail-v1"
SEED = 0

# --------------------------------------------------------------------------------------------
# Populations
# --------------------------------------------------------------------------------------------
# The lifecycle grid is emitted at 100 ms; models are fitted on a deterministic one-second
# decimation of it, matching the phase 3 placement instants exactly so every join is on a real
# shared key. Neighbouring 100 ms rows inside one level episode are near-duplicates, so the
# decimation costs almost no information.
LIFECYCLE_GRID_MS = 100
MODEL_GRID_MS = 1000

QUEUE_CELLS = {"conservative": (100, 0), "midpoint": (50, 50), "optimistic": (0, 100)}
PRIMARY_QUEUE_CELL = "conservative"

# --------------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------------
SURVIVAL_HORIZONS_MS = (100, 250, 500, 1000, 5000)
SWEEP_HORIZONS_MS = (100, 250, 500, 1000, 5000)
MARKOUT_HORIZONS_MS = (100, 500, 1000, 5000)
PRIMARY_MARKOUT_MS = 1000

# Fixed before any model was fitted and never changed.
CATASTROPHIC_THRESHOLDS_TICKS = (10, 25, 50, 100)
PRIMARY_CATASTROPHIC_TICKS = 25
SECONDARY_CATASTROPHIC_TICKS = 50
SEVERITY_THRESHOLD_TICKS = 25

# Problems taken forward to a full model; everything else stays descriptive.
LEVEL_FAILURE_HORIZONS_MS = (500, 1000)
SWEEP_MODEL_HORIZONS_MS = (500, 1000)

# --------------------------------------------------------------------------------------------
# Feature groups, for the pre-registered ablation
# --------------------------------------------------------------------------------------------
STATIC_BOOK = [
    "spread_ticks",
    "signed_obi_l1",
    "signed_obi_l5",
    "signed_obi_l10",
    "signed_weighted_obi_l5",
    "signed_weighted_obi_l10",
    "signed_microprice_offset_ticks",
    "log_own_depth_l1",
    "log_opp_depth_l1",
    "log_own_depth_l5",
    "log_opp_depth_l5",
    "log_own_depth_l10",
    "log_opp_depth_l10",
    "own_concentration_l5",
    "opp_concentration_l5",
    "own_concentration_l10",
    "opp_concentration_l10",
    "own_dispersion_ticks_l10",
    "opp_dispersion_ticks_l10",
]

FLOW_WINDOWS_MS = (100, 250, 500, 1000, 5000)


def recent_flow() -> list[str]:
    names: list[str] = []
    for window in FLOW_WINDOWS_MS:
        names += [
            f"signed_net_depth_flow_l1_{window}ms",
            f"signed_net_depth_flow_l5_{window}ms",
            f"signed_depth_flow_pressure_l10_{window}ms",
            f"signed_trade_imbalance_{window}ms",
            f"signed_log_volume_{window}ms",
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ]
    names += [
        "time_since_trade_ms",
        "time_since_depth_ms",
        "time_since_bbo_change_ms",
        "time_since_mid_change_ms",
    ]
    return names


RECENT_FLOW = recent_flow()

# Observable level-lifecycle state. These are properties of the displayed level, never of any
# individual order, and level age is not a queue rank.
QUEUE_LIFECYCLE = [
    "log_level_age_ms",
    "log_current_qty",
    "log_initial_qty",
    "log_max_qty",
    "relative_remaining_qty",
    "relative_to_max_qty",
    "drawdown_from_max",
    "log_cum_add",
    "log_cum_remove",
    "log_cum_trade_at_quote",
    "log_cum_trade_through",
    "log_cum_replenish",
    "consumption_ratio",
    "removal_ratio",
    "addition_ratio",
    "replenishment_ratio",
    "unexplained_removal_share",
    "log_add_events",
    "log_remove_events",
    "log_replenish_events",
    "log_prints_at_quote",
    "log_prints_through",
    "log_time_since_qty_change_ms",
    "log_time_since_add_ms",
    "log_time_since_remove_ms",
    "log_time_since_print_ms",
    "log_time_since_replenish_ms",
    "has_replenished",
]

FEATURE_SETS = {
    "static_book": STATIC_BOOK,
    "static_book_plus_flow": STATIC_BOOK + RECENT_FLOW,
    "queue_lifecycle": QUEUE_LIFECYCLE,
    "all": STATIC_BOOK + RECENT_FLOW + QUEUE_LIFECYCLE,
}
PRIMARY_FEATURE_SET = "all"
ABLATION_TARGETS = ("level_disappears_1000ms", "trade_through_within_1000ms", "catastrophic_25")

# --------------------------------------------------------------------------------------------
# Bucket and interaction studies, all fixed
# --------------------------------------------------------------------------------------------
BUCKET_SIGNALS = [
    "log_level_age_ms",
    "relative_remaining_qty",
    "log_cum_replenish",
    "log_replenish_events",
    "consumption_ratio",
    "unexplained_removal_share",
    "log_time_since_replenish_ms",
    "log_time_since_print_ms",
    "signed_obi_l1",
    "signed_net_depth_flow_l1_1000ms",
    "signed_trade_imbalance_1000ms",
    "signed_depth_flow_pressure_l10_1000ms",
    "depth_event_count_1000ms",
    "trade_count_1000ms",
]
INTERACTION_PAIRS = [
    ("log_level_age_ms", "relative_remaining_qty"),
    ("log_cum_replenish", "signed_trade_imbalance_1000ms"),
    ("signed_obi_l1", "signed_net_depth_flow_l1_1000ms"),
    ("log_own_depth_l1", "trade_count_1000ms"),
    ("relative_remaining_qty", "signed_trade_imbalance_1000ms"),
]
BUCKETS = 10
INTERACTION_BUCKETS = 5

# --------------------------------------------------------------------------------------------
# Validation, identical geometry to phase 2
# --------------------------------------------------------------------------------------------
N_BLOCKS = 12
FIRST_VALIDATION_BLOCK = 2
PURGE_SECONDS = 60.0
MAX_TARGET_HORIZON_SECONDS = 35.0

BOOTSTRAP_BLOCK_MINUTES = 30
BOOTSTRAP_DRAWS = 500
CALIBRATION_BINS = 10


def methodology(inputs: dict) -> dict:
    from pyresearch.native.predictive import spec as predictive_spec

    return {
        "schema": SCHEMA,
        "phase": "native_queue_tail_v1",
        "status": "descriptive_and_blocked_oof_predictive",
        "corpus": {
            "spec": "research/specs/native_dev_v1.json",
            "development_only": True,
            "is_out_of_sample": False,
            "forward_data_untouched": [
                "the rotation-enabled AWS capture file",
                "every later AWS capture",
                "Tardis June/July/August holdout",
            ],
        },
        "objectives": [
            "replace the free queue-position parameter with observable level-lifecycle state",
            "predict catastrophic passive-fill outcomes rather than average markout",
        ],
        "prohibited_in_this_phase": [
            "quote / no-quote thresholds",
            "cancel, stay or requote rules",
            "inventory management",
            "fee-adjusted expected value",
            "strategy PnL or Sharpe",
            "forward AWS validation",
        ],
        "terminology": {
            "level_age_is_not_queue_rank": True,
            "unexplained_removal_is_not_called_cancellation": True,
            "level_birth_cohort_is_not_front_of_queue": True,
            "claims_true_queue_position": False,
        },
        "level_episodes": {
            "starts": "when a price becomes the best price on its side",
            "ends": "when it stops being best, the segment ends, or the book resynchronizes",
            "same_price_later_is_a_new_episode": True,
            "never_crosses_a_segment_boundary": True,
        },
        "reconciliation": {
            "rule": "an aggressive print at the level accounts for a later displayed reduction "
            "at that level, first come first served; the remainder is unexplained",
            "future_trades_never_explain_an_earlier_removal": True,
            "replenishment_requires_a_prior_reduction_in_the_same_episode": True,
        },
        "populations": {
            "lifecycle_grid_ms": LIFECYCLE_GRID_MS,
            "model_grid_ms": MODEL_GRID_MS,
            "queue_cells": {name: list(cell) for name, cell in QUEUE_CELLS.items()},
            "primary_queue_cell": PRIMARY_QUEUE_CELL,
            "level_birth_cohort": "one hypothetical order placed immediately after the depth "
            "event that makes a price best; everything displayed at that instant is ahead of "
            "it, later additions are behind it, and a print at the same timestamp cannot fill "
            "it",
        },
        "targets": {
            "survival_horizons_ms": list(SURVIVAL_HORIZONS_MS),
            "sweep_horizons_ms": list(SWEEP_HORIZONS_MS),
            "markout_horizons_ms": list(MARKOUT_HORIZONS_MS),
            "catastrophic_thresholds_ticks": list(CATASTROPHIC_THRESHOLDS_TICKS),
            "primary_catastrophic_ticks": PRIMARY_CATASTROPHIC_TICKS,
            "thresholds_fixed_before_fitting": True,
            "censoring": "a horizon that would cross a segment edge is censored, never zero",
        },
        "features": {
            "sets": {name: len(columns) for name, columns in FEATURE_SETS.items()},
            "ablation_targets": list(ABLATION_TARGETS),
            "causality": "every feature is a function of events with a receive timestamp at or "
            "before the decision instant; no fill-time book state, no post-fill information and "
            "no later level evolution enters any model",
        },
        "validation": {
            "scheme": "expanding-window chronologically blocked out-of-fold",
            "blocks": N_BLOCKS,
            "first_validation_block": FIRST_VALIDATION_BLOCK,
            "purge_seconds": PURGE_SECONDS,
            "random_splits_used": False,
            "label": "blocked OOF development estimates",
        },
        "models": {
            "order": ["naive", "logistic or linear", "lightgbm"],
            "lightgbm_classification": predictive_spec.LGBM_CLASSIFICATION,
            "lightgbm_regression": predictive_spec.LGBM_REGRESSION,
            "lightgbm_rounds": predictive_spec.LGBM_ROUNDS,
            "logistic": predictive_spec.LOGISTIC_PARAMS,
            "ridge": predictive_spec.RIDGE_PARAMS,
            "reused_from_phase_2_unchanged": True,
            "phase_2_toxicity_model_reused_as_headline": False,
        },
        "reporting": {
            "dependence_aware_interval": f"moving block bootstrap, "
            f"{BOOTSTRAP_BLOCK_MINUTES} minute blocks, {BOOTSTRAP_DRAWS} draws",
            "iid_standard_errors_used": False,
            "pr_auc_reported": True,
        },
        "seed": SEED,
        "inputs": inputs,
    }
