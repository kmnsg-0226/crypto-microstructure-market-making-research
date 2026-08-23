#!/usr/bin/env python3
"""Small, bounded feasibility probe for Kraken Futures historical MBO."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE = "https://futures.kraken.com/api/history/v3/market/PI_XBTUSD"
OUT = Path(__file__).parent


def get(path, params=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    try:
        with urlopen(Request(url, headers={"User-Agent": "crypto-hft-like-bot/kraken-audit"}), timeout=30) as r:
            return {"status": r.status, "url": url, "body": json.loads(r.read(8_000_000))}
    except HTTPError as e:
        return {"status": e.code, "url": url, "body": e.read(1000).decode("utf-8", "replace")}
    except (URLError, TimeoutError) as e:
        return {"status": None, "url": url, "body": str(e)}


def event_name(element):
    event = element.get("event", {})
    return next(iter(event), "other") if isinstance(event, dict) else "other"


def event_payload(element):
    event = element.get("event", {})
    return next(iter(event.values()), {}) if isinstance(event, dict) and event else {}


def order_uid(element):
    payload = event_payload(element)
    order = payload.get("order", {}) if isinstance(payload, dict) else {}
    return order.get("uid") or payload.get("uid")


def ms(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None


def fetch_pages(path, count=10, params=None):
    pages, elements, token = [], [], None
    for page_no in range(count):
        query = dict(params or {})
        if token:
            query["continuationToken"] = token
        response = get(path, query or None)
        body = response["body"]
        batch = body.get("elements", []) if response["status"] == 200 and isinstance(body, dict) else []
        pages.append({"page": page_no + 1, "count": len(batch), "status": response["status"], "continuationToken": body.get("continuationToken") if isinstance(body, dict) else None})
        elements.extend(batch)
        token = body.get("continuationToken") if isinstance(body, dict) else None
        if response["status"] != 200 or not token:
            break
    return pages, elements


def compact_probe(response):
    body = response["body"]
    if not isinstance(body, dict):
        return {"status": response["status"], "body": body}
    elements = body.get("elements", [])
    return {
        "status": response["status"],
        "keys": sorted(body),
        "element_count": len(elements) if isinstance(elements, list) else None,
        "sample": elements[:2] if isinstance(elements, list) else [],
    }


def main():
    pages, elements = fetch_pages("/orders")

    counts = Counter(event_name(e) for e in elements)
    placements = defaultdict(list)
    all_by_order = defaultdict(list)
    for e in elements:
        uid = order_uid(e)
        if uid:
            all_by_order[uid].append(e)
            if event_name(e) == "OrderPlaced":
                placements[uid].append(e)
    duplicate_placements = {uid: len(v) for uid, v in placements.items() if len(v) > 1}
    placement_uids = set(placements)
    lifecycle_uids = set(all_by_order)
    before_update_cancel = sum(
        any(event_name(e) in {"OrderUpdated", "OrderCancelled"} for e in all_by_order[uid])
        for uid in placement_uids
    )
    updates_or_cancels = sum(counts[n] for n in ("OrderUpdated", "OrderCancelled"))
    update_cancel_without_placement = sum(
        1 for uid in lifecycle_uids - placement_uids
        for e in all_by_order[uid] if event_name(e) in {"OrderUpdated", "OrderCancelled"}
    )
    terminal = {"OrderCancelled", "OrderFilled", "OrderClosed", "OrderDone"}
    incomplete = sorted(uid for uid in placement_uids if not any(event_name(e) in terminal for e in all_by_order[uid]))

    collisions = defaultdict(list)
    for e in elements:
        if event_name(e) != "OrderPlaced":
            continue
        p = event_payload(e).get("order", {})
        key = (e.get("timestamp"), p.get("direction"), p.get("limitPrice"))
        collisions[key].append(p.get("uid"))
    collision_groups = [
        {"timestamp": k[0], "timestamp_iso": ms(k[0]), "side": k[1], "price": k[2], "uids": v}
        for k, v in collisions.items() if len(v) > 1
    ]

    order_timestamps = [e.get("timestamp") for e in elements if isinstance(e.get("timestamp"), int)]
    execution_pages, execution_elements = fetch_pages("/executions", params={"before": max(order_timestamps) + 1} if order_timestamps else None)
    execution_order_uids = set()
    for e in execution_elements:
        execution = event_payload(e).get("execution", {})
        for role in ("makerOrder", "takerOrder"):
            uid = execution.get(role, {}).get("uid")
            if uid:
                execution_order_uids.add(uid)
    execution_timestamps = [e.get("timestamp") for e in execution_elements if isinstance(e.get("timestamp"), int)]
    executions = {"/executions": {"pages": execution_pages, "events": len(execution_elements), "window": {"oldest": ms(min(execution_timestamps)) if execution_timestamps else None, "newest": ms(max(execution_timestamps)) if execution_timestamps else None}, "unique_order_uids": len(execution_order_uids), "order_uid_overlap": len(execution_order_uids & lifecycle_uids), "execution_samples": [event_payload(e) for e in execution_elements[:3]]}}
    for route in ("/trades", "/executions", "/fills", "/publictrades"):
        if route not in executions:
            executions[route] = compact_probe(get(route))
    snapshots = {}
    for route in ("/orderbook", "/book", "/snapshot"):
        snapshots[route] = compact_probe(get(route))

    now = datetime.now(timezone.utc)
    retention = {}
    for days in (7, 30, 90, 365, 730):
        before = int((now - timedelta(days=days)).timestamp() * 1000)
        response = get("/orders", {"before": before})
        batch = response["body"].get("elements", []) if isinstance(response["body"], dict) else []
        timestamps = [e.get("timestamp") for e in batch if isinstance(e.get("timestamp"), int)]
        retention[f"{days}d"] = {
            "requested_before": before,
            "requested_before_iso": ms(before),
            "status": response["status"],
            "events": len(batch),
            "oldest_returned": min(timestamps) if timestamps else None,
            "oldest_returned_iso": ms(min(timestamps)) if timestamps else None,
            "newest_returned_iso": ms(max(timestamps)) if timestamps else None,
        }

    updated_samples = [event_payload(e) for e in elements if event_name(e) == "OrderUpdated"][:5]
    result = {
        "generated_at": now.isoformat(),
        "symbol": "PI_XBTUSD",
        "pages": pages,
        "events": {
            "total": len(elements),
            "window": {"oldest": ms(min(order_timestamps)) if order_timestamps else None, "newest": ms(max(order_timestamps)) if order_timestamps else None},
            "counts": dict(counts),
            "unique_order_uid": len(lifecycle_uids),
            "duplicate_placements": {"orders": len(duplicate_placements), "extra_events": sum(n - 1 for n in duplicate_placements.values()), "uids": duplicate_placements},
            "placement_before_update_or_cancel_rate": before_update_cancel / len(placements) if placements else None,
            "update_cancel_events_without_seen_placement": update_cancel_without_placement,
            "incomplete_lifecycles": {"count": len(incomplete), "uids": incomplete[:100], "definition": "placed order with no explicit cancel/fill/closed/done event in the ten pages"},
            "same_price_timestamp_groups": len(collision_groups),
            "same_price_timestamp_orders": sum(len(g["uids"]) for g in collision_groups),
            "fifo_observable": False if collision_groups else None,
        },
        "order_updated_samples": updated_samples,
        "execution_route_probes": executions,
        "snapshot_route_probes": snapshots,
        "retention": retention,
        "interpretation": {
            "updated": "Review order_updated_samples; an update exposing only remaining quantity/price cannot identify fills versus amendments without executions or explicit reason.",
            "fifo": "Same side, price, and timestamp gives no exact intra-timestamp sequence unless the API supplies a sequence field; JSON array order is not treated as authoritative FIFO.",
            "bootstrap": "Exact reconstruction before the oldest retained order event requires a historical L3 snapshot; an old event stream alone is sufficient only if it begins before the target state and contains all order mutations.",
        },
    }
    (OUT / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
