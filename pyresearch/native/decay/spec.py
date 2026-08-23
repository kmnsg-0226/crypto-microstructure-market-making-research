"""Pre-registered specification for phase 6 — signal decay and horizon extension.

Five phases established that the passive best-touch market-making line is falsified (3, 4A, 4B)
and that the sub-second directional monetisation of the sweep classifier is falsified on cost
(5A: best cell 0.87 bp against a hurdle grid starting at 1 bp). Neither line is reopened here.

This phase asks one question that has never been asked of this corpus:

    Does the microstructure information that is real and stable inside one second persist beyond
    five seconds, and does it become economically meaningful at horizons where a fixed basis-point
    cost is a small fraction of the move?

and one decomposition that decides it:

    Is a positive long-horizon markout merely inherited from the first one-to-five seconds of
    movement, or does signal strength at t predict *additional* movement after t + 5 s?

Every horizon, signal, bucket, drift control, purge, bootstrap geometry, cost hurdle,
classification rule and verdict rule is fixed in this file before any output is inspected. No
model is trained in this phase. No horizon, threshold or decile is selected as "best".
"""
from __future__ import annotations

from pathlib import Path

from pyresearch import ROOT
REPORT_DIR = ROOT / "research/native_decay_v1"
DATA_DIR = ROOT / "data/research/native_decay_v1"

SCHEMA = "crypto-hft-native-decay-v1"
SEED = 0

# ---------------------------------------------------------------------------------------------
# Horizons. Fixed by the brief. There is no "primary" horizon: this phase is about the profile
# across horizons, and nominating one would be the threshold selection the phase forbids.
# ---------------------------------------------------------------------------------------------
HORIZONS_S = (1, 2, 5, 10, 30, 60, 120, 300, 600)
HORIZONS_MS = tuple(h * 1000 for h in HORIZONS_S)

# The pivot for the cumulative / incremental decomposition. Fixed at 5 s because 5 s is the
# longest horizon any earlier phase measured, so "after 5 s" is exactly "beyond what is known".
PIVOT_S = 5
PIVOT_MS = PIVOT_S * 1000

# Horizons whose regenerated target must agree with an already-frozen artifact before any
# analysis runs. 1 s and 5 s exist as frozen phase 1 columns; 2 s exists as the phase 5A
# reconstruction, which was itself reconciled against the frozen columns.
AGREEMENT_HORIZONS_S = (1, 2, 5)
FROZEN_TARGET_COLUMNS = {1: "markout_1000ms_ticks", 5: "markout_5000ms_ticks"}
PHASE5A_TARGET_COLUMNS = {2: "markout_2000ms_ticks"}
# The regenerated target is a forward shift on the same 100 ms grid the frozen columns were
# written from, so agreement is required to be exact. Any disagreement stops the phase.
AGREEMENT_MAX_ABS_TICKS = 0.0
AGREEMENT_MAX_DISAGREEING_ROWS = 0
# The phase 5A 2 s column was reconstructed from the phase 3 mid path, a different artifact that
# is already documented to resolve 32 rows in 4.29 M one event apart inside violent moves. That
# published discrepancy is carried, not re-litigated: 2 s agreement is checked at that tolerance
# and the count is published.
PHASE5A_MAX_DISAGREEING_ROWS = 32

# ---------------------------------------------------------------------------------------------
# Signals. All frozen. Nothing is fitted, refitted, tuned or combined in this phase.
#
# Each signal is reduced to one signed per-instant number whose sign is the direction it implies
# and whose magnitude is its strength. The reductions are fixed, deterministic transforms of
# already-published columns, never estimates:
#
#   obi_*                raw, already signed and already in [-1, 1]
#   direction_p2_*       2p - 1, centring a probability on its own neutral point
#   sweep_dir_p4a        p(ask threatened) - p(bid threatened), the phase 5A direction convention
#                        (a threatened ask implies +1, a threatened bid implies -1) written as one
#                        per-instant number so that every signal is scored on the same rows
# ---------------------------------------------------------------------------------------------
PRIMARY_SIGNAL = "obi_l1"

