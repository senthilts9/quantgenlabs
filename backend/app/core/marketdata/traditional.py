"""Traditional-market data from keyless public sources.

Deliberately no API keys and no third-party SDK: every source here is a
public government/central-bank endpoint that works from a datacenter IP.
That last part is not a small detail -- Yahoo Finance (and therefore
yfinance) returns HTTP 429 to AWS ranges regardless of headers, so anything
built on it silently degrades to synthetic data once deployed. These don't.

  * US Treasury  -- daily par yield curve, 1Mo..30Yr. The real curve, used
                    for discounting, duration/convexity and DV01.
  * FRED         -- index levels (S&P 500), VIX, credit spreads, fed funds.
                    Daily close, no key, via the CSV graph endpoint.
  * ECB          -- daily EUR reference rates for ~29 currencies.

All are END-OF-DAY, not intraday ticks -- stated plainly here so nothing
downstream claims tick-level freshness it doesn't have. Intraday equity
quotes need a keyed provider (Alpaca/Finnhub) or the replay feed.
"""
from __future__ import annotations

import csv
import io
import os
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

_UA = "quantgenlabs/3.0 (+https://quantgenlabs.com)"
# The Treasury endpoint serves a whole calendar year of daily curve data and
# measures ~19s from ap-southeast-2 -- close enough to a 20s ceiling that the
# refresh failed with a read timeout on the first deploy of the EOD store.
# These are background refresher calls now rather than request-path ones, so a
# generous ceiling costs nothing: the only thing a short timeout buys is an
# earlier give-up, and giving up means the curve silently stops updating.
_TIMEOUT = int(os.getenv("MARKETDATA_HTTP_TIMEOUT_S", "45"))

# Public endpoints are polled by every page load; a short TTL keeps the
# console responsive and avoids hammering someone else's free service.
_CACHE: dict[str, tuple[float, object]] = {}
_TTL_SECONDS = 300


def _certifi_context():
    """certifi's CA bundle, or None if certifi isn't installed."""
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _get(url: str) -> bytes:
    """HTTPS GET, verifying certificates.

    Containers (python:*-slim) ship ca-certificates, so the default context
    works there. Some standalone interpreters installed outside the OS
    package manager have no usable root store and fail every call with
    CERTIFICATE_VERIFY_FAILED -- and they can still report a non-empty store,
    so this retries with certifi's bundle on verification failure rather than
    trying to detect the situation up front. Verification is never disabled.
    """
    import ssl

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read()
    except (ssl.SSLCertVerificationError, urllib.error.URLError) as e:
        inner = getattr(e, "reason", e)
        if not isinstance(inner, ssl.SSLCertVerificationError):
            raise
        ctx = _certifi_context()
        if ctx is None:
            raise
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as r:
            return r.read()


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _cached(key: str, fn):
    """Read-through cache with single-flight and stale-on-error.

    Three things here are load-bearing:

    SINGLE FLIGHT. Without it, every caller that arrives while a cold key is
    being fetched starts its OWN fetch. The Treasury curve takes ~19s cold, so
    a fresh container plus a handful of visitors meant a handful of concurrent
    19s downloads of the same file, each of which then overwrote the same entry.
    Measured on a cold start: a request that arrived alongside the warmer's
    fetch paid the full 18.4s instead of waiting for a download already in
    flight. Now the first caller fetches and the rest wait on it.

    STAMP AFTER, NOT BEFORE. The timestamp used to be taken before `fn()` ran,
    so a 19s fetch was already 19s into its 300s TTL the moment it landed --
    the slowest sources expired soonest, which is exactly backwards.

    STALE ON ERROR. A transient blip from a free public endpoint used to turn
    into an exception, which the board surfaces as a 502. Returning the last
    good value is better: these are end-of-day series whose own `as_of` date is
    reported and aged downstream, so a slightly older entry is still honestly
    dated rather than silently wrong. Only a cold miss with no prior value
    raises.
    """
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL_SECONDS:
        return hit[1]
    with _lock_for(key):
        # Re-check under the lock: whoever held it may have just refreshed
        # the entry we were about to duplicate.
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL_SECONDS:
            return hit[1]
        try:
            val = fn()
        except Exception:
            if hit is not None:
                return hit[1]
            raise
        _CACHE[key] = (time.time(), val)
        return val


