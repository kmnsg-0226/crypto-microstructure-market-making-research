"""Pre-registered specification for the directional sweep / execution-feasibility phase.

The passive best-touch market-making line was falsified in phase 4B. What survived it is a
classifier: the phase 4A sweep model predicts imminent level consumption very well. This phase
asks the only remaining question that classifier can answer — whether predicted consumption
carries economically meaningful information about the direction and size of the next price move —
and it fixes every horizon, threshold, bucket and cost hurdle here, before any result is seen.

Nothing in this file is an estimate and nothing in it is optimised. The cost hurdles in
particular are labelled sensitivities: no live account fee tier is used anywhere.
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_directional_sweep_v1"
DATA_DIR = ROOT / "data/research/native_directional_sweep_v1"

SCHEMA = "crypto-hft-native-directional-sweep-v1"
SEED = 0

# --------------------------------------------------------------------------------------------
# The signal, reused exactly as phase 4A froze it and phase 4B scored it
# --------------------------------------------------------------------------------------------
SWEEP_TARGET = "trade_through_within_500ms"
SWEEP_HORIZON_MS = 500
SWEEP_MODEL = "lightgbm"
SWEEP_FEATURE_SET = "all"
DECISION_GRID_MS = 100

# The direction a threatened level implies. A best ask under threat of upward consumption points
# up; a best bid under threat of downward consumption points down. Side 0 is the bid, side 1 the
# ask, matching every earlier phase.
SWEEP_DIRECTION = {0: -1, 1: +1}

# --------------------------------------------------------------------------------------------
# Horizons. The headline is fixed here and is not replaced afterwards.
# --------------------------------------------------------------------------------------------
HEADLINE_HORIZONS_MS = (100, 250, 500, 1000)
PRIMARY_HORIZON_MS = 500
SECONDARY_HORIZON_MS = 1000
# 2 s and 5 s are carried for the descriptive decomposition only.
ALL_HORIZONS_MS = (100, 250, 500, 1000, 2000, 5000)
# Every horizon except 2 s comes straight from the frozen phase 1 markout columns; 2 s is
# reconstructed from the phase 3 mid path, and the reconstruction is reconciled against the
# frozen columns on the horizons they share before it is used.
FROZEN_HORIZONS_MS = (100, 250, 500, 1000, 5000)
RECONSTRUCTED_HORIZONS_MS = (2000,)

SWEEP_THRESHOLDS = (0.30, 0.50, 0.70, 0.90)

# Descriptive tables use every 100 ms row. Models are fitted on a deterministic one-second
# decimation of the same rows, exactly as phase 4A did and for the same reason: neighbouring
# 100 ms rows inside one level episode are near-duplicates, so the decimation costs almost no
# information and keeps the comparison affordable enough to run every feature set on every fold.
MODEL_GRID_MS = 1000
DECILES = 10

# --------------------------------------------------------------------------------------------
# Magnitude and lead-time reporting, fixed
# --------------------------------------------------------------------------------------------
TICK_THRESHOLDS = (1, 5, 10, 25, 50)
LEAD_TIME_BINS_MS = (0.0, 25.0, 50.0, 100.0, 250.0, 500.0)
EVENT_TAUS_MS = (-500, -250, -100, 0, 25, 50, 100, 250, 500, 1000, 2000, 5000)

# --------------------------------------------------------------------------------------------
# Price scale and the fixed execution-cost hurdle grid
# --------------------------------------------------------------------------------------------
TICK_SIZE_USD = 0.10
# One-way, in basis points of notional. The grid brackets the sensitivity values this repository
# already carries in its own frozen specs (taker 5.0 bp per side, maker 2.0 bp per leg) and is
# NOT taken from any live account. A round trip is twice a one-way hurdle and is reported as
# such rather than as an extra grid point.
COST_HURDLE_BPS = (1.0, 2.0, 3.0, 5.0, 7.5, 10.0)

# --------------------------------------------------------------------------------------------
# Feature sets for the model comparison. All side-normalised, all reused from earlier phases
# except the OBI-only subset, which is named here before any model is fitted.
# --------------------------------------------------------------------------------------------
OBI_FEATURES = [
    "signed_obi_l1",
    "signed_obi_l5",
    "signed_obi_l10",
    "signed_weighted_obi_l5",
    "signed_weighted_obi_l10",
    "signed_microprice_offset_ticks",
    "spread_ticks",
]
SWEEP_ONLY = ["sweep_p"]

# The controls the incremental audit must beat, named in the brief.
CONTROL_FEATURES = OBI_FEATURES + [
    "signed_trade_imbalance_500ms",
    "signed_trade_imbalance_1000ms",
    "signed_net_depth_flow_l1_500ms",
    "signed_net_depth_flow_l1_1000ms",
    "signed_depth_flow_pressure_l10_500ms",
    "signed_depth_flow_pressure_l10_1000ms",
    "trade_count_1000ms",
    "depth_event_count_1000ms",
]

MODEL_SETS = ("obi", "sweep_only", "book_flow", "book_flow_plus_sweep")
PRIMARY_MODEL_SET = "book_flow_plus_sweep"

# --------------------------------------------------------------------------------------------
# Regime buckets, fixed causal quantiles
# --------------------------------------------------------------------------------------------
REGIME_SIGNALS = {
    "realised_movement": "backward_mid_abs_change_ticks_1000ms",
    "trade_intensity": "trade_count_1000ms",
    "depth_event_intensity": "depth_event_count_1000ms",
    "spread_state": "spread_ticks",
}
REGIME_BUCKETS = 5

# --------------------------------------------------------------------------------------------
# Validation, identical geometry to phases 2, 3, 4A and 4B
# --------------------------------------------------------------------------------------------
N_BLOCKS = 12
FIRST_VALIDATION_BLOCK = 2
PURGE_SECONDS = 60.0
BOOTSTRAP_BLOCK_MINUTES = 30
BOOTSTRAP_DRAWS = 500
CALIBRATION_BINS = 10


def ticks_to_bps(ticks: float, mid_ticks: float) -> float:
    """Ticks of mid movement as basis points of notional at that mid."""
    return 1e4 * ticks / mid_ticks


def methodology(inputs: dict) -> dict:
    from pyresearch.native.predictive import spec as predictive_spec

    return {
        "schema": SCHEMA,
        "phase": "native_directional_sweep_v1",
        "question": "does predicted level consumption carry economically meaningful information "
        "about the direction and magnitude of imminent price movement?",
        "status": "descriptive and blocked out-of-fold feasibility study on development data",
        "corpus": {
            "spec": "research/specs/native_dev_v1.json",
            "development_only": True,
            "is_out_of_sample": False,
            "forward_data_untouched": [
                "the rotation-enabled AWS capture file",
                "every later AWS capture",
                "every post-native_dev_v1 native capture",
                "Tardis June/July/August holdout",
            ],
        },
        "prohibited_in_this_phase": [
            "any entry or exit rule",
            "choosing a threshold, decile, horizon or regime to trade",
            "position sizing, leverage, stops, targets, holding-period optimisation",
            "maker/taker switching or inventory",
            "strategy PnL or Sharpe",
            "live account fees",
            "forward validation",
            "rescuing the falsified passive market-making line",
        ],
        "signal": {
            "target": SWEEP_TARGET,
            "horizon_ms": SWEEP_HORIZON_MS,
            "model": SWEEP_MODEL,
            "feature_set": SWEEP_FEATURE_SET,
            "source_phase": "native_queue_tail_v1",
            "scored_in": "native_cancel_falsification_v1",
            "retrained_for_this_phase": False,
            "hyper_parameters_changed": False,
            "in_sample_predictions_used": False,
            "provenance": "every score carries the fold whose training window ended before it; "
            "a fold's model never saw its own validation block or any later block",
        },
        "direction": {
            "rule": "a threatened best ask implies +1, a threatened best bid implies -1",
            "side_codes": {"0": "bid", "1": "ask"},
            "directional_markout": "sweep_direction * (mid(t+h) - mid(t)), in ticks",
            "sides_reported_separately": True,
            "symmetry_assumed": False,
        },
        "targets": {
            "headline_horizons_ms": list(HEADLINE_HORIZONS_MS),
            "primary_horizon_ms": PRIMARY_HORIZON_MS,
            "secondary_horizon_ms": SECONDARY_HORIZON_MS,
            "all_horizons_ms": list(ALL_HORIZONS_MS),
            "frozen_from_phase_1": list(FROZEN_HORIZONS_MS),
            "reconstructed_from_mid_path": list(RECONSTRUCTED_HORIZONS_MS),
            "censoring": "a horizon that would cross a segment edge is censored, never zero",
            "no_target_crosses_a_segment_boundary": True,
            "conditional_and_unconditional_reported_separately": True,
        },
        "grids": {
            "sweep_thresholds": list(SWEEP_THRESHOLDS),
            "deciles": DECILES,
            "tick_thresholds": list(TICK_THRESHOLDS),
            "cost_hurdle_bps_one_way": list(COST_HURDLE_BPS),
            "event_taus_ms": list(EVENT_TAUS_MS),
            "fixed_before_any_result": True,
            "threshold_or_decile_selected": False,
            "headline_horizon_replaced_after_seeing_results": False,
        },
        "cost_hurdles": {
            "units": "basis points of notional, one way",
            "is_a_live_account_fee_tier": False,
            "label": "sensitivity values only",
            "repository_reference_values": {
                "taker_fee_bps_per_side": 5.0,
                "maker_fee_bps_per_leg": 2.0,
                "source": "research/specs/obi_passive_entry_search_spec.json and siblings",
            },
            "round_trip": "twice the one-way hurdle; reported as arithmetic, not as extra grid "
            "points",
            "used_as_a_model_feature": False,
        },
        "models": {
            "sets": {
                "obi": OBI_FEATURES,
                "sweep_only": SWEEP_ONLY,
                "book_flow": "phase 4A static_book_plus_flow, side normalised",
                "book_flow_plus_sweep": "the same plus the out-of-fold sweep probability",
            },
            "estimators": ["naive", "logistic or ridge", "lightgbm"],
            "lightgbm_classification": predictive_spec.LGBM_CLASSIFICATION,
            "lightgbm_regression": predictive_spec.LGBM_REGRESSION,
            "lightgbm_rounds": predictive_spec.LGBM_ROUNDS,
            "reused_from_phase_2_unchanged": True,
            "broad_tuning_performed": False,
            "fitted_on_grid_ms": MODEL_GRID_MS,
            "descriptive_tables_use_grid_ms": DECISION_GRID_MS,
        },
        "validation": {
            "scheme": "expanding-window chronologically blocked out-of-fold",
            "blocks": N_BLOCKS,
            "first_validation_block": FIRST_VALIDATION_BLOCK,
            "directional_models_start_one_block_later": "the sweep probability is only "
            "out-of-fold inside a validation block, so the first block that carries a score has "
            "no scored past to train on and is used for training only",
            "purge_seconds": PURGE_SECONDS,
            "random_splits_used": False,
            "label": "blocked OOF development estimates",
            "iid_standard_errors_used": False,
            "dependence_aware_interval": f"moving block bootstrap, "
            f"{BOOTSTRAP_BLOCK_MINUTES} minute blocks, {BOOTSTRAP_DRAWS} draws",
        },
        "denominator": {
            "rule": "every headline economic diagnostic uses the full decision-time opportunity "
            "denominator; conditioning on a sweep having actually happened is reported as a "
            "separate, clearly labelled quantity",
            "false_positives_retained": True,
        },
        "price_scale": {
            "tick_size_usd": TICK_SIZE_USD,
            "note": "basis points are computed against the mid at the decision instant, not a "
            "constant assumed price",
        },
        "seed": SEED,
        "inputs": inputs,
    }
