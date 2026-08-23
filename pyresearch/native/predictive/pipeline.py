"""Run the native predictive decomposition and write every artifact.

    python -m pyresearch.native.predictive.pipeline frames    # reduce the phase 1 export to model frames
    python -m pyresearch.native.predictive.pipeline timing    # cross-stream timing event study
    python -m pyresearch.native.predictive.pipeline models    # blocked OOF estimation for A, B and C
    python -m pyresearch.native.predictive.pipeline joint     # fill / adverse-selection joint diagnostic
    python -m pyresearch.native.predictive.pipeline all
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from pyresearch.native.predictive import data, modeling, spec, timing
from pyresearch.native.predictive.modeling import Problem

FLOAT_FORMAT = "%.10g"
SIDE_META = ["timestamp_ns", "file_index", "segment_id", "side"]


# --------------------------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------------------------
def build_side_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Stack the bid and ask views of every decision row into one side-normalised frame.

    ``side`` is carried as metadata only. It is deliberately not a feature: a pooled model that
    could read the side would not test symmetry, it would just relearn two models.
    """
    parts = []
    for code, side in enumerate(("bid", "ask")):
        view = data.side_view(frame, side)
        view["timestamp_ns"] = frame["timestamp_ns"].to_numpy()
        view["file_index"] = frame["file_index"].to_numpy()
        view["segment_id"] = frame["segment_id"].to_numpy()
        view["side"] = np.int8(code)
        for horizon in spec.FILL_HORIZONS_MS:
            view[f"y_fill_{horizon}ms"] = frame[f"y_{side}_fill_{horizon}ms"].to_numpy()
        view["y_through_given_fill"] = frame[f"y_{side}_through_given_fill"].to_numpy()
        view[f"y_markout_{spec.PRIMARY_MARKOUT_MS}ms"] = frame[
            f"y_{side}_markout_{spec.PRIMARY_MARKOUT_MS}ms"
        ].to_numpy()
        view["y_good_fill"] = frame[f"y_{side}_good_fill"].to_numpy()
        view["filled"] = np.isfinite(frame[f"{side}_time_to_fill_ms"].to_numpy())
        view["time_to_fill_ms"] = frame[f"{side}_time_to_fill_ms"].to_numpy()
        view["fill_via_trade_through"] = frame[f"{side}_fill_via_trade_through"].to_numpy()
        for horizon in spec.POSTFILL_HORIZONS_MS:
            view[f"postfill_markout_{horizon}ms_ticks"] = frame[
                f"{side}_postfill_markout_{horizon}ms_ticks"
            ].to_numpy()
        parts.append(view)
    pooled = pd.concat(parts, ignore_index=True)
    return pooled.sort_values(["timestamp_ns", "side"], ignore_index=True)


# --------------------------------------------------------------------------------------------
# Problems
# --------------------------------------------------------------------------------------------
def price_problems() -> list[Problem]:
    problems = [
        Problem(
            name="price_direction",
            family="price",
            target="y_direction",
            task="classification",
            features=spec.ABSOLUTE_FEATURES,
            description="direction of the next observed mid move, up versus down",
            tags={"horizon_ms": "next_move"},
        )
    ]
    for horizon in spec.MOVE_HORIZONS_MS:
        problems.append(
            Problem(
                name=f"move_within_{horizon}ms",
                family="price",
                target=f"y_move_{horizon}ms",
                task="classification",
                features=spec.ABSOLUTE_FEATURES,
                description=f"any mid move inside {horizon} ms",
                tags={"horizon_ms": str(horizon)},
            )
        )
    return problems


def filled_only(frame: pd.DataFrame) -> np.ndarray:
    return frame["filled"].to_numpy(dtype=bool)


