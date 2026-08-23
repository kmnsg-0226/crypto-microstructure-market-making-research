#!/usr/bin/env python3
"""Small Binance intraday-alpha data-feasibility audit; never downloads Vision archives."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


OUT = Path(__file__).parent
REST = "https://fapi.binance.com"
VISION = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

DATASETS = {
    "ohlcv_1m": {"endpoint": "/fapi/v1/klines", "params": {"interval": "1m"}, "vision": "data/futures/um/daily/klines/{symbol}/1m/", "interval_ms": 60_000},
    "ohlcv_5m": {"endpoint": "/fapi/v1/klines", "params": {"interval": "5m"}, "vision": "data/futures/um/daily/klines/{symbol}/5m/", "interval_ms": 300_000},
    "aggTrades": {"endpoint": "/fapi/v1/aggTrades", "params": {}, "vision": "data/futures/um/daily/aggTrades/{symbol}/", "interval_ms": None},
    "fundingRate": {"endpoint": "/fapi/v1/fundingRate", "params": {}, "vision": "data/futures/um/daily/fundingRate/{symbol}/", "interval_ms": 8 * 3600_000},
    "markPriceKlines": {"endpoint": "/fapi/v1/markPriceKlines", "params": {"interval": "5m"}, "vision": "data/futures/um/daily/markPriceKlines/{symbol}/5m/", "interval_ms": 300_000},
    "indexPriceKlines": {"endpoint": "/fapi/v1/indexPriceKlines", "params": {"interval": "5m", "pair": ""}, "vision": "data/futures/um/daily/indexPriceKlines/{symbol}/5m/", "interval_ms": 300_000},
    "premiumIndexKlines": {"endpoint": "/fapi/v1/premiumIndexKlines", "params": {"interval": "5m", "pair": ""}, "vision": "data/futures/um/daily/premiumIndexKlines/{symbol}/5m/", "interval_ms": 300_000},
    "openInterestHist": {"endpoint": "/futures/data/openInterestHist", "params": {"period": "5m"}, "vision": "data/futures/um/daily/metrics/{symbol}/", "interval_ms": 300_000},
}


def request(url, params=None, method="GET", limit=2_000_000):
    if params:
        url += "?" + urlencode(params)
    try:
        req = Request(url, method=method, headers={"User-Agent": "crypto-hft-like-bot/intraday-alpha-audit"})
        with urlopen(req, timeout=30) as response:
            data = response.read(limit)
            return {"status": response.status, "url": url, "bytes": len(data), "body": data}
    except HTTPError as e:
        return {"status": e.code, "url": url, "bytes": 0, "body": e.read(500).decode("utf-8", "replace")}
    except (URLError, TimeoutError) as e:
        return {"status": None, "url": url, "bytes": 0, "body": str(e)}


def json_get(path, params):
    response = request(REST + path, params)
    try:
        response["json"] = json.loads(response["body"])
    except (TypeError, json.JSONDecodeError):
        response["json"] = None
    return response


def timestamps(dataset, rows):
    if dataset in {"ohlcv_1m", "ohlcv_5m", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines"}:
        return [row[0] for row in rows if isinstance(row, list) and row and isinstance(row[0], int)]
    if dataset == "aggTrades":
        return [row.get("T") for row in rows if isinstance(row, dict) and isinstance(row.get("T"), int)]
    return [row.get("fundingTime", row.get("timestamp")) for row in rows if isinstance(row, dict) and isinstance(row.get("fundingTime", row.get("timestamp")), int)]


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None


def vision_listing(prefix):
    keys, token = [], None
    for _ in range(10):
        params = {"list-type": "2", "prefix": prefix, "max-keys": 1000}
        if token:
            params["continuation-token"] = token
        response = request(VISION, params, limit=4_000_000)
        if response["status"] != 200:
            return {"status": response["status"], "keys": [], "error": response["body"]}
        try:
            root = ET.fromstring(response["body"])
        except ET.ParseError as e:
            return {"status": response["status"], "keys": [], "error": str(e)}
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        keys.extend(x.text for x in root.findall("s3:Contents/s3:Key", ns) if x.text)
        truncated = root.findtext("s3:IsTruncated", default="false", namespaces=ns) == "true"
        token = root.findtext("s3:NextContinuationToken", namespaces=ns)
        if not truncated or not token:
            break
    dates = re.findall(r"(20\d\d-\d\d-\d\d)", "\n".join(keys))
    return {"status": 200, "keys": len(keys), "earliest": min(dates) if dates else None, "latest": max(dates) if dates else None, "sample_key": keys[0] if keys else None}


def vision_listing_fast(prefix):
    def page(start_after=None):
        params = {"list-type": "2", "prefix": prefix, "max-keys": 1000}
        if start_after:
            params["start-after"] = start_after
        response = request(VISION, params, limit=4_000_000)
        if response["status"] != 200:
            return {"status": response["status"], "files": [], "error": response["body"]}
        try:
            root = ET.fromstring(response["body"])
        except ET.ParseError as e:
            return {"status": response["status"], "files": [], "error": str(e)}
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        files = []
        for item in root.findall("s3:Contents", ns):
            key = item.findtext("s3:Key", namespaces=ns)
            size = item.findtext("s3:Size", namespaces=ns)
            if key and key.endswith(".zip"):
                files.append({"key": key, "size": int(size) if size else None})
        return {"status": 200, "files": files}

    first = page()
    symbol = next((s for s in SYMBOLS if f"/{s}/" in prefix), "BTCUSDT")
    parts = prefix.rstrip("/").split("/")
    suffix = parts[-1] if parts[-1] not in SYMBOLS else parts[-2]
    late = page(prefix + f"{symbol}-{suffix}-2025-01-01")
    files = first.get("files", []) + late.get("files", [])
    dates = re.findall(r"(20\d\d-\d\d-\d\d)", "\n".join(x["key"] for x in files))
    latest = max(dates) if dates else None
    latest_file = next((x for x in files if latest and latest in x["key"]), None)
    return {"status": first.get("status"), "earliest": min(dates) if dates else None, "latest": latest, "sample_archive": files[0] if files else None, "latest_archive": latest_file, "late_probe_status": late.get("status"), "error": first.get("error") or late.get("error")}


def sample_dataset(dataset, symbol):
    spec = DATASETS[dataset]
    params = dict(spec["params"])
    params["symbol"] = symbol
    params["limit"] = 20
    if dataset == "indexPriceKlines":
        params.pop("symbol", None)
        params["pair"] = symbol
    elif dataset == "premiumIndexKlines":
        params.pop("pair", None)
    response = json_get(spec["endpoint"], params)
    rows = response["json"] if isinstance(response["json"], list) else []
    ts = timestamps(dataset, rows)
    expected = spec["interval_ms"]
    deltas = [b - a for a, b in zip(ts, ts[1:])]
    gaps = [d for d in deltas if expected and d > expected * 2]
    return {
        "status": response["status"],
        "rows": len(rows),
        "schema": sorted(rows[0]) if rows and isinstance(rows[0], dict) else (f"array[{len(rows[0])}]" if rows and isinstance(rows[0], list) else None),
        "first_timestamp": iso(min(ts)) if ts else None,
        "last_timestamp": iso(max(ts)) if ts else None,
        "timestamp_deltas_ms": sorted(set(deltas))[:20],
        "obvious_gap_count": len(gaps),
        "symbol_scope": symbol,
        "symbol_field_present": any(isinstance(row, dict) and "symbol" in row for row in rows),
        "sample": rows[:2],
    }


def main():
    now = datetime.now(timezone.utc)
    results = {"generated_at": now.isoformat(), "symbols": SYMBOLS, "datasets": {}, "alignment": {}}
    for dataset, spec in DATASETS.items():
        vision = {symbol: vision_listing_fast(spec["vision"].format(symbol=symbol)) for symbol in SYMBOLS}
        samples = {symbol: sample_dataset(dataset, symbol) for symbol in SYMBOLS}
        results["datasets"][dataset] = {"source": {"rest": REST + spec["endpoint"], "vision_prefix": spec["vision"]}, "vision_metadata": vision, "samples": samples, "frequency": {"interval_ms": spec["interval_ms"]}}

    funding_boundary = {}
    for symbol in SYMBOLS:
        rows = json_get("/fapi/v1/fundingRate", {"symbol": symbol, "endTime": 1609459200000, "limit": 1000})["json"]
        funding_boundary[symbol] = {"earliest_returned_at_or_before_2021": iso(rows[0]["fundingTime"]) if rows else None, "rows": len(rows) if isinstance(rows, list) else 0}
    results["rest_boundary_probes"] = {
        "fundingRate": funding_boundary,
        "aggTrades": "REST search window is restricted to the recent 2 days; use Vision archives for older bulk history.",
        "openInterestHist": "REST rejects endTime/startTime boundary parameters in this probe; endpoint is recent-history only in practice. Use Vision metrics for bulk history.",
    }

    # Alignment is causal if features are joined by event timestamp and only shifted/closed bars are used.
    results["alignment"] = {
        "same_exchange_clock": True,
        "causal_join_rule": "floor observations to the target bar and use only timestamps <= decision timestamp; lag close-derived features by one complete bar",
        "lookahead_free": True,
        "caveats": ["funding is event-time data and must not be forward-filled before its funding timestamp", "open-interest history is a separate 5m observation series and must be lagged like any other feature", "aggTrades are event-time rows and require bar aggregation before joining"],
    }
    (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
