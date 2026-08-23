"""Pre-registered assumptions for the passive maker economic feasibility phase.

This phase quantifies how far the current best-touch passive maker setup is from break-even and
decomposes the gap. It does not search for a profitable configuration: no threshold is chosen,
no horizon is selected, no fee schedule is applied, and no cell of any surface is picked.
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_economic_v1"
DATA_DIR = ROOT / "data/research/native_economic_v1"

SCHEMA = "crypto-hft-native-economic-v1"

# --------------------------------------------------------------------------------------------
# Queue-model sensitivity grid, fixed before any result was seen
# --------------------------------------------------------------------------------------------
ALPHA_PERCENTS = (0, 25, 50, 75, 100)
BETA_PERCENTS = (0, 25, 50, 75, 100)
# The three assumptions every headline table is repeated under.
CONSERVATIVE = (100, 0)
MIDPOINT = (50, 50)
OPTIMISTIC = (0, 100)
HEADLINE_CELLS = (CONSERVATIVE, MIDPOINT, OPTIMISTIC)

# --------------------------------------------------------------------------------------------
# Economic object
# --------------------------------------------------------------------------------------------
MARKOUT_HORIZONS_MS = (100, 500, 1000, 5000)
PRIMARY_HORIZON_MS = 1000
FILL_HORIZONS_MS = (500, 1000, 5000)
OBSERVATION_WINDOW_MS = 30000

# One placement per second: a deterministic decimation of the 100 ms decision grid used in
# phases 1 and 2, so every placement instant is also a phase 2 row and the frozen out-of-fold
# predictions join on an exact key.
PLACEMENT_INTERVAL_MS = 1000
ORDER_LOTS = 5
TICK_SIZE_USD = 0.10
QTY_STEP_BTC = 0.001

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
DECILES = 10
QUINTILES = 5

# --------------------------------------------------------------------------------------------
# Activity regimes, quantile buckets on causal observables only
# --------------------------------------------------------------------------------------------
ACTIVITY_SIGNALS = {
    "realized_activity": "backward_mid_abs_change_ticks_5000ms",
    "depth_event_intensity": "depth_event_count_1000ms",
    "trade_intensity": "trade_count_1000ms",
    "spread_state": "spread_ticks",
}
ACTIVITY_BUCKETS = 5

BOOTSTRAP_BLOCK_MINUTES = 30
BOOTSTRAP_DRAWS = 500
SEED = 0


def methodology(inputs: dict) -> dict:
    """The full pre-registration plus the exact inputs the run consumed."""
    return {
        "schema": SCHEMA,
        "phase": "native_economic_v1",
        "status": "descriptive_feasibility_decomposition",
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
        "objective": "quantify the distance from break-even of the best-touch passive maker "
        "setup and decompose it into queue position, treatment of unexplained displayed-depth "
        "removals, fill mechanism, post-fill horizon and observable state",
        "prohibited_in_this_phase": [
            "threshold selection of any kind",
            "holding period, quote distance or order size tuning",
            "fee or rebate optimisation",
            "choosing a best alpha or beta",
            "selecting a surface cell to trade",
            "strategy PnL, Sharpe or walk-forward backtest",
        ],
        "queue_model": {
            "alpha_percents": list(ALPHA_PERCENTS),
            "beta_percents": list(BETA_PERCENTS),
            "alpha_meaning": "fraction of the displayed quantity at the quote assumed to be "
            "ahead of the hypothetical order at placement",
            "beta_meaning": "fraction of an unexplained displayed-quantity removal ahead of the "
            "order credited as queue advancement",
            "alpha_is_not_an_estimate_of_queue_position": True,
            "beta_is_not_a_cancellation_rate": True,
            "removal_attribution_order": [
                "quantity added at the quote after placement, which sits behind the order",
                "aggressive prints at the quote not yet used to explain a reduction",
                "remainder is unexplained and eligible for the beta credit",
            ],
            "beta_credit_stops_once_the_queue_ahead_is_empty": True,
            "trade_through_fills_regardless_of_alpha_and_beta": True,
            "print_at_the_placement_instant_cannot_fill_the_order": True,
            "conservative_cell": list(CONSERVATIVE),
            "midpoint_cell": list(MIDPOINT),
            "optimistic_cell": list(OPTIMISTIC),
        },
        "economic_object": {
            "bid": "signed_markout_h = mid(T_fill + h) - quote_price",
            "ask": "signed_markout_h = quote_price - mid(T_fill + h)",
            "units": "ticks; positive favours the passive trader",
            "horizons_ms": list(MARKOUT_HORIZONS_MS),
            "required_execution_benefit": "-E[signed_markout_h], the benefit that would have to "
            "be earned elsewhere to neutralise the observed markout, before any fee, rebate, "
            "spread capture or inventory assumption",
            "no_fee_schedule_applied": True,
        },
        "population": {
            "placement_interval_ms": PLACEMENT_INTERVAL_MS,
            "order_lots": ORDER_LOTS,
            "observation_window_ms": OBSERVATION_WINDOW_MS,
            "censoring": "a markout is only observed when the fill and the horizon after it "
            "both fall inside the segment and inside the observation window; censored "
            "outcomes leave the denominator and are counted separately",
            "segments": "maximal intervals with a synchronized depth book and a connected "
            "aggressive-trade stream, unchanged from phase 1",
        },
        "out_of_fold_reuse": {
            "source": "data/research/native_predictive_v1/oof_side_predictions.csv.zst",
            "join_key": ["timestamp_ns", "file_index", "segment_id", "side"],
            "refitting_in_this_phase": False,
            "in_sample_predictions_used": False,
        },
        "reporting": {
            "quantiles": list(QUANTILES),
            "dependence_aware_interval": f"moving block bootstrap, "
            f"{BOOTSTRAP_BLOCK_MINUTES} minute blocks, {BOOTSTRAP_DRAWS} draws",
            "iid_standard_errors_used": False,
            "label": "development estimates; nothing here is out of sample",
        },
        "seed": SEED,
        "inputs": inputs,
    }