def side_problems() -> list[Problem]:
    problems: list[Problem] = []
    for horizon in spec.FILL_HORIZONS_MS:
        problems.append(
            Problem(
                name=f"fill_{horizon}ms",
                family="fill",
                target=f"y_fill_{horizon}ms",
                task="classification",
                features=spec.SIDE_FEATURES,
                description=f"full passive fill inside {horizon} ms",
                tags={"horizon_ms": str(horizon), "sides": "pooled"},
            )
        )
    problems.append(
        Problem(
            name="through_given_fill",
            family="fill_mechanism",
            target="y_through_given_fill",
            task="classification",
            features=spec.SIDE_FEATURES,
            # Trained on filled opportunities, predicted everywhere: the joint diagnostic needs
            # a mechanism estimate for opportunities that were never filled too.
            train_mask=filled_only,
            description="the fill arrives as a trade-through rather than at the quote",
            tags={"horizon_ms": "30000", "sides": "pooled"},
        )
    )
    problems.append(
        Problem(
            name=f"markout_{spec.PRIMARY_MARKOUT_MS}ms",
            family="adverse",
            target=f"y_markout_{spec.PRIMARY_MARKOUT_MS}ms",
            task="regression",
            features=spec.SIDE_FEATURES,
            train_mask=filled_only,
            description="signed quote-relative mid markout one second after the fill",
            tags={"horizon_ms": str(spec.PRIMARY_MARKOUT_MS), "sides": "pooled"},
        )
    )
    problems.append(
        Problem(
            name="good_fill_1s",
            family="adverse",
            target="y_good_fill",
            task="classification",
            features=spec.SIDE_FEATURES,
            train_mask=filled_only,
            description="the one-second post-fill markout is on the favourable side of zero",
            tags={"horizon_ms": "1000", "sides": "pooled"},
        )
    )
    return problems


def side_split_problems() -> list[Problem]:
    """Bid-only and ask-only twins of the two primary side problems, to test symmetry."""
    problems = []
    for side_code, side in enumerate(("bid", "ask")):
        problems.append(
            Problem(
                name=f"fill_1000ms_{side}_only",
                family="fill",
                target="y_fill_1000ms",
                task="classification",
                features=spec.SIDE_FEATURES,
                train_mask=lambda frame, code=side_code: frame["side"].to_numpy() == code,
                score_mask=lambda frame, code=side_code: frame["side"].to_numpy() == code,
                description=f"full passive fill inside 1000 ms, {side} orders only",
                tags={"horizon_ms": "1000", "sides": side},
            )
        )
        problems.append(
            Problem(
                name=f"markout_1000ms_{side}_only",
                family="adverse",
                target=f"y_markout_{spec.PRIMARY_MARKOUT_MS}ms",
                task="regression",
                features=spec.SIDE_FEATURES,
                train_mask=lambda frame, code=side_code: filled_only(frame)
                & (frame["side"].to_numpy() == code),
                score_mask=lambda frame, code=side_code: frame["side"].to_numpy() == code,
                description=f"one-second post-fill markout, {side} orders only",
                tags={"horizon_ms": "1000", "sides": side},
            )
        )
    return problems


