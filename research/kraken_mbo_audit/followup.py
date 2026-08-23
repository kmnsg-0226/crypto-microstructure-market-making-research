#!/usr/bin/env python3
"""Bounded execution/order reconciliation and queue-ambiguity probe."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://futures.kraken.com/api/history/v3/market/PI_XBTUSD"
OUT = Path(__file__).parent


def get(path, params=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    try:
        with urlopen(Request(url, headers={"User-Agent": "crypto-hft-like-bot/kraken-mbo-followup"}), timeout=30) as r:
            return {"status": r.status, "body": json.loads(r.read(8_000_000))}
    except HTTPError as e:
        return {"status": e.code, "body": e.read(1000).decode("utf-8", "replace")}
    except (URLError, TimeoutError) as e:
        return {"status": None, "body": str(e)}


def name(e):
    event = e.get("event", {})
    return next(iter(event), "other") if isinstance(event, dict) and event else "other"


def payload(e):
    event = e.get("event", {})
    return next(iter(event.values()), {}) if isinstance(event, dict) and event else {}


def order(e):
    p = payload(e)
    return p.get("order", {}) if isinstance(p, dict) else {}


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * q))]


def main():
    execution_body = get("/executions")
    execution_elements = execution_body["body"].get("elements", []) if execution_body["status"] == 200 else []
    executions = []
    for element in execution_elements[:30]:
        x = payload(element).get("execution", {})
        if x.get("makerOrder", {}).get("uid") and x.get("takerOrder", {}).get("uid"):
            executions.append(x)

    reconciled = []
    all_window_events = []
    for x in executions:
        ts = x["timestamp"]
        response = get("/orders", {"before": ts + 5001})
        batch = response["body"].get("elements", []) if response["status"] == 200 else []
        window = [e for e in batch if isinstance(e.get("timestamp"), int) and ts - 5000 <= e["timestamp"] <= ts + 5000]
        all_window_events.extend(window)
        maker_uid = x["makerOrder"]["uid"]
        maker_events = [e for e in window if order(e).get("uid") == maker_uid]
        placed = [e for e in maker_events if name(e) == "OrderPlaced"]
        terminals = [e for e in maker_events if name(e) in {"OrderCancelled", "OrderFilled", "OrderClosed", "OrderDone"}]
        placement_order = order(placed[0]) if placed else {}
        cancel_payloads = [payload(e) for e in terminals if name(e) == "OrderCancelled"]
        remaining = [order(e).get("quantity") for e in terminals if name(e) == "OrderCancelled" and order(e).get("quantity") not in (None, "")]
        original_qty = float(placement_order["quantity"]) if placement_order.get("quantity") else None
        executed_qty = float(x["quantity"]) if x.get("quantity") else None
        remaining_qty = float(remaining[-1]) if remaining else None
        if original_qty is not None and executed_qty is not None and executed_qty == original_qty:
            fill_class = "full"
        elif remaining_qty is not None and original_qty is not None and executed_qty + remaining_qty == original_qty:
            fill_class = "partial_fill_then_cancel"
        else:
            fill_class = "partial_or_prior_fill_unknown"
        reconciled.append({
            "execution_uid": x.get("uid"),
            "timestamp": iso(ts),
            "maker_order_uid": maker_uid,
            "maker_placed_in_window": bool(placed),
            "execution_price": x.get("price"),
            "maker_price": placement_order.get("limitPrice") or x["makerOrder"].get("limitPrice"),
            "executed_quantity": x.get("quantity"),
            "original_quantity": placement_order.get("quantity") or x["makerOrder"].get("quantity"),
            "remaining_quantity_fields": remaining,
            "terminal_state_in_window": [name(e) for e in terminals],
            "partial_vs_full_single_execution": fill_class,
            "cancel_after_execution_in_window": any(e.get("timestamp", 0) >= ts for e in terminals if name(e) == "OrderCancelled"),
            "window_event_count": len(window),
        })

    placed_count = sum(r["maker_placed_in_window"] for r in reconciled)
    execution_reconciliation_rate = placed_count / len(reconciled) if reconciled else None

    # Exact timestamp/side/price cohorts observed in the fetched order windows.
    cohorts = defaultdict(list)
    for e in all_window_events:
        if name(e) != "OrderPlaced":
            continue
        o = order(e)
        cohorts[(e.get("timestamp"), o.get("direction"), o.get("limitPrice"))].append(o)
    ambiguous = {k: v for k, v in cohorts.items() if len(v) > 1}
    maker_ambiguous = []
    for r in reconciled:
        matches = [v for v in ambiguous.values() if any(o.get("uid") == r["maker_order_uid"] for o in v)]
        if matches:
            cohort = next(v for v in matches)
            maker_ambiguous.append({
                "execution_uid": r["execution_uid"],
                "maker_order_uid": r["maker_order_uid"],
                "cohort_size": len(cohort),
                "cohort_quantity": sum(float(o.get("quantity", 0)) for o in cohort),
                "execution_price": r["execution_price"],
                "at_executed_quote": r["maker_price"] == r["execution_price"],
            })

    # Lifetimes use the same bounded ten-page order sample: only fully observed pairs count.
    pages, order_events, token = [], [], None
    for _ in range(10):
        query = {"continuationToken": token} if token else None
        response = get("/orders", query)
        batch = response["body"].get("elements", []) if response["status"] == 200 else []
        pages.append(len(batch))
        order_events.extend(batch)
        token = response["body"].get("continuationToken") if isinstance(response["body"], dict) else None
        if not token:
            break
    by_uid = defaultdict(list)
    for e in order_events:
        uid = order(e).get("uid")
        if uid:
            by_uid[uid].append(e)
    lifetimes = []
    for events in by_uid.values():
        starts = [e for e in events if name(e) == "OrderPlaced"]
        ends = [e for e in events if name(e) in {"OrderCancelled", "OrderFilled", "OrderClosed", "OrderDone"}]
        if starts and ends:
            lifetime = min(e["timestamp"] for e in ends) - min(e["timestamp"] for e in starts)
            if lifetime >= 0:
                lifetimes.append(lifetime / 1000)

    warmups = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
    # This is an empirical stationary proxy from fully observed lifetimes, not exact state reconstruction.
    placement_rate = len(lifetimes) / max((max((e.get("timestamp", 0) for e in order_events), default=0) - min((e.get("timestamp", 0) for e in order_events), default=0)) / 1000, 1)
    unknown_proxy = {
        key: round(placement_rate * sum(max(lifetime - seconds, 0) for lifetime in lifetimes) / len(lifetimes), 3) if lifetimes else None
        for key, seconds in warmups.items()
    }

    result = {
        "execution_sample": {"requested": 30, "selected": len(executions), "source_status": execution_body["status"]},
        "reconciliation": {"sampled_executions": len(reconciled), "maker_placed_in_window": placed_count, "rate": execution_reconciliation_rate, "records": reconciled},
        "fifo_ambiguity": {
            "ambiguous_same_ms_side_price_cohorts": len(ambiguous),
            "orders_in_ambiguous_cohorts": sum(len(v) for v in ambiguous.values()),
            "executions_with_ambiguous_maker": len(maker_ambiguous),
            "ambiguous_makers_at_execution_price": sum(x["at_executed_quote"] for x in maker_ambiguous),
            "maker_cohort_size_median": median([x["cohort_size"] for x in maker_ambiguous]) if maker_ambiguous else None,
            "maker_cohort_size_p95": percentile([x["cohort_size"] for x in maker_ambiguous], .95),
            "maker_cohort_size_max": max((x["cohort_size"] for x in maker_ambiguous), default=None),
            "records": maker_ambiguous,
            "queue_ahead_effect": "For each ambiguous maker cohort, FIFO position can vary from first to last within the cohort; cohort treatment yields an interval/conservative bound, not an exact fill time.",
            "conservative_convention": "Treat same-millisecond, same-side, same-price placements as one unordered queue cohort; report queue-ahead as a range or use the worst-case position.",
        },
        "lifetimes": {
            "order_pages": pages,
            "fully_matched_lifecycles": len(lifetimes),
            "median_seconds": median(lifetimes) if lifetimes else None,
            "p95_seconds": percentile(lifetimes, .95),
            "p99_seconds": percentile(lifetimes, .99),
            "max_seconds": max(lifetimes) if lifetimes else None,
            "unknown_pre_window_inventory_proxy_after_warmup": unknown_proxy,
            "proxy_definition": "estimated steady-state count of observed-lifetime orders older than each warm-up; excludes right-censored/unmatched orders and is not exact reconstruction",
        },
        "verdict": "B1" if len(maker_ambiguous) == 0 else "B2",
    }
    (OUT / "followup_results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
