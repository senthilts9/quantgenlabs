"""Consolidated cross-asset board.

Fetches the whole instrument master and returns one row per instrument with
last, change, realised vol, 52-week range and mark staleness.

Three things here are not cosmetic:

  CONCURRENCY. Forty sequential HTTPS round-trips to FRED take ~20s, which is
  a dead screen. They are independent, so they run on a small thread pool and
  the board returns in about a second. The pool is capped: opening forty
  simultaneous connections to a free public service to save half a second is
  how you get blocked.

  VOL BY QUOTE TYPE. A PRICE series gets percentage returns annualised by
  sqrt(252). A RATE series must NOT: the 10Y moving 4.77 -> 4.78 is a 1bp
  move, and expressing it as +0.21% and annualising that is meaningless --
  worse, it makes a quiet rates market look violent and a near-zero yield
  look infinitely volatile. Rates and spreads get BASIS POINT vol, which is
  how a rates desk actually quotes them.

  STALENESS. FRED publishes with a per-series lag; the FX block routinely
  trails the equity block by several business days. A mark is dated and aged
  in business days so a week-old FX rate cannot silently masquerade as
  today's close. Risk numbers computed on stale marks are wrong in a way that
  no amount of downstream maths will reveal.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import numpy as np

from . import universe as U
from .traditional import fred_series

# Free public endpoint: enough parallelism to be fast, not enough to be rude.
_MAX_WORKERS = 8
# Daily series older than this are flagged. Two business days absorbs a normal
# publication lag plus a holiday; beyond that the mark is genuinely behind.
_STALE_AFTER_BUSINESS_DAYS = 3


def _business_days_since(d: str) -> int | None:
    try:
        then = datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return int(np.busday_count(then, date.today()))


def _stats(points: list[dict], quote_type: str) -> dict:
    """Last / change / vol / 52w range for one series."""
    vals = np.array([p["value"] for p in points], dtype=float)
    if len(vals) < 2:
        return {}
    last, prev = float(vals[-1]), float(vals[-2])
    change = last - prev

    out = {
        "last": round(last, 6),
        "previous": round(prev, 6),
        "change": round(change, 6),
        "as_of": points[-1]["date"],
        "observations": len(vals),
        "high_52w": round(float(vals[-252:].max()), 6),
        "low_52w": round(float(vals[-252:].min()), 6),
    }

    if quote_type == "PRICE":
        out["change_pct"] = round(change / prev * 100, 4) if prev else None
        # Percentage returns; drop non-finite rows from zero/negative prints.
        r = np.diff(vals) / vals[:-1]
        r = r[np.isfinite(r)]
        if len(r) >= 20:
            out["vol_annual_pct"] = round(float(r.std(ddof=1)) * math.sqrt(252) * 100, 3)
        rng = out["high_52w"] - out["low_52w"]
        out["pct_of_52w_range"] = (round((last - out["low_52w"]) / rng * 100, 1)
                                   if rng > 0 else None)
    else:
        # RATE / SPREAD: basis points, not percent. A yield change is additive.
        out["change_bp"] = round(change * 100, 2)
        d = np.diff(vals) * 100.0
        d = d[np.isfinite(d)]
        if len(d) >= 20:
            out["vol_annual_bp"] = round(float(d.std(ddof=1)) * math.sqrt(252), 1)
        out["range_52w_bp"] = round((out["high_52w"] - out["low_52w"]) * 100, 1)
    return out


def _fred_row(inst: U.Instrument, days: int) -> dict:
    row = U.to_dict(inst)
    try:
        pts = (fred_series(inst.series_id, limit=days) or {}).get("points") or []
        if not pts:
            return {**row, "status": "no_data"}
        row.update(_stats(pts, inst.quote_type))
        age = _business_days_since(row.get("as_of", ""))
        row["staleness_business_days"] = age
        row["stale"] = age is not None and age > _STALE_AFTER_BUSINESS_DAYS
        row["status"] = "ok"
    except Exception as e:
        row.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:120]})
    return row


def _crypto_rows(insts: list[U.Instrument]) -> list[dict]:
    """Live consolidated mid per crypto pair, read off the shared quote cache.

    The multi-venue fan-out used to happen here, behind a 15s TTL. It was
    ~3.5s of the board's ~4s, and 15s is shorter than anyone spends looking at
    a screen, so in practice almost every request paid it. It now happens on a
    background refresher (services.crypto_quotes) and this is a cache read.

    Only genuinely live quotes are returned. `live_or_synth` falls back to a
    synthetic book when the venues are unreachable, and letting that reach a
    board labelled REALTIME would be the worst kind of bug on this platform:
    invented prices presented as live ones. The row is marked unavailable
    instead. `quote_age_s` says how old the mid is, and a quote the refresher
    has stopped updating goes stale rather than posing as current.
    """
    from ...services import crypto_quotes

    # One cold-fetch budget for the whole board, not one per pair: five pairs
    # each waiting their own timeout is five times the bound anyone intended.
    until = crypto_quotes.deadline()

    def one(inst: U.Instrument) -> dict:
        row = U.to_dict(inst)
        q = crypto_quotes.get(inst.series_id, until=until)
        row["quote_age_s"] = round(q.age_s, 1)
        if q.mid is None:
            row.update({"status": "unavailable",
                        "note": q.note or "no live venue quote"})
            return row
        row.update({
            "last": round(q.mid, 6),
            "as_of": datetime.fromtimestamp(q.ts, timezone.utc).isoformat(timespec="seconds"),
            "staleness_business_days": 0, "stale": q.stale,
            "venues": q.venues, "status": "ok",
            "venue_names": list(q.venue_names),
        })
        if q.stale:
            row["note"] = f"quote not refreshed for {row['quote_age_s']}s"
        return row

    return [one(i) for i in insts]


def market_board(days: int = 260, include_crypto: bool = True) -> dict:
    """The full cross-asset board, one row per instrument."""
    fred = [i for i in U.UNIVERSE if i.source == "fred"]
    crypto = [i for i in U.UNIVERSE if i.source == "ccxt"]

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        rows = list(ex.map(lambda i: _fred_row(i, days), fred))

    if include_crypto and crypto:
        rows += _crypto_rows(crypto)

    ok = [r for r in rows if r.get("status") == "ok"]
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(r["asset_class"], []).append(r)

    stale = [r["symbol"] for r in ok if r.get("stale")]
    movers = sorted([r for r in ok if r.get("change_pct") is not None],
                    key=lambda r: abs(r["change_pct"]), reverse=True)

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instruments": len(rows),
        "live": len(ok),
        "rows": rows,
        "by_asset_class": by_class,
        "asset_classes": sorted(by_class),
        "top_movers": [{"symbol": r["symbol"], "name": r["name"],
                        "change_pct": r["change_pct"], "last": r["last"]}
                       for r in movers[:8]],
        "stale_marks": stale,
        # Surfaced rather than buried: any risk number computed while this is
        # non-empty is using marks that are not today's.
        "data_quality": {
            "stale_count": len(stale),
            "failed": [r["symbol"] for r in rows if r.get("status") not in ("ok",)],
            "note": ("all marks current" if not stale else
                     f"{len(stale)} instrument(s) marked on stale data: "
                     f"{', '.join(stale[:6])}"),
        },
        "sources": {"fred": "end-of-day, US Federal Reserve (FRED)",
                    "ccxt": "live consolidated mid across public venues"},
    }


def marks(symbols: list[str] | None = None) -> dict[str, float]:
    """symbol -> last price, for OMS marking. PRICE instruments only.

    RATE and SPREAD instruments are excluded by construction: they have no
    price to mark a share-based position against.
    """
    want = [U.resolve(s) for s in symbols] if symbols else list(U.tradable())
    insts = [i for i in want if i is not None and i.tradable and i.source == "fred"]
    if not insts:
        return {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        rows = list(ex.map(lambda i: _fred_row(i, 5), insts))
    return {r["symbol"]: r["last"] for r in rows
            if r.get("status") == "ok" and r.get("last") is not None}