SIGNALS = {
    "obi_l1": {
        "kind": "raw_book_state",
        "source": "native_dev_v1 phase 1 grid column obi_l1",
        "transform": "identity",
        "role": "primary",
        "is_out_of_fold": False,
        "note": "a raw observable, not a fitted score; it needs no fold and is defined on every "
        "row of the corpus",
    },
    "obi_l5": {
        "kind": "raw_book_state",
        "source": "native_dev_v1 phase 1 grid column obi_l5",
        "transform": "identity",
        "role": "secondary",
        "is_out_of_fold": False,
    },
    "obi_l10": {
        "kind": "raw_book_state",
        "source": "native_dev_v1 phase 1 grid column obi_l10",
        "transform": "identity",
        "role": "secondary",
        "is_out_of_fold": False,
    },
    "direction_p2_logistic": {
        "kind": "frozen_oof_model_score",
        "source": "native_predictive_v1 oof_price_predictions price_direction_linear",
        "transform": "2p - 1",
        "role": "primary",
        "is_out_of_fold": True,
        "note": "phase 2 headline direction model, blocked OOF, ROC AUC 0.7412",
    },
    "direction_p2_lightgbm": {
        "kind": "frozen_oof_model_score",
        "source": "native_predictive_v1 oof_price_predictions price_direction_lightgbm",
        "transform": "2p - 1",
        "role": "secondary",
        "is_out_of_fold": True,
    },
    "sweep_dir_p4a": {
        "kind": "frozen_oof_model_score",
        "source": "native_cancel_falsification_v1 sweep_scores sweep_p, both sides",
        "transform": "p(side=ask) - p(side=bid)",
        "role": "primary",
        "is_out_of_fold": True,
        "note": "phase 4A sweep model, scored OOF at 100 ms by phase 4B; the difference is the "
        "phase 5A direction convention expressed per instant and is not a fitted combination",
    },
}

# "existing frozen combined/simple OOF score only if already available": phase 5A fitted
# book+flow+sweep directional models but published metrics only, never a stored OOF prediction
# file. No frozen combined score exists in the repository, so none is used, and none is fitted
# here. This is recorded rather than worked around.
COMBINED_SIGNAL_AVAILABLE = False

SIGNAL_NEUTRAL = 0.0  # every signal above is signed and centred on zero after its transform

# ---------------------------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------------------------
# The headline population is the set of instants that carry every signal, so that the signals are
# compared on identical rows: the phase 2 validation blocks, which are also exactly the rows the
# phase 4B sweep scores cover. obi_* is additionally reported on the full corpus as a secondary
# table, clearly labelled, because it needs no fold.
HEADLINE_POPULATION = "oof_scored_rows"
FULL_CORPUS_SECONDARY_FOR = ("obi_l1", "obi_l5", "obi_l10")

DECILES = 10

# ---------------------------------------------------------------------------------------------
# Purge. Horizon specific, per the brief: purge = horizon + 60 s.
#
# The frozen scores were produced by models whose training windows end 60 s before their
# validation block. An evaluated row at t carries a target window [t, t + h]. Requiring
# t >= block_start + h + 60 s makes the whole span of length h *behind* the evaluated row also
# post-training, so no evaluated target window overlaps — or sits within h of — anything the
# scoring model could have seen. The old flat 60 s purge is not reused at any horizon.
# ---------------------------------------------------------------------------------------------
BASE_PURGE_S = 60.0


def purge_seconds(horizon_s: float) -> float:
    return float(horizon_s) + BASE_PURGE_S


# ---------------------------------------------------------------------------------------------
# Dependence. 100 ms rows are never treated as independent anywhere in this phase.
# Block length = max(30 min, 12 * h), 500 draws, seed 0.
# ---------------------------------------------------------------------------------------------
BOOTSTRAP_DRAWS = 500
BOOTSTRAP_MIN_BLOCK_S = 30 * 60.0
BOOTSTRAP_BLOCK_MULTIPLE = 12
BOOTSTRAP_LOW_PCT = 5.0
BOOTSTRAP_HIGH_PCT = 95.0


def bootstrap_block_seconds(horizon_s: float) -> float:
    return max(BOOTSTRAP_MIN_BLOCK_S, BOOTSTRAP_BLOCK_MULTIPLE * float(horizon_s))


# ---------------------------------------------------------------------------------------------
# Drift control. The corpus is a 13-14 % three-day BTC rally, so at minute horizons the
# unconditional drift is a first-order contaminant of any mean markout. Two fixed controls, both
# applied to already-realised returns only. No future return is ever used as a feature.
# ---------------------------------------------------------------------------------------------
DEMEAN_BLOCK_MINUTES = 30
DRIFT_CONTROLS = (
    "raw",       # no adjustment, published for completeness
    "demeaned",  # the row's own 30-minute time block mean markout at that horizon subtracted
)
DECISION_DRIFT_CONTROL = "demeaned"
# Up and down are always reported separately. A common drift shifts both legs the same way; a
# real directional signal moves them in opposite directions.
REPORT_SIDES_SEPARATELY = True

# ---------------------------------------------------------------------------------------------
# Non-overlapping sample. Deterministic, horizon spaced, no randomness.
# ---------------------------------------------------------------------------------------------
ANCHOR_RULE = (
    "within each segment, keep the rows whose index from the segment's first row is an exact "
    "multiple of h / 100 ms, so consecutive kept windows touch but never overlap"
)