# --- US Treasury par yield curve ----------------------------------------
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)

# Tenor label -> years, for curve interpolation and bond maths.
TENOR_YEARS = {
    "1 Mo": 1 / 12, "1.5 Month": 1.5 / 12, "2 Mo": 2 / 12, "3 Mo": 0.25,
    "4 Mo": 4 / 12, "6 Mo": 0.5, "1 Yr": 1.0, "2 Yr": 2.0, "3 Yr": 3.0,
    "5 Yr": 5.0, "7 Yr": 7.0, "10 Yr": 10.0, "20 Yr": 20.0, "30 Yr": 30.0,
}


def _fetch_treasury(year: int | None = None) -> list[dict]:
    year = year or datetime.now(timezone.utc).year
    rows = list(csv.DictReader(io.StringIO(_get(TREASURY_URL.format(year=year)).decode())))
    if not rows:                     # early January: fall back to last year
        rows = list(csv.DictReader(io.StringIO(
            _get(TREASURY_URL.format(year=year - 1)).decode())))
    return rows


def _iso_date(raw: str) -> str | None:
    """Treasury's M/D/YYYY -> YYYY-MM-DD.

    Not cosmetic. The store orders and ranges by date as TEXT, and on
    `MM/DD/YYYY` strings lexicographic order is not chronological order:
    '12/31/2025' sorts above '09/08/2026', so `MAX(date)` returned a curve
    from LAST December as the latest one. ISO is the only text format where
    string order and date order agree, which is why the store keeps every
    date in it (FRED already publishes ISO, so this is the odd source out).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _curve_rows_to_dated(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Treasury CSV rows -> [(iso_date, [{tenor, years, yield_pct}, ...]), ...]."""
    out = []
    for r in rows:
        date = _iso_date(r.get("Date"))
        if not date:
            continue
        pts = []
        for label, yrs in TENOR_YEARS.items():
            raw = (r.get(label) or "").strip()
            if raw:
                try:
                    pts.append({"tenor": label, "years": yrs, "yield_pct": float(raw)})
                except ValueError:
                    pass
        if pts:
            out.append((date, pts))
    return out


def refresh_treasury(backfill_prior_year: bool = False) -> int:
    """Pull the Treasury curve and persist every day in the file.

    The endpoint is per-year, so a normal refresh fetches only the current
    year. `backfill_prior_year` is for the first population, where the factor
    model wants two years of curve and January would otherwise leave it with
    almost nothing.
    """
    from .store import eod
    try:
        rows = _fetch_treasury()
        if backfill_prior_year:
            try:
                rows = rows + _fetch_treasury(datetime.now(timezone.utc).year - 1)
            except Exception:
                pass                             # current year alone is usable
    except Exception as e:                       # noqa: BLE001
        eod().mark_failed("treasury", f"{type(e).__name__}: {e}")
        raise
    return eod().upsert_curve(_curve_rows_to_dated(rows))


def treasury_curve() -> dict:
    """Latest par yield curve + a short 10Y history. Yields are percent.

    Served from the EOD store. This was the worst cold start on the platform
    at ~19s -- the endpoint returns a whole year of daily curves -- and it was
    paid again by the first visitor after every restart, because an in-process
    cache does not survive one.
    """
    from .store import eod
    st = eod()
    as_of, points = st.curve_latest()
    if as_of is None:
        try:
            refresh_treasury(backfill_prior_year=True)
        except Exception:
            return {"as_of": None, "points": [], "history": [],
                    "source": "us-treasury", "origin": "unavailable"}
        as_of, points = st.curve_latest()
    return {"as_of": as_of, "points": points,
            "history": st.curve_tenor_history("10 Yr", 60),
            "source": "us-treasury", "origin": "eod-store"}


