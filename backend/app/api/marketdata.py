"""Market data API — traditional assets from keyless public sources.

Equity indices/VIX/credit from FRED, the par yield curve from the US
Treasury, FX from the ECB. All end-of-day; `as_of` is returned on every
payload so the UI can date-stamp rather than imply live ticks.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..core import marketdata as md

router = APIRouter(prefix="/api/marketdata", tags=["market-data"])


@router.get("/snapshot")
def snapshot() -> dict:
    return md.snapshot()


@router.get("/store")
def store_status() -> dict:
    """What the EOD store holds and when each source was last reached.

    A persisted store answers reads even when the upstream is unreachable,
    which is the point of it -- but that makes "how current is this?" a
    question the platform has to be able to answer rather than assume. `ok`
    false with an older `refreshed_at` means the screens are serving real data
    that has stopped being updated, which is a materially different situation
    from a healthy store and should not look identical.
    """
    from ..core.marketdata.store import eod
    st = eod()
    return {"coverage": st.coverage(), "sources": st.meta()}


@router.get("/universe")
def universe(desk: str = Query("traditional",
                               pattern="^(traditional|digital|all)$")) -> dict:
    """The instrument master: what can be traded, marked and stressed.

    `tradable` is false for RATE and SPREAD instruments. They are risk
    factors, not order lines -- a yield has no price to multiply by quantity.

    Scoped by desk, and defaulting to `traditional`, because this endpoint
    populates order-entry dropdowns. Serving the full master to the Blotter
    let a trader pick BTCUSDT on a screen whose marks, hours and settlement
    are all end-of-day equities infrastructure. `desk=all` returns the
    complete master for anyone who genuinely wants it.
    """
    rows = [md.universe.to_dict(i) for i in md.UNIVERSE]
    if desk != "all":
        want = md.universe.CRYPTO_ASSET_CLASS if desk == "digital" else None
        rows = [r for r in rows
                if (r["asset_class"] == md.universe.CRYPTO_ASSET_CLASS)
                == (want is not None)]
    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r["asset_class"]] = by_class.get(r["asset_class"], 0) + 1
    return {
        "instruments": rows,
        "count": len(rows),
        "tradable": sum(1 for r in rows if r["tradable"]),
        "by_asset_class": by_class,
        "risk_factors": sorted({r["risk_factor"] for r in rows}),
        # Aliases resolve user input to a canonical symbol, so an unscoped
        # map re-admits "BTC" to a traditional order-entry box even when the
        # instrument list excludes it.
        "aliases": {k: v for k, v in md.universe.ALIASES.items()
                    if desk == "all"
                    or md.universe.is_crypto(v) == (desk == "digital")},
    }


@router.get("/board")
def board(days: int = Query(260, ge=30, le=2000),
          desk: str = Query("traditional",
                            pattern="^(traditional|digital|all)$")) -> dict:
    """Consolidated board — one row per instrument, scoped to a desk.

    Crypto rows are live consolidated mids; everything else is end-of-day and
    is dated and aged in business days, so a stale mark is visible rather
    than assumed current.

    `desk` is a deliberate replacement for the old `include_crypto` boolean.
    The two desks are not a toggle on one screen: they settle differently,
    trade different hours and are marked by different infrastructure with
    different latency. Defaulting to `traditional` keeps digital assets off
    the FX/rates/credit board, where a live 24/7 mid sitting beside a
    three-day-old Treasury series invites a comparison that does not hold.
    """
    from ..core.marketdata.universe import is_crypto
    try:
        data = md.market_board(days=days, include_crypto=(desk != "traditional"))
        if desk == "digital":
            rows = data.get("rows") or data.get("board") or []
            key = "rows" if "rows" in data else "board"
            data[key] = [r for r in rows
                         if is_crypto(str(r.get("symbol", "")))]
        data["desk"] = desk
        return data
    except Exception as e:
        raise HTTPException(502, f"board build failed: {type(e).__name__}: {e}")


@router.get("/curve")
def curve() -> dict:
    try:
        return md.treasury_curve()
    except Exception as e:
        raise HTTPException(502, f"treasury fetch failed: {e}")


@router.get("/fx")
def fx() -> dict:
    try:
        return md.ecb_fx()
    except Exception as e:
        raise HTTPException(502, f"ecb fetch failed: {e}")


@router.get("/series/{series_id}")
def series(series_id: str, limit: int = Query(260, le=2000)) -> dict:
    try:
        return md.fred_series(series_id.upper(), limit)
    except Exception as e:
        raise HTTPException(502, f"fred fetch failed: {e}")