# ---------------------------------------------------------------------------------------------
# Cost hurdles. Diagnostic only, exactly the phase 5A grid, one way, in basis points.
# Not a live account fee tier. No PnL, Sharpe, entry rule or holding period anywhere.
# ---------------------------------------------------------------------------------------------
COST_HURDLE_BPS = (1.0, 2.0, 3.0, 5.0, 7.5, 10.0)
TICK_SIZE_USD = 0.10
REPOSITORY_REFERENCE_FEES = {"taker_bps_per_side": 5.0, "maker_bps_per_leg": 2.0}

# ---------------------------------------------------------------------------------------------
# The decision quantity, and what "resolved" and "stable" mean. Fixed before any result is seen.
#
# Decision quantity: the drift-demeaned top-minus-bottom signal-decile spread of the markout at
# horizon h. A decile spread is used rather than a raw conditional mean because any drift common
# to all rows of a period cancels in the difference, which is the first line of defence against
# the corpus's three-day rally; the demeaning is the second.
#
# It is estimated on the full purged population, because that carries the most precise centre,
# and its uncertainty comes from the horizon-aware moving block bootstrap, because that is what
# accounts for the dependence between overlapping 100 ms rows. The deterministic non-overlapping
# anchor sample is computed alongside as an independence check.
#
#   resolved  the 5-95 % bootstrap interval excludes zero AND the anchor-sample point estimate
#             carries the same sign; requiring both means a claim can never rest on the
#             overlap-inflated sample alone
#   stable    at least 60 % of the horizon's chronological blocks carry that same sign
#
# Deciles are formed once, globally, over the evaluated population of that signal and horizon.
# They are never re-ranked per block and never formed on any future return.
# ---------------------------------------------------------------------------------------------
DECISION_QUANTITY = "demeaned top-minus-bottom signal-decile spread of the markout"
DECISION_SAMPLE = "full purged population, bootstrap for uncertainty, anchors as a sign check"
STABILITY_MIN_BLOCK_SHARE = 0.60

# The incremental decomposition. Deciles come from the signal at t in both terms, so the two
# reconcile exactly: cumulative(h) = cumulative(5 s) + incremental(5 s -> h).
INCREMENTAL_DEFINITION = "decile spread of markout(t -> t+h) - markout(t -> t+5 s)"
RECONCILIATION_TOLERANCE_TICKS = 1e-9

CLASSIFICATION_RULES = {
    "ultra_short_only": "cumulative edge is resolved at some h <= 5 s, and the incremental edge "
    "after 5 s is unresolved at every h > 5 s",
    "short_continuation": "the incremental edge after 5 s is resolved and positive at some "
    "h <= 60 s, and unresolved or negative at every h >= 120 s",
    "medium_horizon_persistence": "the incremental edge after 5 s is resolved, positive and "
    "stable at some h >= 120 s",
    "reversal": "the incremental edge after 5 s is resolved and negative at some h > 5 s, and "
    "no resolved positive incremental edge at any longer horizon",
    "ambiguous": "resolved incremental edges of both signs across horizons, or a resolved edge "
    "that fails the block-stability floor, or a sign that flips between the raw and demeaned "
    "views",
}
CLASSIFICATION_ORDER = (
    "reversal",
    "medium_horizon_persistence",
    "short_continuation",
    "ultra_short_only",
    "ambiguous",
)

# ---------------------------------------------------------------------------------------------
# Project verdict rules, fixed before any result is seen.
# ---------------------------------------------------------------------------------------------
VERDICT_RULES = {
    "A": "current L2 monetisation closed - no signal shows a resolved positive incremental edge "
    "after 5 s at any horizon, and no gross edge at any horizon and any decile reaches the "
    "cheapest one-way hurdle of 1 bp",
    "B": "longer-horizon information survives but is marginal - a resolved, stable positive "
    "incremental edge after 5 s exists, but the gross edge stays below a 2 bp round trip "
    "(the cheapest hurdle in the grid taken as 1 bp per side)",
    "C": "economically material persistence - a resolved, stable gross edge at some horizon "
    "exceeds a 2 bp round trip, driven by incremental movement after 5 s rather than inherited "
    "from the first 5 s",
    "D": "reversal structure - a resolved, stable negative incremental edge after 5 s dominates "
    "the profile",
    "E": "insufficient data - at every horizon of 120 s and beyond the bootstrap interval on the "
    "decision quantity spans zero by more than the quantity itself, so the corpus cannot resolve "
    "the question either way",
}

# ---------------------------------------------------------------------------------------------
# What this phase may not do.
# ---------------------------------------------------------------------------------------------
PROHIBITED = (
    "training any model, horizon specific or otherwise",
    "combining signals into a new score",
    "selecting a horizon, threshold, decile or regime as best",
    "any entry or exit rule, position sizing, stops, targets or holding-period optimisation",
    "strategy PnL, Sharpe or equity curve",
    "live account fees",
    "using any future return as a feature",
    "reopening the falsified passive market-making line",
    "reopening the falsified sub-second directional monetisation line",
    "reading, loading or summarising post-native_dev_v1 AWS data or the Tardis holdout",
    "re-freezing the failing passive_binary_sha256 audit gate",
)