def treasury_history(days: int = 250) -> dict:
    """Full curve history as a (dates x tenors) matrix.

    The factor model needs every tenor per day, not just the 10Y: PCA on a
    single tenor is meaningless. Rows with any missing tenor are dropped
    rather than imputed -- interpolating a gap would manufacture a curve
    shape that was never quoted, and PCA would then fit that artefact.

    Served from the EOD store, which holds every day the refresher has seen --
    so the two-year sample the factor model wants accumulates instead of being
    re-downloaded, and January no longer depends on catching the prior-year
    file in the same request.
    """
    from .store import eod
    st = eod()
    tenors, dates, matrix = st.curve_matrix(days)
    if not dates:
        try:
            refresh_treasury(backfill_prior_year=True)
        except Exception:
            return {"tenors": [], "years": [], "dates": [], "matrix": [],
                    "source": "us-treasury", "origin": "unavailable"}
        tenors, dates, matrix = st.curve_matrix(days)
    return {"tenors": tenors, "years": [TENOR_YEARS.get(t, 0.0) for t in tenors],
            "dates": dates, "matrix": matrix,
            "source": "us-treasury", "origin": "eod-store"}


# --- FRED (keyless CSV) --------------------------------------------------
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

FRED_SERIES = {
    "SP500":    ("S&P 500", "index"),
    "VIXCLS":   ("VIX", "index"),
    "DFF":      ("Fed Funds (effective)", "pct"),
    "T10Y2Y":   ("10Y-2Y spread", "pct"),
    "BAMLH0A0HYM2": ("US High Yield OAS", "pct"),
    "DTWEXBGS": ("USD Broad Index", "index"),
}


def _fred_full(series_id: str) -> dict:
    """The whole parsed series, cached once per series_id.

    The CSV graph endpoint takes no date range -- `?id=SID` returns the entire
    history every time -- so the window a caller wants has always been a slice
    of something already in hand.

    This used to be cached per (series_id, limit), which meant each distinct
    window re-downloaded and re-parsed the identical file. The console asks for
    five: limit=5 (fred_latest), 30 and 260 (the board), 500 (VaR/FRM) and 750
    (the quant hub). So one series behind five screens was five full downloads,
    and any window nobody had thought to pre-warm paid a cold fan-out across
    the whole instrument master -- measured at 5.2s for the board's 30-day
    window and 6.9s for /api/quant/distributions at 750.

    Caching the parse once and slicing on read makes every window a cache hit
    as soon as any window has been fetched, which is what removes the class of
    bug rather than one instance of it: a new screen asking for a 400-day
    window is now free instead of quietly slow.
    """
    def build():
        rows = list(csv.reader(io.StringIO(_get(FRED_URL.format(sid=series_id)).decode())))
        label, unit = FRED_SERIES.get(series_id, (series_id, ""))
        if not rows:
            return {"series_id": series_id, "label": label, "unit": unit,
                    "points": [], "source": "fred"}
        out = []
        for r in rows[1:]:
            if len(r) < 2:
                continue
            date, raw = r[0], r[1].strip()
            if raw in (".", ""):
                continue
            try:
                out.append({"date": date, "value": float(raw)})
            except ValueError:
                continue
        return {"series_id": series_id, "label": label, "unit": unit,
                "points": out, "source": "fred"}
    return _cached(f"fred:{series_id}", build)


def _label(series_id: str) -> tuple[str, str]:
    return FRED_SERIES.get(series_id, (series_id, ""))


def refresh_fred_series(series_id: str) -> int:
    """Pull one series from FRED and persist it. Returns rows written.

    The fetch is unavoidably full -- the CSV endpoint has no date range -- but
    the WRITE is incremental: only new dates and genuinely revised values touch
    the database.
    """
    from .store import eod
    try:
        full = _fred_full(series_id)
    except Exception as e:                       # noqa: BLE001
        eod().mark_failed(f"fred:{series_id}", f"{type(e).__name__}: {e}")
        raise
    return eod().upsert_series(series_id, full.get("points") or [])


