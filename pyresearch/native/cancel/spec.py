"""Pre-registered specification for the cancel / stay falsification study.

Everything a result could be tuned on is fixed in this file: the signal, the probability grid,
the latency grid, the queue assumptions, the headline cells and the falsification criteria. The
grids are sensitivity grids. No threshold in them is an estimate of anything, none of them is
selected afterwards, and the phase deliberately keeps the criteria that would kill the passive
market-making line written down before any number is produced.
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_cancel_falsification_v1"
DATA_DIR = ROOT / "data/research/native_cancel_falsification_v1"

SCHEMA = "crypto-hft-native-cancel-falsification-v1"
SEED = 0

# --------------------------------------------------------------------------------------------
# The signal. This is the phase 4A headline sweep model, reused unchanged.
# --------------------------------------------------------------------------------------------
# Phase 4A fitted two sweep horizons. The 500 ms model is the one whose numbers were reported as
# the headline observable result (ROC AUC 0.879, PR lift 5.5x, expected sign in 120/120 blocks),
# so it is the one carried forward. The choice was made from the phase 4A report, before any
# cancellation outcome was computed, and is not revisited afterwards.
SWEEP_TARGET = "trade_through_within_500ms"
SWEEP_HORIZON_MS = 500
SWEEP_MODEL = "lightgbm"
SWEEP_FEATURE_SET = "all"

# The lifecycle grid is emitted at 100 ms. Phase 4A *fitted* on a one-second decimation of it and
# this phase changes nothing about that: the same rows train the same model with the same
# parameters and the same folds. Only the scoring cadence changes, because a resting order has to
# be able to react between two one-second instants when the median best level lives 204 ms.
TRAIN_GRID_MS = 1000
DECISION_GRID_MS = 100

# --------------------------------------------------------------------------------------------
# Queue assumptions. Three fixed cells, no search, none of them called best.
# --------------------------------------------------------------------------------------------
QUEUE_CELLS = {
    "conservative": (100, 0),
    "midpoint": (50, 50),
    "optimistic": (0, 100),
}

# --------------------------------------------------------------------------------------------
# The two sensitivity grids
# --------------------------------------------------------------------------------------------
CANCEL_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)
NEVER_CANCEL = "never"
# Fixed effective-cancellation delays. These are NOT measurements of Binance cancel latency and
# are not calibrated to anything; they are sensitivities that ask how much of the protection
# survives when the cancel cannot be instantaneous.
CANCEL_LATENCIES_MS = (0, 25, 50, 100)

HEADLINE_THRESHOLDS = (0.30, 0.50, 0.70)
HEADLINE_LATENCIES_MS = (0, 50, 100)

# --------------------------------------------------------------------------------------------
# Outcome definitions, all inherited from earlier phases unchanged
# --------------------------------------------------------------------------------------------
PRIMARY_MARKOUT_MS = 1000
MARKOUT_HORIZONS_MS = (100, 500, 1000, 5000)
CATASTROPHIC_THRESHOLDS_TICKS = (25, 50, 100)
SCORE_DECILES = 10

# Lead-time reporting bins, in milliseconds, fixed in advance.
LEAD_TIME_BINS_MS = (0.0, 25.0, 50.0, 100.0, 250.0, 500.0)

BOOTSTRAP_BLOCK_MINUTES = 30
BOOTSTRAP_DRAWS = 500

# --------------------------------------------------------------------------------------------
# Causal rules
# --------------------------------------------------------------------------------------------
# A decision instant must be strictly after the placement instant. An order cancelled at its own
# placement instant would be a quote / no-quote decision, and entry filtering is out of scope for
# this phase; requiring t > T0 keeps the study to the single intervention it claims to test.
DECISION_STRICTLY_AFTER_PLACEMENT = True
# A fill is prevented only when it happens strictly after the cancellation becomes effective. A
# fill stamped at exactly the effective instant survives, because at zero latency the print that
# causes it is part of the information the signal itself was computed from, and information
# observed at t cannot retract an execution that already happened at t.
FILL_PREVENTED_STRICTLY_AFTER_EFFECTIVE = True

FALSIFICATION_CRITERIA = {
    "supporting": [
        "increasing out-of-fold sweep risk corresponds to increasingly adverse fill outcomes",
        "cancellation removes a materially larger share of negative than positive markout over "
        "a broad range of the fixed thresholds",
        "the behaviour survives non-zero effective cancellation latency",
        "it is stable across the chronological blocks",
        "it does not depend on the optimistic queue assumption alone",
        "lead time is meaningfully positive before a large share of catastrophic fills",
        "the improvement is not obtained by cancelling almost every order",
    ],
    "falsifying": [
        "protection disappears at 50-100 ms effective latency",
        "favourable markout sacrificed is comparable to adverse markout avoided",
        "only alpha=0 works",
        "only one narrow threshold works",
        "most signal crossings happen at or after the fill",
        "block stability is poor",
        "the tail only improves when nearly all fills are eliminated",
    ],
    "relaxed_after_seeing_results": False,
}


def methodology(inputs: dict) -> dict:
    from pyresearch.native.predictive import spec as predictive_spec

    return {
        "schema": SCHEMA,
        "phase": "native_cancel_falsification_v1",
        "question": "for an order that is already resting, does cancelling when observable "
        "out-of-fold sweep probability is high remove more adverse markout than favourable "
        "markout?",
        "status": "counterfactual falsification study on development data",
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
        "prohibited_in_this_phase": [
            "entry or quote / no-quote filtering",
            "requoting, re-entry, moving the order or crossing the spread",
            "fees, rebates, spread capture, inventory or opportunity cost",
            "strategy PnL, Sharpe or a backtest",
            "any optimisation of alpha, beta, thresholds, latency or model parameters",
            "forward AWS validation",
        ],
        "signal": {
            "target": SWEEP_TARGET,
            "horizon_ms": SWEEP_HORIZON_MS,
            "model": SWEEP_MODEL,
            "feature_set": SWEEP_FEATURE_SET,
            "source_phase": "native_queue_tail_v1",
            "refit_identically_for_scoring": True,
            "trained_on_grid_ms": TRAIN_GRID_MS,
            "scored_on_grid_ms": DECISION_GRID_MS,
            "training_rows_unchanged_from_phase_4a": True,
            "provenance": "every score carries the fold whose training window ended before it; "
            "a fold's model never sees its own validation block or any later block",
            "phase_2_toxicity_model_used": False,
        },
        "intervention": {
            "actions": ["stay", "cancel"],
            "requote": False,
            "re_entry": False,
            "partial_cancel": False,
            "placement_unchanged_from_phase_3": True,
            "decision_instants": "every 100 ms lifecycle row strictly after the placement "
            "instant, on the same side and segment, while the order's quote price is still the "
            "best price on its side",
            "decision_strictly_after_placement": DECISION_STRICTLY_AFTER_PLACEMENT,
            "effective_cancel_time": "decision timestamp + fixed latency",
            "fill_prevented_strictly_after_effective": (
                FILL_PREVENTED_STRICTLY_AFTER_EFFECTIVE
            ),
            "same_event_rule": "a signal computed from the information available at t cannot "
            "prevent an execution stamped at t",
        },
        "grids": {
            "queue_cells": {name: list(cell) for name, cell in QUEUE_CELLS.items()},
            "cancel_thresholds": list(CANCEL_THRESHOLDS),
            "cancel_latencies_ms": list(CANCEL_LATENCIES_MS),
            "baseline": NEVER_CANCEL,
            "headline_thresholds": list(HEADLINE_THRESHOLDS),
            "headline_latencies_ms": list(HEADLINE_LATENCIES_MS),
            "fixed_before_any_result": True,
            "is_an_optimisation_grid": False,
            "best_cell_selected": False,
        },
        "outcomes": {
            "primary_markout_ms": PRIMARY_MARKOUT_MS,
            "markout_horizons_ms": list(MARKOUT_HORIZONS_MS),
            "catastrophic_thresholds_ticks": list(CATASTROPHIC_THRESHOLDS_TICKS),
            "markout_sign": "bid: mid above the quote is favourable; ask: mid below the quote "
            "is favourable",
            "denominator": "every table retains the complete baseline cohort; a cancelled order "
            "is never counted as a zero-markout fill and never silently dropped",
            "excluded_by_design": [
                "fees and rebates",
                "spread capture",
                "inventory value",
                "opportunity cost of the forgone quote",
                "any value from a replacement order",
            ],
        },
        "validation": {
            "scheme": "expanding-window chronologically blocked out-of-fold",
            "folds_shared_with": ["native_predictive_v1", "native_queue_tail_v1"],
            "random_splits_used": False,
            "label": "blocked OOF development estimates",
            "iid_standard_errors_used": False,
            "dependence_aware_interval": f"moving block bootstrap, "
            f"{BOOTSTRAP_BLOCK_MINUTES} minute blocks, {BOOTSTRAP_DRAWS} draws",
        },
        "models": {
            "lightgbm_classification": predictive_spec.LGBM_CLASSIFICATION,
            "lightgbm_rounds": predictive_spec.LGBM_ROUNDS,
            "hyper_parameters_changed": False,
        },
        "falsification": FALSIFICATION_CRITERIA,
        "seed": SEED,
        "inputs": inputs,
    }


def grid_spec() -> dict:
    return {
        "schema": "crypto-hft-cancel-grid-v1",
        "queue_cells": {name: {"alpha_pct": a, "beta_pct": b} for name, (a, b) in QUEUE_CELLS.items()},
        "cancel_thresholds": list(CANCEL_THRESHOLDS),
        "cancel_latencies_ms": list(CANCEL_LATENCIES_MS),
        "headline_cells": [
            {"queue_cell": cell, "latency_ms": latency, "threshold": threshold}
            for cell in QUEUE_CELLS
            for latency in HEADLINE_LATENCIES_MS
            for threshold in HEADLINE_THRESHOLDS
        ],
        "baseline": NEVER_CANCEL,
        "catastrophic_thresholds_ticks": list(CATASTROPHIC_THRESHOLDS_TICKS),
        "lead_time_bins_ms": list(LEAD_TIME_BINS_MS),
        "score_deciles": SCORE_DECILES,
        "fixed_before_any_result": True,
        "added_or_removed_after_seeing_results": False,
    }