def ticks_to_bps(ticks: float, mid_ticks: float) -> float:
    """Ticks of mid movement as basis points of notional at that mid."""
    return 1e4 * ticks / mid_ticks


def methodology(inputs: dict, extra: dict | None = None) -> dict:
    doc = {
        "schema": SCHEMA,
        "phase": "native_decay_v1",
        "question": "does microstructure information that is real inside one second persist "
        "beyond five seconds, and does signal strength at t predict additional movement after "
        "t + 5 s rather than merely inheriting the first five seconds of it?",
        "status": "descriptive decay study on frozen signals and development data; no model is "
        "trained and no horizon is selected",
        "corpus": {
            "spec": "research/specs/native_dev_v1.json",
            "development_only": True,
            "is_out_of_sample": False,
            "label": "blocked out-of-fold development estimates",
            "forward_data_untouched": [
                "the rotation-enabled AWS capture file",
                "every later AWS capture",
                "every post-native_dev_v1 native capture",
                "the Tardis June/July/August holdout",
            ],
        },
        "prohibited_in_this_phase": list(PROHIBITED),
        "horizons_s": list(HORIZONS_S),
        "pivot_s": PIVOT_S,
        "no_primary_horizon": "the phase reports the profile across horizons; nominating one "
        "would be the selection this phase forbids",
        "signals": SIGNALS,
        "combined_frozen_oof_score_available": COMBINED_SIGNAL_AVAILABLE,
        "models_trained_in_this_phase": 0,
        "targets": {
            "definition": "mid(t + h) - mid(t) in ticks, both mids read from the frozen phase 1 "
            "100 ms grid column mid_ticks",
            "construction": "forward shift of h / 100 ms rows inside the same segment",
            "censoring": "a horizon that would cross a segment edge is censored, never zero",
            "no_target_crosses_a_segment_boundary": True,
            "agreement_horizons_s": list(AGREEMENT_HORIZONS_S),
            "agreement_rule": "regenerated 1 s and 5 s must equal the frozen phase 1 columns "
            "exactly; 2 s is checked against the phase 5A reconstruction at that artifact's own "
            "published tolerance. Disagreement stops the phase.",
        },
        "population": {
            "headline": HEADLINE_POPULATION,
            "reason": "the rows that carry every signal, so signals are compared on identical "
            "rows",
            "full_corpus_secondary_for": list(FULL_CORPUS_SECONDARY_FOR),
        },
        "purge": {
            "rule": "horizon specific, purge = horizon + 60 s",
            "base_s": BASE_PURGE_S,
            "old_flat_purge_reused": False,
            "by_horizon_s": {str(h): purge_seconds(h) for h in HORIZONS_S},
            "applied_as": "an evaluated row must sit at least (h + 60 s) after the start of its "
            "own validation block",
        },
        "dependence": {
            "rows_treated_as_independent": False,
            "scheme": "horizon-aware moving block bootstrap",
            "block_seconds_by_horizon": {
                str(h): bootstrap_block_seconds(h) for h in HORIZONS_S
            },
            "draws": BOOTSTRAP_DRAWS,
            "seed": SEED,
            "iid_standard_errors_used": False,
            "non_overlapping_anchor_rule": ANCHOR_RULE,
            "effective_sample_size_reported": True,
        },
        "drift_control": {
            "reason": "the corpus is a three-day 13-14 % BTC rally; at minute horizons the "
            "unconditional drift is a first-order contaminant of any mean markout",
            "controls": list(DRIFT_CONTROLS),
            "decision_control": DECISION_DRIFT_CONTROL,
            "demean_block_minutes": DEMEAN_BLOCK_MINUTES,
            "future_returns_used_as_features": False,
            "sides_reported_separately": REPORT_SIDES_SEPARATELY,
        },
        "cost_hurdles": {
            "units": "basis points of notional, one way",
            "grid": list(COST_HURDLE_BPS),
            "is_a_live_account_fee_tier": False,
            "label": "diagnostic sensitivity values only",
            "repository_reference_values": REPOSITORY_REFERENCE_FEES,
            "round_trip": "twice the one-way hurdle, reported as arithmetic",
            "pnl_or_sharpe_computed": False,
        },
        "classification_rules": CLASSIFICATION_RULES,
        "classification_order": list(CLASSIFICATION_ORDER),
        "stability_min_block_share": STABILITY_MIN_BLOCK_SHARE,
        "verdict_rules": VERDICT_RULES,
        "seed": SEED,
        "inputs": inputs,
    }
    if extra:
        doc.update(extra)
    return doc