def fred_series(series_id: str, limit: int = 260) -> dict:
    """One FRED series as {date, value}, newest `limit` observations.

    Reads the EOD store. Missing days come back as '.' from FRED and are
    dropped at parse time rather than zero-filled -- a zero VIX would be worse
    than a gap.

    The network is touched only if the store has never held this series (first
    boot on an empty volume), and that first read persists what it fetched, so
    it happens once per series for the life of the database rather than once
    per cache expiry. Everything after is a keyed SQLite lookup.
    """
    from .store import eod
    st = eod()
    if not st.has_series(series_id):
        # Cold database. Fetch, persist, and serve from what we just wrote.
        try:
            refresh_fred_series(series_id)
        except Exception:
            label, unit = _label(series_id)
            return {"series_id": series_id, "label": label, "unit": unit,
                    "points": [], "source": "fred", "origin": "unavailable"}
    label, unit = _label(series_id)
    return {"series_id": series_id, "label": label, "unit": unit,
            "points": st.series(series_id, limit), "source": "fred",
            "origin": "eod-store"}


def fred_latest(series_ids: list[str] | None = None) -> list[dict]:
    """Latest level + day-over-day change for a set of series."""
    out = []
    for sid in (series_ids or list(FRED_SERIES)):
        try:
            s = fred_series(sid, limit=5)
            pts = s.get("points") or []
            if not pts:
                continue
            last = pts[-1]
            prev = pts[-2] if len(pts) > 1 else last
            chg = last["value"] - prev["value"]
            out.append({
                "series_id": sid, "label": s.get("label", sid),
                "unit": s.get("unit", ""), "date": last["date"],
                "value": last["value"], "change": round(chg, 4),
                "change_pct": round(chg / prev["value"] * 100, 3) if prev["value"] else 0.0,
            })
        except Exception:
            continue
    return out


# --- ECB FX reference rates ---------------------------------------------
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def _usd_pairs(rates: dict[str, float]) -> list[dict]:
    """USD crosses derived from the EUR reference rates.

    Derived on READ rather than stored: they are a pure function of the rates
    already persisted, and storing a derived value invites the two to disagree
    after a revision.
    """
    eurusd = rates.get("USD")
    if not eurusd:
        return []
    pairs = [{"pair": "EUR/USD", "rate": round(eurusd, 6)}]
    for ccy in ("JPY", "GBP", "CHF", "AUD", "CAD", "SEK", "NOK"):
        if ccy in rates:
            if ccy == "GBP":                     # quoted GBP/USD by convention
                pairs.append({"pair": "GBP/USD", "rate": round(eurusd / rates[ccy], 6)})
            else:
                pairs.append({"pair": f"USD/{ccy}", "rate": round(rates[ccy] / eurusd, 6)})
    return pairs


def refresh_ecb() -> int:
    """Pull the ECB daily reference rates and persist them."""
    from .store import eod
    try:
        root = ET.fromstring(_get(ECB_URL))
        ns = {"ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
        cube = root.find(".//ecb:Cube/ecb:Cube", ns)
        as_of = cube.get("time") if cube is not None else None
        rates = {c.get("currency"): float(c.get("rate"))
                 for c in (cube.findall("ecb:Cube", ns) if cube is not None else [])}
    except Exception as e:                       # noqa: BLE001
        eod().mark_failed("ecb", f"{type(e).__name__}: {e}")
        raise
    if not as_of or not rates:
        return 0
    return eod().upsert_fx(as_of, rates)


def ecb_fx() -> dict:
    """EUR-based daily reference rates, plus USD-based crosses derived from
    them (EURUSD is the only direct USD quote the ECB publishes).

    Served from the EOD store. The ECB publishes one set of rates a day; there
    is nothing to gain from fetching it on a request.
    """
    from .store import eod
    st = eod()
    as_of, rates = st.fx_latest()
    if as_of is None:
        try:
            refresh_ecb()
        except Exception:
            return {"as_of": None, "base": "EUR", "rates": {}, "usd_pairs": [],
                    "source": "ecb", "origin": "unavailable"}
        as_of, rates = st.fx_latest()
    return {"as_of": as_of, "base": "EUR", "rates": rates,
            "usd_pairs": _usd_pairs(rates), "source": "ecb", "origin": "eod-store"}


def snapshot() -> dict:
    """One call for the Market Data screen. Each source is independent, so a
    single outage degrades one panel instead of blanking the page."""
    out: dict = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "errors": {}}
    for name, fn in (("indices", fred_latest), ("curve", treasury_curve), ("fx", ecb_fx)):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = None
            out["errors"][name] = f"{type(e).__name__}: {e}"[:160]
    return out
