"""Transaction Cost Analysis over the OMS fill history.

Implementation shortfall (Perold) is the spine: the cost of a parent order is
the gap between what you got and the price when you decided to trade, and it
decomposes into pieces a desk can act on:

    IS = spread/impact cost + fees + opportunity cost (unfilled)

Conventions that matter and are easy to get wrong:

  * SIGNED so positive is ALWAYS bad, on both sides. A buy filled above
    arrival and a sell filled below it are both positive cost. Reporting raw
    (exec - arrival) means buys and sells cancel in the aggregate and a desk
    bleeding on both sides looks flat.
  * bps are computed against the ARRIVAL price, not the execution price. Using
    the execution price as denominator makes costs look smaller precisely when
    they are largest.
  * OPPORTUNITY COST is charged on the unfilled remainder. An algo that
    "achieves" a great price by only filling 20% of the order is not cheap --
    it just moved the cost somewhere the naive metric can't see it.

Aggregates are reported with medians and p95 alongside means, because
execution cost distributions are skewed and a single bad print drags the mean
somewhere no trade actually happened.
"""
from __future__ import annotations

import statistics
from collections import defaultdict


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None,
                "best": None, "worst": None, "stdev": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(_pct(values, 0.95), 3),
        "best": round(min(values), 3),
        "worst": round(max(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


def order_tca(order: dict, fills: list[dict], mark: float | None = None) -> dict | None:
    """Cost breakdown for one order. Returns None if it can't be measured.

    Deliberately returns None rather than zero when the arrival price is
    missing: a zero would be averaged into the aggregates as a free trade.
    """
    arrival = order.get("arrival_mid")
    filled = order.get("filled_qty") or 0.0
    if not arrival or arrival <= 0:
        return None

    sign = 1 if order.get("side") == "buy" else -1
    avg = order.get("avg_price") or 0.0
    qty = order.get("qty") or 0.0

    exec_bps = ((avg - arrival) / arrival * 1e4 * sign) if filled > 0 else 0.0
    notional = filled * avg
    fees = sum(f.get("fee", 0.0) for f in fills)
    fee_bps = (fees / notional * 1e4) if notional > 0 else 0.0

    # Opportunity cost: the unfilled remainder marked at the current price.
    # Without a mark we report it as unmeasured rather than assuming zero --
    # assuming zero flatters exactly the orders that failed to complete.
    unfilled = max(0.0, qty - filled)
    opp_bps = None
    if unfilled > 0 and mark:
        opp_bps = round((mark - arrival) / arrival * 1e4 * sign * (unfilled / qty), 3)

    total = exec_bps + fee_bps + (opp_bps or 0.0)
    return {
        "order_id": order.get("id"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "algo": order.get("algo") or "UNKNOWN",
        "venue": order.get("venue"),
        "qty": qty,
        "filled_qty": filled,
        "fill_rate_pct": round(filled / qty * 100, 2) if qty else 0.0,
        "arrival": round(arrival, 6),
        "avg_price": round(avg, 6),
        "notional": round(notional, 2),
        "execution_bps": round(exec_bps, 3),
        "fee_bps": round(fee_bps, 3),
        "opportunity_bps": opp_bps,
        "total_cost_bps": round(total, 3),
        "n_fills": len(fills),
        "created_at": order.get("created_at"),
    }


def analyse(orders: list[dict], fills: list[dict],
            marks: dict[str, float] | None = None) -> dict:
    """Full TCA over the order book history.

    Child orders are EXCLUDED from the headline aggregates: a parent and its
    children describe the same economic trade, and counting both would
    double-weight every routed order and understate average cost (children
    inherit the parent's arrival price and each look cheap in isolation).
    Venue statistics are computed from children, since that is where venue
    attribution actually lives.
    """
    marks = {k.upper(): v for k, v in (marks or {}).items()}
    by_order = defaultdict(list)
    for f in fills:
        by_order[f.get("order_id")].append(f)

    parents, children = [], []
    for o in orders:
        if o.get("status") in ("rejected", "cancelled") and not o.get("filled_qty"):
            continue
        row = order_tca(o, by_order.get(o.get("id"), []),
                        marks.get((o.get("symbol") or "").upper()))
        if row is None:
            continue
        (children if o.get("parent_id") else parents).append(row)

    costs = [r["total_cost_bps"] for r in parents]
    exec_only = [r["execution_bps"] for r in parents]
    fee_only = [r["fee_bps"] for r in parents]
    fill_rates = [r["fill_rate_pct"] for r in parents]

    # By algo -- the comparison that decides which algo gets the flow
    by_algo = defaultdict(list)
    for r in parents:
        by_algo[r["algo"]].append(r)
    algo_rows = []
    for algo, rows in sorted(by_algo.items()):
        c = [x["total_cost_bps"] for x in rows]
        algo_rows.append({
            "algo": algo, "orders": len(rows),
            "notional": round(sum(x["notional"] for x in rows), 2),
            "avg_fill_rate_pct": round(statistics.fmean(
                [x["fill_rate_pct"] for x in rows]), 2),
            **{f"cost_{k}": v for k, v in _summary(c).items() if k != "n"},
        })

    # By venue -- from children where routing actually happened
    by_venue = defaultdict(list)
    for r in children or parents:
        if r.get("venue"):
            by_venue[r["venue"]].append(r)
    venue_rows = []
    for venue, rows in sorted(by_venue.items()):
        c = [x["execution_bps"] for x in rows]
        venue_rows.append({
            "venue": venue, "fills": len(rows),
            "notional": round(sum(x["notional"] for x in rows), 2),
            "share_pct": 0.0,       # filled in below once total is known
            **{f"cost_{k}": v for k, v in _summary(c).items() if k != "n"},
        })
    tot_notional = sum(v["notional"] for v in venue_rows) or 1.0
    for v in venue_rows:
        v["share_pct"] = round(v["notional"] / tot_notional * 100, 2)

    # Notional-weighted average cost -- what the desk actually paid. The
    # simple mean over orders lets a tiny order matter as much as a huge one.
    weighted = (sum(r["total_cost_bps"] * r["notional"] for r in parents) /
                sum(r["notional"] for r in parents)) if parents else None

    return {
        "orders_analysed": len(parents),
        "child_orders": len(children),
        "total_notional": round(sum(r["notional"] for r in parents), 2),
        "cost_bps": _summary(costs),
        "execution_bps": _summary(exec_only),
        "fee_bps": _summary(fee_only),
        "fill_rate_pct": _summary(fill_rates),
        "notional_weighted_cost_bps": round(weighted, 3) if weighted is not None else None,
        "by_algo": algo_rows,
        "by_venue": venue_rows,
        "orders": sorted(parents, key=lambda r: r["created_at"] or "", reverse=True)[:50],
        "outliers": sorted(parents, key=lambda r: -abs(r["total_cost_bps"]))[:5],
    }


def fill_training_rows(orders: list[dict], fills: list[dict]) -> list[dict]:
    """Turn the OMS history into labelled rows for the fill-probability model.

    label = 1 if the order completed, 0 otherwise. This is the real training
    data the SOR model should eventually use instead of a synthetic generator;
    it is exposed here so the sample size is visible and the model is only
    retrained on live data once there is enough of it to mean anything.
    """
    by_order = defaultdict(list)
    for f in fills:
        by_order[f.get("order_id")].append(f)
    rows = []
    for o in orders:
        qty = o.get("qty") or 0.0
        if qty <= 0 or o.get("status") == "rejected":
            continue
        filled = o.get("filled_qty") or 0.0
        rows.append({
            "symbol": o.get("symbol"), "algo": o.get("algo"),
            "venue": o.get("venue"), "qty": qty,
            "n_fills": len(by_order.get(o.get("id"), [])),
            "fill_rate": round(filled / qty, 6),
            "label_complete": 1 if filled >= qty - 1e-9 else 0,
        })
    return rows