# --------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------
def write_methodology() -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (spec.REPORT_DIR / "methodology.json").write_text(
        json.dumps(spec.methodology(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_timing() -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    events = timing.load_events()
    timing.latency_table(events).to_csv(
        spec.REPORT_DIR / "cross_stream_timing.csv", index=False, float_format=FLOAT_FORMAT
    )
    timing.category_summary(events).to_csv(
        spec.REPORT_DIR / "cross_stream_timing_summary.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    del events
    race_columns = [
        f"{side}_{name}"
        for side in ("bid", "ask")
        for name in (
            "time_to_fill_ms",
            "time_to_mid_adverse_ms",
            "time_to_quote_gone_ms",
            "time_to_best_adverse_ms",
        )
    ]
    frame = data.load_model_frame(columns=race_columns)
    timing.race_handicap_table(frame).to_csv(
        spec.REPORT_DIR / "race_handicap.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("timing artifacts written")


def _run_group(
    problems: list[Problem],
    frame: pd.DataFrame,
    folds: list[data.Fold],
    oof_name: str,
) -> tuple[list[dict], list[pd.DataFrame], list[pd.DataFrame], list[pd.DataFrame]]:
    pooled_rows: list[dict] = []
    fold_tables: list[pd.DataFrame] = []
    weight_tables: list[pd.DataFrame] = []
    calibrations: list[pd.DataFrame] = []
    oof_wide: pd.DataFrame | None = None
    for problem in problems:
        oof, fold_metrics, weights = modeling.run_problem(problem, frame, folds)
        if oof.empty:
            continue
        fold_tables.append(fold_metrics)
        if not weights.empty:
            weights.insert(0, "family", problem.family)
            weight_tables.append(weights)
        pooled_rows.extend(modeling.pooled_metrics(problem, oof))
        if problem.task == "classification":
            for model in ("linear", "lightgbm"):
                calibrations.append(modeling.calibration(problem, oof, model))
        keys = [c for c in SIDE_META if c in oof.columns]
        renamed = oof[keys + ["y"] + [f"pred_{m}" for m in modeling.MODELS]].rename(
            columns={
                "y": f"y_{problem.name}",
                **{f"pred_{m}": f"{problem.name}_{m}" for m in modeling.MODELS},
            }
        )
        oof_wide = (
            renamed if oof_wide is None else oof_wide.merge(renamed, on=keys, how="outer")
        )
        print(f"  {problem.name}: {len(oof):,} OOF rows over {oof['fold'].nunique()} folds")
    if oof_wide is not None:
        spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
        oof_wide.sort_values(
            [c for c in SIDE_META if c in oof_wide.columns], ignore_index=True
        ).to_csv(spec.DATA_DIR / oof_name, index=False, float_format=FLOAT_FORMAT)
    return pooled_rows, fold_tables, weight_tables, calibrations


def run_models() -> None:
    write_methodology()
    frame = data.load_model_frame()
    timestamps = frame["timestamp_ns"].to_numpy()
    folds = data.build_folds(timestamps)
    fold_table = data.fold_table(timestamps, folds)
    fold_table.to_csv(spec.REPORT_DIR / "folds.csv", index=False, float_format=FLOAT_FORMAT)
    print(fold_table[["fold", "train_rows", "validation_rows", "validation_start_utc"]])

    pooled: list[dict] = []
    folds_out: list[pd.DataFrame] = []
    weights_out: list[pd.DataFrame] = []
    calibration_out: list[pd.DataFrame] = []

    print("price problems")
    result = _run_group(price_problems(), frame, folds, "oof_price_predictions.csv.zst")
    pooled += result[0]
    folds_out += result[1]
    weights_out += result[2]
    calibration_out += result[3]

    print("side problems")
    side_frame = build_side_frame(frame)
    del frame
    result = _run_group(
        side_problems() + side_split_problems(),
        side_frame,
        folds,
        "oof_side_predictions.csv.zst",
    )
    pooled += result[0]
    folds_out += result[1]
    weights_out += result[2]
    calibration_out += result[3]
    del side_frame

    pooled_table = pd.DataFrame(pooled)
    fold_metrics = pd.concat(folds_out, ignore_index=True)
    for family, path in (
        ("price", "price_direction_metrics.csv"),
        ("fill", "fill_metrics.csv"),
        ("fill_mechanism", "fill_mechanism_metrics.csv"),
        ("adverse", "adverse_selection_metrics.csv"),
    ):
        subset = pooled_table[pooled_table["family"] == family]
        if family == "price":
            subset = pooled_table[
                (pooled_table["family"] == "price")
                & (pooled_table["problem"] == "price_direction")
            ]
        subset.to_csv(spec.REPORT_DIR / path, index=False, float_format=FLOAT_FORMAT)
    pooled_table[
        (pooled_table["family"] == "price") & (pooled_table["problem"] != "price_direction")
    ].to_csv(
        spec.REPORT_DIR / "move_intensity_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    pooled_table.to_csv(
        spec.REPORT_DIR / "pooled_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    fold_metrics.to_csv(
        spec.REPORT_DIR / "fold_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    if calibration_out:
        pd.concat([c for c in calibration_out if not c.empty], ignore_index=True).to_csv(
            spec.REPORT_DIR / "calibration.csv", index=False, float_format=FLOAT_FORMAT
        )

    weights = pd.concat(weights_out, ignore_index=True)
    linear = weights[weights["model"] == "linear"]
    linear.groupby(["problem", "feature"], as_index=False).agg(
        mean_coefficient=("value", "mean"),
        median_coefficient=("value", "median"),
        min_coefficient=("value", "min"),
        max_coefficient=("value", "max"),
        folds_positive=("value", lambda values: float((values > 0).mean())),
        folds=("value", "size"),
    ).to_csv(
        spec.REPORT_DIR / "model_coefficients.csv", index=False, float_format=FLOAT_FORMAT
    )
    gbm = weights[weights["model"] == "lightgbm"]
    importance = gbm.groupby(["problem", "feature"], as_index=False).agg(
        mean_gain=("value", "mean"), folds=("value", "size")
    )
    importance["gain_share"] = importance["mean_gain"] / importance.groupby("problem")[
        "mean_gain"
    ].transform("sum")
    importance.sort_values(
        ["problem", "mean_gain"], ascending=[True, False], ignore_index=True
    ).to_csv(spec.REPORT_DIR / "feature_importance.csv", index=False, float_format=FLOAT_FORMAT)
    print("model artifacts written")


# --------------------------------------------------------------------------------------------
# Joint diagnostic
# --------------------------------------------------------------------------------------------
def bucket_table(
    frame: pd.DataFrame, signal: str, outcomes: dict[str, str], buckets: int
) -> pd.DataFrame:
    values = pd.to_numeric(frame[signal], errors="coerce")
    try:
        labels = pd.qcut(values, buckets, labels=False, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    work = frame.assign(_bucket=labels, _signal=values).dropna(subset=["_bucket"])
    aggregation = {"observations": ("_signal", "size"), "signal_mean": ("_signal", "mean")}
    for name, column in outcomes.items():
        aggregation[name] = (column, "mean")
    table = work.groupby("_bucket", observed=True).agg(**aggregation).reset_index()
    table = table.rename(columns={"_bucket": "bucket"})
    table.insert(0, "signal", signal)
    return table


def run_joint() -> None:
    """Are the states that are easiest to fill also the states with the worst markout?

    Everything here uses out-of-fold predictions only, and it is a structural diagnostic, not a
    trading rule: no threshold is chosen and no expected value is computed.
    """
    oof = pd.read_csv(
        spec.DATA_DIR / "oof_side_predictions.csv.zst",
        usecols=SIDE_META
        + [
            "y_fill_5000ms",
            "fill_5000ms_lightgbm",
            f"markout_{spec.PRIMARY_MARKOUT_MS}ms_lightgbm",
        ],
    )
    side_frame = data.load_model_frame(
        columns=["timestamp_ns", "file_index", "segment_id"]
        + [
            f"{side}_{name}"
            for side in ("bid", "ask")
            for name in (
                "time_to_fill_ms",
                "fill_via_trade_through",
                f"postfill_markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks",
            )
        ]
    )
    realised = []
    for code, side in enumerate(("bid", "ask")):
        block = pd.DataFrame(
            {
                "timestamp_ns": side_frame["timestamp_ns"].to_numpy(),
                "file_index": side_frame["file_index"].to_numpy(),
                "segment_id": side_frame["segment_id"].to_numpy(),
                "side": np.int8(code),
                "filled": np.isfinite(side_frame[f"{side}_time_to_fill_ms"].to_numpy()),
                "fill_via_trade_through": side_frame[
                    f"{side}_fill_via_trade_through"
                ].to_numpy(),
                "realised_markout_ticks": side_frame[
                    f"{side}_postfill_markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"
                ].to_numpy(),
            }
        )
        realised.append(block)
    merged = oof.merge(
        pd.concat(realised, ignore_index=True),
        on=["timestamp_ns", "file_index", "segment_id", "side"],
        how="left",
    )

    fill_column = "fill_5000ms_lightgbm"
    adverse_column = f"markout_{spec.PRIMARY_MARKOUT_MS}ms_lightgbm"
    usable = merged[merged[fill_column].notna() & merged[adverse_column].notna()].copy()
    usable["fill_quintile"] = pd.qcut(
        usable[fill_column], spec.BUCKET_QUANTILES, labels=False, duplicates="drop"
    )
    usable["adverse_quintile"] = pd.qcut(
        usable[adverse_column], spec.BUCKET_QUANTILES, labels=False, duplicates="drop"
    )
    joint = (
        usable.groupby(["fill_quintile", "adverse_quintile"], observed=True)
        .agg(
            opportunities=("filled", "size"),
            filled_opportunities=("filled", "sum"),
            fill_rate=("filled", "mean"),
            trade_through_rate=("fill_via_trade_through", "mean"),
            mean_markout_ticks=("realised_markout_ticks", "mean"),
            median_markout_ticks=("realised_markout_ticks", "median"),
            predicted_fill=(fill_column, "mean"),
            predicted_markout=(adverse_column, "mean"),
        )
        .reset_index()
    )
    joint.to_csv(
        spec.REPORT_DIR / "joint_fill_adverse_bucket.csv", index=False, float_format=FLOAT_FORMAT
    )

    marginal = bucket_table(
        usable,
        fill_column,
        {
            # Both the matching horizon and the 30 s window, so the calibration of the model
            # being bucketed is readable without a horizon mismatch.
            "realised_fill_5000ms": "y_fill_5000ms",
            "fill_rate_30000ms": "filled",
            "trade_through_rate": "fill_via_trade_through",
            "mean_markout_ticks": "realised_markout_ticks",
        },
        10,
    )
    marginal.to_csv(
        spec.REPORT_DIR / "predicted_fill_vs_markout.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    print(f"joint diagnostic written over {len(usable):,} out-of-fold opportunities")


def robustness_problems() -> tuple[list[Problem], list[Problem]]:
    """Twins of the two headline problems with the in-segment clock feature removed.

    ``segment_age_ms`` is causal and was pre-registered, but it is a position-in-session index
    rather than a book state, and it carries the largest single gain share in the markout model.
    Dropping it says how much of each result is microstructure and how much is knowing where in
    the segment the decision sits. This is a robustness check, not a feature search: no result
    below is used to choose a feature set.
    """
    absolute = [f for f in spec.ABSOLUTE_FEATURES if f != "segment_age_ms"]
    side = [f for f in spec.SIDE_FEATURES if f != "segment_age_ms"]
    price = [
        Problem(
            name="price_direction_no_clock",
            family="robustness",
            target="y_direction",
            task="classification",
            features=absolute,
            description="price direction without segment_age_ms",
            tags={"horizon_ms": "next_move", "variant": "no_clock"},
        )
    ]
    sided = [
        Problem(
            name=f"markout_{spec.PRIMARY_MARKOUT_MS}ms_no_clock",
            family="robustness",
            target=f"y_markout_{spec.PRIMARY_MARKOUT_MS}ms",
            task="regression",
            features=side,
            train_mask=filled_only,
            description="post-fill markout without segment_age_ms",
            tags={"horizon_ms": str(spec.PRIMARY_MARKOUT_MS), "variant": "no_clock"},
        ),
        Problem(
            name="good_fill_1s_no_clock",
            family="robustness",
            target="y_good_fill",
            task="classification",
            features=side,
            train_mask=filled_only,
            description="good-fill classification without segment_age_ms",
            tags={"horizon_ms": "1000", "variant": "no_clock"},
        ),
    ]
    return price, sided


def run_robustness() -> None:
    frame = data.load_model_frame()
    folds = data.build_folds(frame["timestamp_ns"].to_numpy())
    price, sided = robustness_problems()
    rows: list[dict] = []
    for problem in price:
        oof, _, _ = modeling.run_problem(problem, frame, folds)
        rows.extend(modeling.pooled_metrics(problem, oof))
        print(f"  {problem.name}: {len(oof):,} OOF rows")
    side_frame = build_side_frame(frame)
    del frame
    for problem in sided:
        oof, _, _ = modeling.run_problem(problem, side_frame, folds)
        rows.extend(modeling.pooled_metrics(problem, oof))
        print(f"  {problem.name}: {len(oof):,} OOF rows")
    pd.DataFrame(rows).to_csv(
        spec.REPORT_DIR / "robustness_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("robustness artifacts written")


def run_descriptive() -> None:
    """Population and censoring bookkeeping, and the markout split by fill mechanism.

    Purely descriptive and in-sample: these are the denominators every later statement has to be
    read against, not estimates of anything.
    """
    columns = ["timestamp_ns", "file_index", "segment_id", "remaining_ns"] + [
        f"{side}_{name}"
        for side in ("bid", "ask")
        for name in (
            "time_to_fill_ms",
            "fill_via_trade_through",
            "fill_500ms",
            "fill_1000ms",
            "fill_5000ms",
            "fill_before_observed_mid_adverse",
        )
        + tuple(f"postfill_markout_{h}ms_ticks" for h in spec.POSTFILL_HORIZONS_MS)
    ]
    frame = data.load_model_frame(columns=columns)
    rows = []
    for side in ("bid", "ask"):
        eligible = len(frame)
        filled = np.isfinite(frame[f"{side}_time_to_fill_ms"].to_numpy())
        for horizon in spec.FILL_HORIZONS_MS:
            flags = frame[f"{side}_fill_{horizon}ms"].to_numpy(dtype="float64")
            evaluable = np.isfinite(flags)
            rows.append(
                {
                    "side": side,
                    "statistic": f"fill_{horizon}ms",
                    "eligible": eligible,
                    "evaluable": int(evaluable.sum()),
                    "censored_or_unavailable": int(eligible - evaluable.sum()),
                    "positives": int(np.nansum(flags)),
                    "rate": float(np.nanmean(flags)),
                }
            )
        rows.append(
            {
                "side": side,
                "statistic": "full_fill_within_30000ms",
                "eligible": eligible,
                "evaluable": eligible,
                "censored_or_unavailable": 0,
                "positives": int(filled.sum()),
                "rate": float(filled.mean()),
            }
        )
        through = frame[f"{side}_fill_via_trade_through"].to_numpy(dtype="float64")
        rows.append(
            {
                "side": side,
                "statistic": "fill_via_trade_through_given_fill",
                "eligible": eligible,
                "evaluable": int(filled.sum()),
                "censored_or_unavailable": int(eligible - filled.sum()),
                "positives": int(np.nansum(through)),
                "rate": float(np.nanmean(through[filled])),
            }
        )
        rows.append(
            {
                "side": side,
                "statistic": "fill_at_quote_given_fill",
                "eligible": eligible,
                "evaluable": int(filled.sum()),
                "censored_or_unavailable": int(eligible - filled.sum()),
                "positives": int(filled.sum() - np.nansum(through)),
                "rate": float(1.0 - np.nanmean(through[filled])),
            }
        )
    pd.DataFrame(rows).to_csv(
        spec.REPORT_DIR / "fill_population.csv", index=False, float_format=FLOAT_FORMAT
    )

    mechanism_rows = []
    for side in ("bid", "ask"):
        filled = np.isfinite(frame[f"{side}_time_to_fill_ms"].to_numpy())
        through = frame[f"{side}_fill_via_trade_through"].to_numpy(dtype="float64")
        for mechanism, mask in (
            ("all_fills", filled),
            ("fill_via_trade_through", filled & (through == 1.0)),
            ("fill_at_quote", filled & (through == 0.0)),
        ):
            record = {
                "side": side,
                "mechanism": mechanism,
                "fills": int(mask.sum()),
                "share_of_fills": float(mask.sum() / max(int(filled.sum()), 1)),
                "median_time_to_fill_ms": float(
                    np.nanmedian(frame[f"{side}_time_to_fill_ms"].to_numpy()[mask])
                ),
            }
            for horizon in spec.POSTFILL_HORIZONS_MS:
                markout = frame[f"{side}_postfill_markout_{horizon}ms_ticks"].to_numpy()[mask]
                observed = np.isfinite(markout)
                record[f"observed_{horizon}ms"] = int(observed.sum())
                record[f"mean_markout_{horizon}ms_ticks"] = float(np.nanmean(markout))
                record[f"median_markout_{horizon}ms_ticks"] = float(np.nanmedian(markout))
                record[f"frac_favourable_{horizon}ms"] = float(
                    np.nanmean(markout[observed] > 0)
                )
            mechanism_rows.append(record)
    pd.DataFrame(mechanism_rows).to_csv(
        spec.REPORT_DIR / "mechanism_markout_summary.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    print("descriptive population and mechanism artifacts written")


def run_conditional() -> None:
    """Descriptive conditional behaviour of the strongest raw signals. In-sample by design."""
    frame = data.load_model_frame(
        columns=[
            "timestamp_ns",
            "obi_l1",
            "obi_l10",
            "weighted_obi_l10",
            "trade_imbalance_1000ms",
            "depth_flow_pressure_l10_1000ms",
            "net_depth_flow_l1_500ms",
            "spread_ticks",
            "time_since_mid_change_ms",
            "backward_mid_abs_change_ticks_1000ms",
            "y_direction",
            "y_move_500ms",
            "y_move_1000ms",
            "y_bid_fill_1000ms",
            "y_bid_through_given_fill",
            "y_bid_good_fill",
            f"y_bid_markout_{spec.PRIMARY_MARKOUT_MS}ms",
        ]
    )
    outcomes = {
        "p_next_move_up": "y_direction",
        "p_move_within_500ms": "y_move_500ms",
        "p_move_within_1000ms": "y_move_1000ms",
        "p_bid_fill_1000ms": "y_bid_fill_1000ms",
        "p_bid_trade_through_given_fill": "y_bid_through_given_fill",
        "mean_bid_markout_ticks": f"y_bid_markout_{spec.PRIMARY_MARKOUT_MS}ms",
    }
    tables = [
        bucket_table(frame, signal, outcomes, 10)
        for signal in (
            "obi_l1",
            "obi_l10",
            "weighted_obi_l10",
            "trade_imbalance_1000ms",
            "depth_flow_pressure_l10_1000ms",
            "net_depth_flow_l1_500ms",
            "time_since_mid_change_ms",
            "backward_mid_abs_change_ticks_1000ms",
        )
    ]
    pd.concat([t for t in tables if not t.empty], ignore_index=True).to_csv(
        spec.REPORT_DIR / "conditional_behaviour.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("conditional behaviour written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "frames",
            "timing",
            "descriptive",
            "models",
            "joint",
            "conditional",
            "robustness",
            "all",
        ),
    )
    arguments = parser.parse_args()
    if arguments.stage in ("frames", "all"):
        data.build_model_frames()
    if arguments.stage in ("timing", "all"):
        run_timing()
    if arguments.stage in ("descriptive", "all"):
        run_descriptive()
    if arguments.stage in ("models", "all"):
        run_models()
    if arguments.stage in ("joint", "all"):
        run_joint()
    if arguments.stage in ("conditional", "all"):
        run_conditional()
    if arguments.stage in ("robustness", "all"):
        run_robustness()


if __name__ == "__main__":
    main()
