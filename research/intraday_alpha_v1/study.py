#!/usr/bin/env python3
"""Small, causal Binance 5m directional-alpha feasibility study."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import time
import zipfile

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).parent
REST = "https://fapi.binance.com"
VISION = "https://data.binance.vision/data/futures/um/monthly"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
START = pd.Timestamp("2024-01-01", tz="UTC")
END = pd.Timestamp("2026-01-01", tz="UTC")
FINAL_TEST = pd.Timestamp("2025-07-01", tz="UTC")
FEE_BP = 4.0
SLIPPAGE_BP = 1.5
BAR_MINUTES = 5
PER_YEAR = 365.25 * 24 * 60 / BAR_MINUTES

SERIES = {
    "price": ("klines", "5m"),
    "mark": ("markPriceKlines", "5m"),
    "index": ("indexPriceKlines", "5m"),
    "premium": ("premiumIndexKlines", "5m"),
    "metrics": ("metrics", None),
}


def fetch(url, attempts=3):
    from urllib.request import Request, urlopen
    last = None
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": "crypto-hft-like-bot/intraday-alpha-v1"}), timeout=45) as r:
                return r.read()
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


def month_urls(symbol, month):
    ym = month.strftime("%Y-%m")
    return {name: (f"{VISION}/{folder}/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip" if interval else f"{VISION}/{folder}/{symbol}/{symbol}-{folder}-{ym}.zip") for name, (folder, interval) in SERIES.items()}


def read_archive(blob, kind):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(name) as fh:
            raw = pd.read_csv(fh, header=None)
    if raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.iat[0, 0], (int, float, np.integer, np.floating)):
        raw = pd.read_csv(io.BytesIO(blob), compression="zip")
    if kind == "metrics":
        cols = {str(c).lower().replace(" ", "_"): c for c in raw.columns}
        ts_col = next((cols[x] for x in ("timestamp", "create_time", "time") if x in cols), raw.columns[0])
        oi_col = next((cols[x] for x in ("sum_open_interest", "sumopeninterest") if x in cols), None)
        value_col = next((cols[x] for x in ("sum_open_interest_value", "sumopeninterestvalue") if x in cols), None)
        ts = pd.to_numeric(raw[ts_col], errors="coerce")
        if ts.isna().all():
            parsed = pd.to_datetime(raw[ts_col], errors="coerce", utc=True).astype("int64")
            divisor = 1_000_000 if parsed.abs().max() > 10**17 else 1_000
            ts = parsed // divisor
        return pd.DataFrame({"ts": ts, "oi": pd.to_numeric(raw[oi_col], errors="coerce") if oi_col else np.nan, "oi_value": pd.to_numeric(raw[value_col], errors="coerce") if value_col else np.nan})
    return pd.DataFrame({"ts": pd.to_numeric(raw.iloc[:, 0], errors="coerce"), "open": pd.to_numeric(raw.iloc[:, 1], errors="coerce"), "high": pd.to_numeric(raw.iloc[:, 2], errors="coerce"), "low": pd.to_numeric(raw.iloc[:, 3], errors="coerce"), "close": pd.to_numeric(raw.iloc[:, 4], errors="coerce"), "volume": pd.to_numeric(raw.iloc[:, 5], errors="coerce")})


def load_symbol(symbol):
    months = pd.date_range(START, END - pd.Timedelta(days=1), freq="MS", tz="UTC")
    tasks = [(month, name, url) for month in months for name, url in month_urls(symbol, month).items() if name != "metrics"]
    days = pd.date_range(START, END - pd.Timedelta(days=1), freq="D", tz="UTC")
    tasks.extend((day, "metrics", f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day:%Y-%m-%d}.zip") for day in days)
    frames = {name: [] for name in SERIES}
    missing = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        future_map = {pool.submit(fetch, url): (month, name, url) for month, name, url in tasks}
        for future in as_completed(future_map):
            month, name, url = future_map[future]
            try:
                frame = read_archive(future.result(), name)
                if not frame.empty:
                    frames[name].append(frame)
            except Exception as exc:
                missing.append({"name": name, "month": str(month.date()), "error": str(exc)[:200]})
    base = pd.concat(frames["price"], ignore_index=True).drop_duplicates("ts")
    base["timestamp"] = pd.to_datetime(base.pop("ts"), unit="ms", utc=True)
    base = base.set_index("timestamp").sort_index()
    for name in ("mark", "index", "premium"):
        other = pd.concat(frames[name], ignore_index=True).drop_duplicates("ts")
        other["timestamp"] = pd.to_datetime(other.pop("ts"), unit="ms", utc=True)
        other = other.set_index("timestamp").sort_index().rename(columns={"close": name})[[name]]
        base = base.join(other, how="left")
    metrics = pd.concat(frames["metrics"], ignore_index=True).drop_duplicates("ts")
    metrics["timestamp"] = pd.to_datetime(metrics.pop("ts"), unit="ms", utc=True)
    base = base.join(metrics.set_index("timestamp").sort_index()[["oi", "oi_value"]], how="left")
    base = base.loc[(base.index >= START) & (base.index < END)]
    return base, missing


def funding(symbol):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    rows, cursor = [], int(START.timestamp() * 1000)
    end_ms = int(END.timestamp() * 1000)
    while cursor < end_ms:
        url = REST + "/fapi/v1/fundingRate?" + urlencode({"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        with urlopen(Request(url, headers={"User-Agent": "crypto-hft-like-bot/intraday-alpha-v1"}), timeout=30) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        rows.extend(batch)
        nxt = max(int(x["fundingTime"]) for x in batch) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1000:
            break
    f = pd.DataFrame(rows).drop_duplicates("fundingTime")
    if f.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    f["timestamp"] = pd.to_datetime(f["fundingTime"], unit="ms", utc=True)
    f["funding"] = pd.to_numeric(f["fundingRate"], errors="coerce")
    f = f.set_index("timestamp")[['funding']].sort_index()
    f.attrs["times_ns"] = f.index.view("int64")
    f.attrs["values"] = f["funding"].to_numpy()
    return f


def make_features(df, fund):
    df = df.copy()
    df["basis"] = df["mark"] / df["index"] - 1
    df["logret"] = np.log(df["close"]).diff()
    for h in (1, 3, 6, 12, 48, 288):
        df[f"ret_{h}"] = df["close"].pct_change(h)
        df[f"oi_chg_{h}"] = df["oi"].pct_change(h)
    df["rv_1h"] = df["logret"].rolling(12).std() * np.sqrt(12)
    df["volume_log"] = np.log1p(df["volume"])
    df["volume_shock"] = df["volume"] / df["volume"].rolling(48).median() - 1
    df["oi_log"] = np.log(df["oi"].replace(0, np.nan))
    df["funding"] = pd.merge_asof(pd.DataFrame(index=df.index), fund, left_index=True, right_index=True, direction="backward")["funding"] if not fund.empty else np.nan
    for h, label in ((1, "5m"), (3, "15m"), (6, "30m"), (12, "60m")):
        entry = df["open"].shift(-1)
        exit_ = df["close"].shift(-(h + 1))
        df[f"target_{label}"] = np.log(exit_ / entry)
    return df


GROUPS = {
    "price_volume": ["ret_1", "ret_3", "ret_6", "ret_12", "ret_48", "ret_288", "rv_1h", "volume_log", "volume_shock"],
    "plus_basis": ["ret_1", "ret_3", "ret_6", "ret_12", "ret_48", "ret_288", "rv_1h", "volume_log", "volume_shock", "basis", "premium"],
    "plus_oi": ["ret_1", "ret_3", "ret_6", "ret_12", "ret_48", "ret_288", "rv_1h", "volume_log", "volume_shock", "basis", "premium", "oi_log", "oi_chg_1", "oi_chg_3", "oi_chg_12", "oi_chg_48"],
    "full": ["ret_1", "ret_3", "ret_6", "ret_12", "ret_48", "ret_288", "rv_1h", "volume_log", "volume_shock", "basis", "premium", "oi_log", "oi_chg_1", "oi_chg_3", "oi_chg_12", "oi_chg_48", "funding"],
}
HORIZONS = {"5m": 1, "15m": 3, "30m": 6, "60m": 12}


def splits(h):
    out = []
    for test_start in pd.date_range("2024-10-01", "2025-07-01", freq="3MS", tz="UTC"):
        val_start = test_start - pd.Timedelta(days=90)
        train_start = val_start - pd.Timedelta(days=365)
        out.append((train_start, val_start, test_start, min(test_start + pd.Timedelta(days=90), END), test_start >= FINAL_TEST))
    return out


def net_trade(gross, side, entry_ts, exit_ts, fund):
    funding_bp = 0.0
    if not fund.empty:
        times = fund.attrs.get("times_ns", fund.index.view("int64"))
        values = fund.attrs.get("values", fund["funding"].to_numpy())
        lo, hi = np.searchsorted(times, entry_ts.value, "left"), np.searchsorted(times, exit_ts.value, "right")
        funding_bp = float(side * values[lo:hi].sum() * 10000)
    base_cost_bp = 2 * (FEE_BP + SLIPPAGE_BP)
    return gross - (base_cost_bp / 10000) - funding_bp / 10000, base_cost_bp + abs(funding_bp)


def choose_threshold(scores, returns, ts, fund, h):
    # ponytail: fixed zero threshold; no validation grid is worth the runtime here.
    return 0.0
    finite = np.isfinite(scores)
    scores, returns = scores[finite], returns[finite]
    candidates = [0.0] if len(scores) == 0 else sorted(set([0.0, *np.quantile(np.abs(scores), [0.25, 0.5, 0.75]).tolist()]))
    best = (0.0, -np.inf)
    for threshold in candidates:
        pos = np.where(scores > threshold, 1, np.where(scores < -threshold, -1, 0))
        valid = pos != 0
        if not valid.any():
            continue
        net = []
        for side, gross, t in zip(pos[valid], returns[valid], ts[valid]):
            value, _ = net_trade(gross * side, side, t + pd.Timedelta(minutes=5), t + pd.Timedelta(minutes=5 * (h + 1)), fund)
            net.append(value)
        score = np.mean(net)
        if score > best[1]:
            best = (threshold, score)
    return best[0]


def metric_row(symbol, horizon, group, model, records, final_holdout, h):
    r = pd.DataFrame(records)
    if r.empty:
        return {"symbol": symbol, "horizon": horizon, "features": group, "model": model, "final_holdout": final_holdout, "observations": 0}
    score = r["score"].to_numpy()
    y = r["target"].to_numpy()
    pred = np.sign(score)
    auc = roc_auc_score((y > 0).astype(int), score) if len(np.unique(y > 0)) == 2 else np.nan
    ic = np.corrcoef(score, y)[0, 1] if np.std(score) and np.std(y) else np.nan
    # Conservative non-overlapping trade tape.
    tape = r.iloc[::h].copy()
    tape = tape[tape.score.abs() >= tape.threshold]
    net = tape.net.to_numpy()
    gross = tape.gross.to_numpy()
    cost = tape.cost_bp.to_numpy()
    ann = PER_YEAR / h
    wealth = np.cumprod(1 + net) if len(net) else np.array([])
    peak = np.maximum.accumulate(wealth) if len(wealth) else np.array([])
    dd = np.min(wealth / peak - 1) if len(wealth) else np.nan
    by_year = {}
    if len(tape):
        for year, x in tape.groupby(tape.timestamp.dt.year):
            by_year[str(year)] = {"trades": len(x), "net_edge_bp": float(x.net.mean() * 10000), "net_positive": bool(x.net.mean() > 0)}
    deciles = []
    if len(r) >= 20:
        r = r.copy()
        r["decile"] = pd.qcut(r.score.rank(method="first"), 10, labels=False) + 1
        deciles = [{"decile": int(k), "observations": len(x), "forward_return_bp": float(x.target.mean() * 10000)} for k, x in r.groupby("decile")]
    top_n = max(1, int(len(tape) * 0.05))
    top_share = float(tape.nlargest(top_n, "net").net.sum() / tape.net.sum()) if len(tape) and tape.net.sum() else np.nan
    return {"symbol": symbol, "horizon": horizon, "features": group, "model": model, "final_holdout": final_holdout, "observations": len(r), "directional_accuracy": float(np.mean(pred == np.sign(y))), "auc": float(auc), "ic": float(ic), "gross_sharpe": float(np.mean(gross) / np.std(gross, ddof=1) * np.sqrt(ann)) if len(gross) > 1 and np.std(gross, ddof=1) else np.nan, "net_sharpe": float(np.mean(net) / np.std(net, ddof=1) * np.sqrt(ann)) if len(net) > 1 and np.std(net, ddof=1) else np.nan, "annualized_return": float(np.mean(net) * ann), "max_drawdown": float(dd), "turnover_roundtrips_per_year": float(len(tape) / max((tape.timestamp.iloc[-1] - tape.timestamp.iloc[0]).total_seconds() / (365.25 * 86400), 1 / 365.25)) if len(tape) else 0.0, "trade_count": len(tape), "gross_edge_bp": float(np.mean(gross) * 10000) if len(gross) else np.nan, "all_in_cost_bp": float(np.mean(cost)) if len(cost) else np.nan, "net_edge_bp": float(np.mean(net) * 10000) if len(net) else np.nan, "long_net_edge_bp": float(tape.loc[tape.side > 0, "net"].mean() * 10000) if (tape.side > 0).any() else np.nan, "short_net_edge_bp": float(tape.loc[tape.side < 0, "net"].mean() * 10000) if (tape.side < 0).any() else np.nan, "top_5pct_net_pnl_share": top_share, "year_stability": by_year, "deciles": deciles}


def fit_score(model, x_train, y_train, x_apply):
    if model == "unconditional":
        return np.full(len(x_apply), np.mean(y_train))
    if model == "momentum":
        return x_apply[:, 3]  # ret_12 is the fourth feature in every group containing it.
    estimator = LogisticRegression(max_iter=300, C=1.0, solver="liblinear") if model == "logistic" else Ridge(alpha=1.0)
    estimator.fit(x_train, y_train if model == "ridge" else (y_train > 0).astype(int))
    return estimator.decision_function(x_apply) if model == "logistic" else estimator.predict(x_apply)


def run_symbol(symbol, raw):
    fund = funding(symbol)
    data = make_features(raw, fund)
    results, decile_rows = [], []
    for horizon, h in HORIZONS.items():
        target = f"target_{horizon}"
        for group, cols in GROUPS.items():
            required = cols + [target]
            sample = data.replace([np.inf, -np.inf], np.nan).dropna(subset=required).copy()
            models = ("unconditional", "momentum", "logistic", "ridge") if group == "full" else ("unconditional", "momentum", "ridge")
            for model in models:
                records = []
                for train_start, val_start, test_start, test_end, final in splits(h):
                    train_end = val_start - pd.Timedelta(minutes=5 * h)
                    val_end = test_start - pd.Timedelta(minutes=5 * h)
                    train = sample.loc[(sample.index >= train_start) & (sample.index < train_end)]
                    val = sample.loc[(sample.index >= val_start) & (sample.index < val_end)]
                    test = sample.loc[(sample.index >= test_start) & (sample.index < test_end)]
                    test = test.iloc[::h]
                    if len(train) < 500 or len(val) < 100 or len(test) < 100:
                        continue
                    scaler_model = None
                    train_fit = train.iloc[::12]
                    x_train = train_fit[cols].to_numpy(float)
                    x_val = val[cols].to_numpy(float)
                    x_test = test[cols].to_numpy(float)
                    if model in ("logistic", "ridge"):
                        scaler_model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=300, C=1.0, solver="liblinear") if model == "logistic" else Ridge(alpha=1.0))
                        scaler_model.fit(x_train, (train_fit[target] > 0).astype(int) if model == "logistic" else train_fit[target])
                        val_score = scaler_model.decision_function(x_val) if model == "logistic" else scaler_model.predict(x_val)
                        test_score = scaler_model.decision_function(x_test) if model == "logistic" else scaler_model.predict(x_test)
                    else:
                        val_score = val["ret_12"].to_numpy() if model == "momentum" else np.full(len(val), train[target].mean())
                        test_score = test["ret_12"].to_numpy() if model == "momentum" else np.full(len(test), train[target].mean())
                    threshold = 0.0 if model in ("unconditional", "momentum") else choose_threshold(val_score, val[target].to_numpy(), val.index.to_numpy(), fund, h)
                    positions = np.where(test_score > threshold, 1, np.where(test_score < -threshold, -1, 0))
                    for ts, score, side, target_value in zip(test.index, test_score, positions, test[target].to_numpy()):
                        if side:
                            gross = side * target_value
                            net, cost_bp = net_trade(gross, side, ts + pd.Timedelta(minutes=5), ts + pd.Timedelta(minutes=5 * (h + 1)), fund)
                        else:
                            gross, net, cost_bp = 0.0, 0.0, 0.0
                        records.append({"timestamp": ts, "score": score, "side": side, "target": target_value, "gross": gross, "net": net, "cost_bp": cost_bp, "threshold": threshold})
                frame = pd.DataFrame(records)
                row = metric_row(symbol, horizon, group, model, records, False, h)
                results.append(row)
                # Final holdout is also reported separately for economic gating.
                if records:
                    final_records = frame[frame.timestamp >= FINAL_TEST].to_dict("records")
                    row_final = metric_row(symbol, horizon, group, model, final_records, True, h)
                    results.append(row_final)
    return results


def main():
    all_results, manifests = [], {}
    for symbol in SYMBOLS:
        raw, missing = load_symbol(symbol)
        manifests[symbol] = {"rows": len(raw), "start": str(raw.index.min()), "end": str(raw.index.max()), "missing_archives": missing[:30]}
        all_results.extend(run_symbol(symbol, raw))
    (ROOT / "data_manifest.json").write_text(json.dumps({"window": [str(START), str(END)], "symbols": manifests, "fee_bp_per_side": FEE_BP, "slippage_bp_per_side": SLIPPAGE_BP, "aggTrades": "skipped in V1; optional bulk-heavy input"}, indent=2) + "\n")
    (ROOT / "metrics.json").write_text(json.dumps(all_results, indent=2, default=str) + "\n")
    pd.DataFrame(all_results).drop(columns=["year_stability", "deciles"], errors="ignore").to_csv(ROOT / "metrics.csv", index=False)
    print(json.dumps({"rows": len(all_results), "manifest": manifests}, indent=2))


if __name__ == "__main__":
    main()
