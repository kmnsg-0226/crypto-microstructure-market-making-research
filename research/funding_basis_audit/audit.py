#!/usr/bin/env python3
"""Multi-year Binance spot/perpetual funding and basis feasibility audit."""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile


OUT = Path(__file__).parent
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
START_MONTH = "2021-01"
END_MONTH = "2026-05"
SPOT = "https://data.binance.vision/data/spot/monthly/klines"
PERP = "https://data.binance.vision/data/futures/um/monthly/klines"
FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
HORIZONS = {"8h": 8, "24h": 24, "3d": 72, "7d": 168, "30d": 720}
COSTS = {"optimistic": 2.0, "realistic_retail_api": 8.0, "conservative": 20.0}
MIN_YEARS_FOR_VERDICT = 2
MIN_30D_TRADES = 30


def timestamp(value):
    """Return exchange time in milliseconds; accept ms and microseconds."""
    try:
        value = int(float(value))
        return value // 1000 if value > 100_000_000_000_000 else value
    except (TypeError, ValueError):
        return None


def month_range(start, end):
    year, month = map(int, start.split("-"))
    stop_year, stop_month = map(int, end.split("-"))
    while (year, month) <= (stop_year, stop_month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year, month = year + 1, 1


def read_klines(path):
    rows = {}
    dropped = 0
    with zipfile.ZipFile(path) as archive, archive.open(archive.namelist()[0]) as raw:
        for row in csv.reader(line.decode("utf-8") for line in raw):
            if not row:
                continue
            opened = timestamp(row[0])
            if opened is None:
                dropped += 1
                continue
            try:
                close = float(row[4])
            except (IndexError, TypeError, ValueError):
                dropped += 1
                continue
            rows[opened] = close
    return rows, dropped


def download(url, path):
    request = Request(url, headers={"User-Agent": "funding-basis-audit/2.0"})
    try:
        with urlopen(request, timeout=90) as response, path.open("wb") as output:
            output.write(response.read())
    except HTTPError as error:
        if error.code == 404:
            return False
        raise
    return True


def load_market(symbol, base, directory):
    prices = {}
    archive_count = 0
    dropped = 0
    missing = []
    for month in month_range(START_MONTH, END_MONTH):
        url = f"{base}/{symbol}/1h/{symbol}-1h-{month}.zip"
        path = directory / f"{symbol}_{base.split('/')[4]}_{month}.zip"
        if not download(url, path):
            missing.append(month)
            continue
        rows, invalid = read_klines(path)
        prices.update(rows)
        dropped += invalid
        archive_count += 1
        path.unlink()
    return prices, {"archives": archive_count, "missing_months": missing, "dropped_rows": dropped}


def funding_rows(symbol, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000}
        request = Request(FUNDING + "?" + urlencode(params), headers={"User-Agent": "funding-basis-audit/2.0"})
        with urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not payload:
            break
        batch = [(int(item["fundingTime"]), float(item["fundingRate"])) for item in payload]
        rows.extend((time, rate) for time, rate in batch if start_ms <= time < end_ms)
        newest = max(time for time, _ in batch)
        if newest < cursor:
            raise RuntimeError(f"funding pagination regressed for {symbol}")
        cursor = newest + 1
        if len(batch) < 1000:
            break
    return sorted(set(rows))


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def year(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).year


def basis_bps(spot, perp, opened):
    return (perp[opened] / spot[opened] - 1.0) * 10000


def trade_rows(spot, perp, funding, horizon_hours):
    horizon_ms = horizon_hours * 3_600_000
    common = sorted(set(spot) & set(perp))
    index = set(common)
    trades = []
    for opened in common[::horizon_hours]:
        closed = opened + horizon_ms
        if closed not in index or not spot[opened] or not spot[closed]:
            continue
        entry_basis = basis_bps(spot, perp, opened)
        exit_basis = basis_bps(spot, perp, closed)
        pair_return = spot[closed] / spot[opened] - perp[closed] / perp[opened]
        paid_funding = sum(rate for time, rate in funding if opened < time <= closed)
        path = [basis_bps(spot, perp, time) for time in common if opened <= time <= closed]
        trades.append({
            "opened": opened,
            "year": year(opened),
            "funding_bps": -paid_funding * 10000,
            "basis_bps": pair_return * 10000,
            "gross_bps": (-paid_funding + pair_return) * 10000,
            "basis_widening_bps": max(path) - entry_basis,
        })
    return trades


def summarize(trades, leg_cost_bps, horizon_hours):
    pair_cost_bps = leg_cost_bps * 2
    equity = 1.0
    peak = equity
    max_drawdown = 0.0
    for trade in trades:
        net = (trade["gross_bps"] - pair_cost_bps) / 10000 / 2
        equity *= 1 + net
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    net_bps = [trade["gross_bps"] - pair_cost_bps for trade in trades]
    scale = 365 * 24 / horizon_hours / 2
    return {
        "trade_count": len(trades),
        "funding_pnl_bps": statistics.fmean(t["funding_bps"] for t in trades) if trades else None,
        "basis_pnl_bps": statistics.fmean(t["basis_bps"] for t in trades) if trades else None,
        "gross_bps": statistics.fmean(t["gross_bps"] for t in trades) if trades else None,
        "net_bps": statistics.fmean(net_bps) if net_bps else None,
        "annualized_return_on_2N": statistics.fmean(net_bps) / 10000 * scale if net_bps else None,
        "max_drawdown": max_drawdown,
        "worst_basis_widening_bps": max((t["basis_widening_bps"] for t in trades), default=None),
        "pair_round_trip_cost_bps": pair_cost_bps,
    }


def yearly(trades, leg_cost_bps):
    grouped = {}
    for trade in trades:
        grouped.setdefault(str(trade["year"]), []).append(trade)
    output = {}
    for label, rows in sorted(grouped.items()):
        years = max((rows[-1]["opened"] - rows[0]["opened"]) / (365.25 * 24 * 3_600_000), 1 / 365.25)
        hours = max(1, round(years * 365.25 * 24 / max(1, len(rows) - 1)))
        summary = summarize(rows, leg_cost_bps, hours)
        output[label] = {"trade_count": summary["trade_count"], "mean_gross_bps": statistics.fmean(x["gross_bps"] for x in rows), "mean_net_bps": summary["net_bps"], "positive_net_trade_fraction": sum(x["gross_bps"] - leg_cost_bps * 2 > 0 for x in rows) / len(rows)}
    return output


def reversal_frequency(funding):
    changes = sum((a[1] > 0) != (b[1] > 0) for a, b in zip(funding, funding[1:]) if a[1] and b[1])
    pairs = sum(bool(a[1] and b[1]) for a, b in zip(funding, funding[1:]))
    return {"sign_changes": changes, "adjacent_pairs": pairs, "frequency": changes / pairs if pairs else None}


def main():
    result = {"requested_period": {"start_month": START_MONTH, "end_month": END_MONTH}, "symbols": SYMBOLS, "horizons_hours": HORIZONS, "costs": COSTS, "coverage_by_symbol_year": {}, "symbols_result": {}}
    with tempfile.TemporaryDirectory(prefix="funding_basis_audit_") as temporary:
        directory = Path(temporary)
        for symbol in SYMBOLS:
            spot, spot_meta = load_market(symbol, SPOT, directory)
            perp, perp_meta = load_market(symbol, PERP, directory)
            common = sorted(set(spot) & set(perp))
            if not common or common[-1] - common[0] < 30 * 24 * 3_600_000:
                raise RuntimeError(f"30d holding analysis lacks sufficient aligned history for {symbol}")
            funding = funding_rows(symbol, common[0], common[-1] + 3_600_000)
            coverage = {}
            for opened in common:
                bucket = coverage.setdefault(str(year(opened)), {"first": opened, "last": opened, "rows": 0})
                bucket["first"] = min(bucket["first"], opened)
                bucket["last"] = max(bucket["last"], opened)
                bucket["rows"] += 1
            result["coverage_by_symbol_year"][symbol] = {label: {**value, "first": iso(value["first"]), "last": iso(value["last"])} for label, value in coverage.items()}
            analyses = {}
            for label, hours in HORIZONS.items():
                trades = trade_rows(spot, perp, funding, hours)
                if label == "30d" and len(trades) < MIN_30D_TRADES:
                    raise RuntimeError(f"30d holding analysis lacks {MIN_30D_TRADES} independent trades for {symbol}; got {len(trades)}")
                analyses[label] = {"funding_reversal_frequency": reversal_frequency(funding), "scenarios": {name: {**summarize(trades, cost, hours), "year_by_year": yearly(trades, cost)} for name, cost in COSTS.items()}}
            result["symbols_result"][symbol] = {
                "spot": spot_meta,
                "perpetual": perp_meta,
                "aligned_rows": len(common),
                "aligned_first": iso(common[0]),
                "aligned_last": iso(common[-1]),
                "aligned_years": (common[-1] - common[0]) / (365.25 * 24 * 3_600_000),
                "funding_event_count": len(funding),
                "funding_first": iso(funding[0][0]) if funding else None,
                "funding_last": iso(funding[-1][0]) if funding else None,
                "analyses": analyses,
            }
    eligible = all(item["aligned_years"] >= MIN_YEARS_FOR_VERDICT and item["analyses"]["30d"]["scenarios"]["realistic_retail_api"]["trade_count"] >= MIN_30D_TRADES for item in result["symbols_result"].values())
    realistic = [item["analyses"]["30d"]["scenarios"]["realistic_retail_api"] for item in result["symbols_result"].values()]
    if not eligible:
        result["verdict"] = "INSUFFICIENT_HISTORY"
    elif all(item["net_bps"] > 0 for item in realistic):
        result["verdict"] = "A"
    elif any(item["net_bps"] > 0 for item in realistic):
        result["verdict"] = "B"
    else:
        result["verdict"] = "C"
    (OUT / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = ["# Binance funding/basis feasibility audit", "", f"Requested monthly 1h Vision window: {START_MONTH} through {END_MONTH}.", "", "## Coverage by symbol and year", "", "| Symbol | Year | Aligned 1h rows | First | Last |", "|---|---:|---:|---|---|"]
    for symbol in SYMBOLS:
        for label, item in result["coverage_by_symbol_year"][symbol].items():
            lines.append(f"| {symbol} | {label} | {item['rows']} | {item['first']} | {item['last']} |")
    lines += ["", "All archive timestamps were coerced numeric, invalid/header rows dropped, and microseconds normalized to milliseconds before alignment.", "", "## Funding reversals", "", "| Symbol | Funding events | Sign changes | Adjacent pairs | Reversal frequency |", "|---|---:|---:|---:|---:|"]
    for symbol in SYMBOLS:
        reversal = result["symbols_result"][symbol]["analyses"]["8h"]["funding_reversal_frequency"]
        lines.append(f"| {symbol} | {result['symbols_result'][symbol]['funding_event_count']} | {reversal['sign_changes']} | {reversal['adjacent_pairs']} | {reversal['frequency']:.2%} |")
    lines += ["", "## Results", "", "Costs are round-trip bps per leg: optimistic 2, realistic retail API 8, conservative 20. Basis PnL is long spot/short perpetual; funding PnL is the short-perpetual funding cash flow. Returns are on 2N capital.", "", "| Symbol | Horizon | Scenario | Trades | Funding bp | Basis bp | Gross bp | Net bp | Ann. return on 2N | Max DD | Worst widening bp |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for symbol in SYMBOLS:
        for horizon in HORIZONS:
            for scenario in COSTS:
                item = result["symbols_result"][symbol]["analyses"][horizon]["scenarios"][scenario]
                lines.append(f"| {symbol} | {horizon} | {scenario} | {item['trade_count']} | {item['funding_pnl_bps']:.3f} | {item['basis_pnl_bps']:.3f} | {item['gross_bps']:.3f} | {item['net_bps']:.3f} | {item['annualized_return_on_2N']:.2%} | {item['max_drawdown']:.2%} | {item['worst_basis_widening_bps']:.3f} |")
    lines += ["", "## Year-by-year stability (realistic retail API costs)", "", "| Symbol | Horizon | Year | Trades | Mean gross bp | Mean net bp | Positive net trade fraction |", "|---|---|---:|---:|---:|---:|---:|"]
    for symbol in SYMBOLS:
        for horizon in HORIZONS:
            years = result["symbols_result"][symbol]["analyses"][horizon]["scenarios"]["realistic_retail_api"]["year_by_year"]
            for label, item in years.items():
                lines.append(f"| {symbol} | {horizon} | {label} | {item['trade_count']} | {item['mean_gross_bps']:.3f} | {item['mean_net_bps']:.3f} | {item['positive_net_trade_fraction']:.2%} |")
    lines += ["", f"## Verdict: {result['verdict']}", "", "A requires at least two years of aligned history, at least 30 independent 30d trades per symbol, and positive realistic net 30d carry for every symbol. B is regime-dependent/marginal; C is insufficient economics."]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"verdict": result["verdict"], "coverage": result["coverage_by_symbol_year"], "aligned_rows": {s: v["aligned_rows"] for s, v in result["symbols_result"].items()}}, indent=2))


if __name__ == "__main__":
    main()
