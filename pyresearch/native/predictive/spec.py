"""Pre-registered methodology for the native predictive decomposition.

Every choice that could be tuned against a validation score lives here and is written verbatim
into ``methodology.json`` before any model is fitted: fold geometry, purge length, feature list,
model hyper-parameters, seeds, target definitions and thresholds. Nothing in the modelling code
selects any of them.
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_predictive_v1"
DATA_DIR = ROOT / "data/research/native_predictive_v1"

SCHEMA = "crypto-hft-native-predictive-v1"
SEED = 0

# --------------------------------------------------------------------------------------------
# Folds
# --------------------------------------------------------------------------------------------
# Twelve contiguous wall-clock blocks across the ~71 h corpus, expanding window, validating on
# blocks 2..11. Blocks are hours long, not rows, because neighbouring 100 ms rows are almost the
# same observation.
N_BLOCKS = 12
FIRST_VALIDATION_BLOCK = 2
# Longest chain of forward information attached to any row: 30 s fill/race observation window
# plus a 5 s post-fill markout. Sixty seconds purges that with room to spare.
PURGE_SECONDS = 60.0
MAX_TARGET_HORIZON_SECONDS = 35.0

# --------------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------------
MOVE_HORIZONS_MS = (250, 500, 1000, 5000)
FILL_HORIZONS_MS = (500, 1000, 5000)
POSTFILL_HORIZONS_MS = (100, 500, 1000, 5000)
PRIMARY_MARKOUT_MS = 1000
# Sign threshold only. Chosen before any model was fitted and never moved: a fill is "good" when
# the mid one second later is on the favourable side of the quote price.
GOOD_FILL_THRESHOLD_TICKS = 0.0
# Handicaps applied to the fill time when rebuilding the fill-versus-adverse race, to test how
# much of the original statistic survives if the trade feed is assumed to lead the depth feed.
RACE_HANDICAPS_MS = (0, 25, 50, 100, 200, 500)

# --------------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------------
WINDOWS_MS = (100, 250, 500, 1000, 5000)
LEVEL_FLOW_WINDOWS_MS = (100, 1000)

# Absolute-frame features, used by the price-formation models.
BOOK_FEATURES = [
    "spread_ticks",
    "microprice_minus_mid_ticks",
    "bid_concentration_l5",
    "ask_concentration_l5",
    "bid_concentration_l10",
    "ask_concentration_l10",
    "bid_dispersion_ticks_l10",
    "ask_dispersion_ticks_l10",
]
DEPTH_FEATURES = [
    "log_bid_depth_l1",
    "log_ask_depth_l1",
    "log_bid_depth_l5",
    "log_ask_depth_l5",
    "log_bid_depth_l10",
    "log_ask_depth_l10",
]
IMBALANCE_FEATURES = [
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l5",
    "weighted_obi_l10",
]
ACTIVITY_FEATURES = [
    "time_since_trade_ms",
    "time_since_depth_ms",
    "time_since_bbo_change_ms",
    "time_since_mid_change_ms",
    "segment_age_ms",
]


def window_features() -> list[str]:
    names: list[str] = []
    for window in WINDOWS_MS:
        names += [
            f"net_depth_flow_l1_{window}ms",
            f"net_depth_flow_l5_{window}ms",
            f"depth_flow_pressure_l5_{window}ms",
            f"depth_flow_pressure_l10_{window}ms",
            f"trade_imbalance_{window}ms",
            f"signed_log_volume_{window}ms",
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ]
    for window in LEVEL_FLOW_WINDOWS_MS:
        names += [
            f"log_bid_depth_add_l1_{window}ms",
            f"log_bid_depth_remove_l1_{window}ms",
            f"log_ask_depth_add_l1_{window}ms",
            f"log_ask_depth_remove_l1_{window}ms",
        ]
    return names


ABSOLUTE_FEATURES = (
    BOOK_FEATURES + DEPTH_FEATURES + IMBALANCE_FEATURES + window_features() + ACTIVITY_FEATURES
)

# Side-normalised frame, used by the passive-fill and adverse-selection models. "own" is the
# side the hypothetical resting order sits on; every signed quantity is oriented so that a
# positive value favours that order.
SIDE_SYMMETRIC_FEATURES = [
    "spread_ticks",
    "own_concentration_l5",
    "opp_concentration_l5",
    "own_concentration_l10",
    "opp_concentration_l10",
    "own_dispersion_ticks_l10",
    "opp_dispersion_ticks_l10",
    "log_own_depth_l1",
    "log_opp_depth_l1",
    "log_own_depth_l5",
    "log_opp_depth_l5",
    "log_own_depth_l10",
    "log_opp_depth_l10",
    "signed_obi_l1",
    "signed_obi_l5",
    "signed_obi_l10",
    "signed_weighted_obi_l5",
    "signed_weighted_obi_l10",
    "signed_microprice_offset_ticks",
    "log_queue_ahead_lots",
    "queue_to_own_depth_l5",
]


def side_window_features() -> list[str]:
    names: list[str] = []
    for window in WINDOWS_MS:
        names += [
            f"signed_net_depth_flow_l1_{window}ms",
            f"signed_net_depth_flow_l5_{window}ms",
            f"signed_depth_flow_pressure_l5_{window}ms",
            f"signed_depth_flow_pressure_l10_{window}ms",
            f"signed_trade_imbalance_{window}ms",
            f"signed_log_volume_{window}ms",
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ]
    for window in LEVEL_FLOW_WINDOWS_MS:
        names += [
            f"log_own_depth_add_l1_{window}ms",
            f"log_own_depth_remove_l1_{window}ms",
            f"log_opp_depth_add_l1_{window}ms",
            f"log_opp_depth_remove_l1_{window}ms",
        ]
    return names


SIDE_FEATURES = SIDE_SYMMETRIC_FEATURES + side_window_features() + ACTIVITY_FEATURES

# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------
# Feature clipping quantiles, fitted on the training fold only, applied before standardisation.
# Markout and flow tails are extreme enough that an unclipped linear model is dominated by a
# handful of rows.
CLIP_QUANTILES = (0.001, 0.999)

# Clip bounds and the fill median are estimated from an evenly spaced stride through the
# training fold. Purely computational: an order statistic at these sample sizes is stable well
# past the precision that matters here.
PREPROCESSOR_SAMPLE_ROWS = 500_000

LOGISTIC_PARAMS = {"solver": "lbfgs", "C": 1.0, "max_iter": 200, "tol": 1e-4}
RIDGE_PARAMS = {"alpha": 1.0, "solver": "lsqr"}

LGBM_COMMON = {
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 500,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "max_bin": 127,
    "num_threads": 8,
    "seed": SEED,
    "bagging_seed": SEED,
    "feature_fraction_seed": SEED,
    "deterministic": True,
    "force_row_wise": True,
    "verbosity": -1,
}
LGBM_CLASSIFICATION = dict(LGBM_COMMON, objective="binary")
# L1 objective, not L2: post-fill markout tails are heavy and a squared loss would fit the
# flash-move outliers rather than the body of the distribution.
LGBM_REGRESSION = dict(LGBM_COMMON, objective="regression_l1")
LGBM_ROUNDS = 300

# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------
CALIBRATION_BINS = 10
BUCKET_QUANTILES = 5
BOOTSTRAP_BLOCK_MINUTES = 30
BOOTSTRAP_DRAWS = 500


def methodology() -> dict:
    """The full pre-registration, written before any model is fitted."""
    return {
        "schema": SCHEMA,
        "phase": "native_predictive_v1",
        "status": "pre_registered_before_fitting",
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
            "maker strategy search",
            "threshold optimisation",
            "fee, holding period, quote distance or order size tuning",
            "PnL, Sharpe or portfolio evaluation",
            "neural sequence models",
            "automated hyper-parameter search",
        ],
        "validation": {
            "scheme": "expanding-window chronologically blocked out-of-fold",
            "blocks": N_BLOCKS,
            "first_validation_block": FIRST_VALIDATION_BLOCK,
            "folds": N_BLOCKS - FIRST_VALIDATION_BLOCK,
            "purge_seconds": PURGE_SECONDS,
            "max_target_horizon_seconds": MAX_TARGET_HORIZON_SECONDS,
            "random_splits_used": False,
            "shuffled_k_fold_used": False,
            "label": "blocked OOF development estimates",
        },
        "targets": {
            "price_direction": "next_mid_move_dir among rows whose next mid move is observed "
            "inside the 30 s window",
            "move_intensity_horizons_ms": list(MOVE_HORIZONS_MS),
            "fill_horizons_ms": list(FILL_HORIZONS_MS),
            "postfill_horizons_ms": list(POSTFILL_HORIZONS_MS),
            "primary_markout_ms": PRIMARY_MARKOUT_MS,
            "good_fill_threshold_ticks": GOOD_FILL_THRESHOLD_TICKS,
            "race_handicaps_ms": list(RACE_HANDICAPS_MS),
            "censoring": "never recoded as a negative outcome; censored rows leave the "
            "denominator and are counted separately",
        },
        "features": {
            "absolute_frame": ABSOLUTE_FEATURES,
            "side_normalised_frame": SIDE_FEATURES,
            "windows_ms": list(WINDOWS_MS),
            "level_flow_windows_ms": list(LEVEL_FLOW_WINDOWS_MS),
            "causality": "every feature is a function of raw events with a receive timestamp "
            "at or before the decision instant, and every trailing window is cleared at the "
            "start of its segment",
            "per_level_reduction": "per-level add and remove are carried explicitly at L1 for "
            "the 100 ms and 1 s windows and aggregated into net L1 and L5 flow plus the "
            "exponentially weighted L5/L10 pressures elsewhere, rather than shipping all 200 "
            "raw per-level columns into a first baseline",
        },
        "models": {
            "order": ["naive", "linear", "lightgbm"],
            "naive_classification": "constant training-fold base rate",
            "naive_regression": "constant training-fold median",
            "linear_classification": {"estimator": "LogisticRegression", **LOGISTIC_PARAMS},
            "linear_regression": {"estimator": "Ridge", **RIDGE_PARAMS},
            "preprocessing": {
                "clip_quantiles": list(CLIP_QUANTILES),
                "fitted_on": "training fold only",
                "quantile_estimation": f"evenly spaced stride over at most "
                f"{PREPROCESSOR_SAMPLE_ROWS} training rows",
                "standardisation": "mean/std of the clipped training fold",
                "missing_values": "filled with the training-fold median for linear models, "
                "left as NaN for LightGBM",
            },
            "lightgbm_classification": LGBM_CLASSIFICATION,
            "lightgbm_regression": LGBM_REGRESSION,
            "lightgbm_rounds": LGBM_ROUNDS,
            "early_stopping": "not used; a fixed pre-registered round count avoids selecting "
            "on the validation block",
        },
        "reporting": {
            "aggregation_levels": ["rows", "fold blocks", "segments", "utc days"],
            "dependence_aware_interval": f"moving block bootstrap, {BOOTSTRAP_BLOCK_MINUTES} "
            f"minute blocks, {BOOTSTRAP_DRAWS} draws",
            "iid_standard_errors_used": False,
            "calibration_bins": CALIBRATION_BINS,
            "bucket_quantiles": BUCKET_QUANTILES,
        },
        "seed": SEED,
    }
