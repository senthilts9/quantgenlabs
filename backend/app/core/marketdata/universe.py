"""Instrument reference data — the desk's instrument master.

Three separate modules used to hardcode their own two-instrument view of the
world (`{"SP500": ..., "VIX": ...}`): the OMS reference-price lookup, the
portfolio risk series map, and the stress-test factor map. They drifted, and
a symbol known to one was invisible to the others. This is the single source
of truth all three now read.

Two distinctions here do real work and are the reason this is reference data
rather than a dict of tickers:

  QUOTE TYPE. A PRICE instrument marks as qty x price. A RATE does not --
  "100 units of DGS10 at 4.77" is not a position, it is a category error. The
  10Y yield enters a book through DV01 on an actual bond, not through
  notional. RATE and SPREAD instruments are therefore NOT tradable in the
  share-based OMS; they are risk factors, and the FI screen prices the bonds
  that carry them. Marking a rate as a price is the kind of bug that produces
  a confident, completely wrong P&L.

  QUOTE CONVENTION. FRED publishes DEXUSEU as dollars-per-euro (EUR/USD) but
  DEXJPUS as yen-per-dollar (USD/JPY). Inverting one of them is the classic
  FX sign error. Rather than carry an `invert` flag that someone eventually
  sets wrong, each symbol is NAMED for the convention its series already
  uses, so the quote is right by construction and there is nothing to invert.

LATENCY CLASS is stated per instrument and never inferred. FRED is
end-of-day and publishes with a lag that differs by series -- the FX block
routinely trails the equity block by several business days. Downstream code
must be able to see that instead of assuming every mark is today's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Risk factors that stress.py shocks. Every instrument maps to exactly one.
FACTORS = ("EQUITY", "VOL", "RATES", "CREDIT", "FX", "COMMODITY", "CRYPTO")


@dataclass(frozen=True)
class Instrument:
    symbol: str          # desk ticker
    name: str
    asset_class: str
    source: str          # "fred" | "ccxt"
    series_id: str       # provider-side identifier
    quote_type: str      # PRICE | RATE | SPREAD
    risk_factor: str
    unit: str = ""
    currency: str = "USD"
    latency_class: str = "EOD"   # EOD | REALTIME
    # Signed exposure to `risk_factor`, for stress testing. Only FX needs
    # anything other than +1: the FX factor is defined as USD APPRECIATION,
    # which pushes USD/JPY up but EUR/USD down. Shocking both in the same
    # direction would assert that the dollar rose against the yen and fell
    # against the euro in the same scenario -- internally contradictory, and
    # it would net two real exposures into an understated loss.
    factor_sign: float = 1.0

    @property
    def tradable(self) -> bool:
        """Only PRICE instruments can be held as qty x price in the OMS."""
        return self.quote_type == "PRICE"


def _i(*a, **k) -> Instrument:
    return Instrument(*a, **k)


# Every series below was verified to return a live value from the deployment's
# own egress IP. Series that 404 or were discontinued (WILL5000IND,
# GOLDAMGBD228NLBM) and monthly-only series are deliberately absent: a board
# that shows a stale monthly print next to a daily close invites the reader to
# compare two things that are not comparable.
UNIVERSE: tuple[Instrument, ...] = (
    # --- equity indices (tradable as index units, CFD/future-style) -------
    _i("SPX", "S&P 500", "EQUITY_INDEX", "fred", "SP500", "PRICE", "EQUITY", "index"),
    _i("NDX", "Nasdaq 100", "EQUITY_INDEX", "fred", "NASDAQ100", "PRICE", "EQUITY", "index"),
    _i("DJI", "Dow Jones Industrial", "EQUITY_INDEX", "fred", "DJIA", "PRICE", "EQUITY", "index"),
    # Quoted in JPY, not USD. Marking it as USD would value a Nikkei position
    # at roughly 150x its worth and the error looks like a fat finger, not a
    # currency bug.
    _i("NKY", "Nikkei 225", "EQUITY_INDEX", "fred", "NIKKEI225", "PRICE", "EQUITY", "index", "JPY"),

    # --- volatility -------------------------------------------------------
    _i("VIX", "CBOE Volatility Index", "VOLATILITY", "fred", "VIXCLS", "PRICE", "VOL", "vol pts"),
    _i("VXN", "Nasdaq 100 Volatility", "VOLATILITY", "fred", "VXNCLS", "PRICE", "VOL", "vol pts"),
    _i("VXD", "Dow Volatility", "VOLATILITY", "fred", "VXDCLS", "PRICE", "VOL", "vol pts"),
    _i("OVX", "Crude Oil Volatility", "VOLATILITY", "fred", "OVXCLS", "PRICE", "VOL", "vol pts"),
    _i("GVZ", "Gold Volatility", "VOLATILITY", "fred", "GVZCLS", "PRICE", "VOL", "vol pts"),
    _i("RVX", "Russell 2000 Volatility", "VOLATILITY", "fred", "RVXCLS", "PRICE", "VOL", "vol pts"),
    # VXOCLS (the old S&P 100 vol index) is deliberately NOT here. It resolves
    # and returns 9,321 observations, so it looks like a working series -- but
    # it was discontinued and its last print is 2021-09-23. On a live board a
    # discontinued series shows a five-year-old number in the same column as
    # today's, which is worse than not carrying it. Same reason excludes the
    # monthly copper and quarterly all-commodity indices: both resolve, both
    # are months stale, and neither belongs beside a daily mark.

    # --- govt curve (RATE: risk factors, not share-tradable) --------------
    _i("UST1M", "US 1-Month Bill", "RATE", "fred", "DGS1MO", "RATE", "RATES", "%"),
    _i("UST3M", "US 3-Month Bill", "RATE", "fred", "DGS3MO", "RATE", "RATES", "%"),
    _i("UST6M", "US 6-Month Bill", "RATE", "fred", "DGS6MO", "RATE", "RATES", "%"),
    _i("UST1Y", "US 1-Year Note", "RATE", "fred", "DGS1", "RATE", "RATES", "%"),
    _i("UST2Y", "US 2-Year Note", "RATE", "fred", "DGS2", "RATE", "RATES", "%"),
    _i("UST5Y", "US 5-Year Note", "RATE", "fred", "DGS5", "RATE", "RATES", "%"),
    _i("UST10Y", "US 10-Year Note", "RATE", "fred", "DGS10", "RATE", "RATES", "%"),
    _i("UST30Y", "US 30-Year Bond", "RATE", "fred", "DGS30", "RATE", "RATES", "%"),
    _i("UST3Y", "US 3-Year Note", "RATE", "fred", "DGS3", "RATE", "RATES", "%"),
    _i("UST7Y", "US 7-Year Note", "RATE", "fred", "DGS7", "RATE", "RATES", "%"),
    _i("UST20Y", "US 20-Year Bond", "RATE", "fred", "DGS20", "RATE", "RATES", "%"),
    # Policy and bank funding, so a curve shock has something to anchor the
    # front end against rather than inferring it from the 1M bill.
    _i("FEDTGT", "Fed Funds Target (upper)", "RATE", "fred", "DFEDTARU", "RATE", "RATES", "%"),
    _i("PRIME", "Bank Prime Loan Rate", "RATE", "fred", "DPRIME", "RATE", "RATES", "%"),
    _i("FWD5Y5Y", "5y5y Forward Inflation", "RATE", "fred", "T5YIFR", "SPREAD", "RATES", "%"),
    _i("CURVE_10Y2Y", "10Y-2Y Slope", "RATE", "fred", "T10Y2Y", "SPREAD", "RATES", "%"),
    _i("CURVE_10Y3M", "10Y-3M Slope", "RATE", "fred", "T10Y3M", "SPREAD", "RATES", "%"),
    _i("REAL10Y", "10Y TIPS Real Yield", "RATE", "fred", "DFII10", "RATE", "RATES", "%"),
    _i("BE10Y", "10Y Breakeven Inflation", "RATE", "fred", "T10YIE", "SPREAD", "RATES", "%"),
    _i("BE5Y", "5Y Breakeven Inflation", "RATE", "fred", "T5YIE", "SPREAD", "RATES", "%"),

    # --- money market / policy -------------------------------------------
    _i("FEDFUNDS", "Fed Funds (effective)", "RATE", "fred", "DFF", "RATE", "RATES", "%"),
    _i("SOFR", "Secured Overnight Financing", "RATE", "fred", "SOFR", "RATE", "RATES", "%"),
    _i("EFFR", "Effective Fed Funds", "RATE", "fred", "EFFR", "RATE", "RATES", "%"),
    _i("MTG30Y", "30Y Fixed Mortgage", "RATE", "fred", "MORTGAGE30US", "RATE", "RATES", "%"),

    # --- credit spreads ---------------------------------------------------
    _i("HY_OAS", "US High Yield OAS", "CREDIT", "fred", "BAMLH0A0HYM2", "SPREAD", "CREDIT", "%"),
    _i("IG_OAS", "US Investment Grade OAS", "CREDIT", "fred", "BAMLC0A0CM", "SPREAD", "CREDIT", "%"),
    _i("CCC_OAS", "US CCC & Lower OAS", "CREDIT", "fred", "BAMLH0A3HYC", "SPREAD", "CREDIT", "%"),
    # The rating ladder, so a credit shock can widen by quality instead of
    # moving one aggregate. AAA->AA->BBB->BB->CCC is where compression and
    # decompression actually show up; with three points it cannot.
    _i("AA_OAS", "US AA Corporate OAS", "CREDIT", "fred", "BAMLC0A2CAA", "SPREAD", "CREDIT", "%"),
    _i("BBB_OAS", "US BBB Corporate OAS", "CREDIT", "fred", "BAMLC0A4CBBB", "SPREAD", "CREDIT", "%"),
    _i("BB_OAS", "US BB High Yield OAS", "CREDIT", "fred", "BAMLH0A1HYBB", "SPREAD", "CREDIT", "%"),
    _i("EM_OAS", "EM Corporate OAS", "CREDIT", "fred", "BAMLEMCBPIOAS", "SPREAD", "CREDIT", "%"),
    # A YIELD, not a spread -- hence RATE. Filing an effective yield as a
    # SPREAD would add a 5.8% "spread" to the credit factor and overstate
    # every credit shock by roughly the risk-free rate.
    _i("AAA_YIELD", "US AAA Corporate Yield", "CREDIT", "fred", "BAMLC0A1CAAAEY",
       "RATE", "CREDIT", "%"),

    # --- FX. Each symbol is named for the convention FRED already uses ----
    # USD is the QUOTE currency here: a stronger dollar sends these DOWN.
    _i("EURUSD", "Euro / US Dollar", "FX", "fred", "DEXUSEU", "PRICE", "FX",
       "USD per EUR", factor_sign=-1.0),
    _i("GBPUSD", "Sterling / US Dollar", "FX", "fred", "DEXUSUK", "PRICE", "FX",
       "USD per GBP", factor_sign=-1.0),
    _i("AUDUSD", "Aussie / US Dollar", "FX", "fred", "DEXUSAL", "PRICE", "FX",
       "USD per AUD", factor_sign=-1.0),
    # USD is the BASE currency below: a stronger dollar sends these UP.
    _i("USDJPY", "US Dollar / Yen", "FX", "fred", "DEXJPUS", "PRICE", "FX", "JPY per USD", "JPY"),
    _i("USDCAD", "US Dollar / Can Dollar", "FX", "fred", "DEXCAUS", "PRICE", "FX", "CAD per USD", "CAD"),
    _i("USDCHF", "US Dollar / Swiss Franc", "FX", "fred", "DEXSZUS", "PRICE", "FX", "CHF per USD", "CHF"),
    _i("USDCNY", "US Dollar / Renminbi", "FX", "fred", "DEXCHUS", "PRICE", "FX", "CNY per USD", "CNY"),
    _i("USDMXN", "US Dollar / Mexican Peso", "FX", "fred", "DEXMXUS", "PRICE", "FX", "MXN per USD", "MXN"),
    _i("USDINR", "US Dollar / Indian Rupee", "FX", "fred", "DEXINUS", "PRICE", "FX", "INR per USD", "INR"),
    _i("DXY_BROAD", "USD Broad Index", "FX", "fred", "DTWEXBGS", "PRICE", "FX", "index"),
    _i("DXY_AFE", "USD vs Advanced Economies", "FX", "fred", "DTWEXAFEGS", "PRICE", "FX", "index"),

    # --- commodities ------------------------------------------------------
    _i("WTI", "WTI Crude", "COMMODITY", "fred", "DCOILWTICO", "PRICE", "COMMODITY", "USD/bbl"),
    _i("BRENT", "Brent Crude", "COMMODITY", "fred", "DCOILBRENTEU", "PRICE", "COMMODITY", "USD/bbl"),
    _i("NATGAS", "Henry Hub Natural Gas", "COMMODITY", "fred", "DHHNGSP", "PRICE", "COMMODITY", "USD/MMBtu"),

    # --- crypto. The only genuinely sub-second source on the platform. ----
    _i("BTCUSDT", "Bitcoin", "CRYPTO", "ccxt", "BTC/USDT", "PRICE", "CRYPTO",
       "USDT", "USDT", "REALTIME"),
    _i("ETHUSDT", "Ether", "CRYPTO", "ccxt", "ETH/USDT", "PRICE", "CRYPTO",
       "USDT", "USDT", "REALTIME"),
    _i("SOLUSDT", "Solana", "CRYPTO", "ccxt", "SOL/USDT", "PRICE", "CRYPTO",
       "USDT", "USDT", "REALTIME"),
    _i("XRPUSDT", "XRP", "CRYPTO", "ccxt", "XRP/USDT", "PRICE", "CRYPTO",
       "USDT", "USDT", "REALTIME"),
    # Tokenised gold, 1 token = 1 fine troy ounce. FRED's London fix series
    # was discontinued, so this is the platform's only live gold mark -- and
    # it carries PAX issuer credit risk, which a real desk would not net
    # against an LBMA fix. Labelled CRYPTO, not COMMODITY, for that reason.
    _i("PAXGUSDT", "PAX Gold (tokenised oz)", "CRYPTO", "ccxt", "PAXG/USDT",
       "PRICE", "CRYPTO", "USDT", "USDT", "REALTIME"),
)

BY_SYMBOL: dict[str, Instrument] = {i.symbol: i for i in UNIVERSE}
BY_SERIES: dict[str, Instrument] = {i.series_id: i for i in UNIVERSE}

# Legacy tickers that already exist in the persisted OMS book and in saved
# orders. Dropping them would orphan real positions, so they resolve rather
# than 404 -- renaming an instrument must never strand a holding.
ALIASES = {"SP500": "SPX", "SPY": "SPX", "VIXCLS": "VIX", "NDX100": "NDX",
           "XRP-PERP": "XRPUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT"}


# Listed options on the underlyings above, so the book can carry CONVEXITY.
# Without them every position is linear, portfolio gamma and vega are
# identically zero, and the Greeks are arithmetically true but say nothing.
#
# Format: UNDERLYING-C|P-STRIKE-YYYYMMDD   e.g. SPX-P-7500-20270115
# Deliberately parsed rather than enumerated: the option chain is combinatorial
# and listing every strike/expiry in a static tuple is exactly the kind of
# hardcoding this module exists to remove.
# The underlying group allows "_" because desk tickers contain it (DXY_BROAD,
# DXY_AFE). It is unambiguous: the field delimiter is "-", never "_". Without it
# those symbols silently fail to parse as options -- resolve() just returns None,
# so the ticker reads as unknown rather than as "this underlying cannot have
# options", which is a confusing way to find out.
_OPT_RE = re.compile(r"^([A-Z0-9_]+)-([CP])-([0-9]+(?:\.[0-9]+)?)-(\d{8})$")


def parse_option(symbol: str) -> dict | None:
    """Option ticker -> contract spec, or None if it is not one."""
    m = _OPT_RE.match((symbol or "").upper().strip())
    if not m:
        return None
    und, cp, strike, expiry = m.groups()
    base = BY_SYMBOL.get(und) or BY_SYMBOL.get(ALIASES.get(und, ""))
    if base is None:
        return None
    try:
        exp = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
    except ValueError:
        return None
    # Actual/365 to expiry. Expired contracts are allowed to resolve so a
    # historic fill still prices; time to expiry simply floors at zero.
    years = max((exp - date.today()).days, 0) / 365.0
    return {"underlying": base.symbol, "is_call": cp == "C",
            "strike": float(strike), "expiry": exp.isoformat(),
            "years_to_expiry": years, "expired": years <= 0,
            "multiplier": 100.0 if base.asset_class == "EQUITY_INDEX" else 1.0}


def option_instrument(symbol: str) -> Instrument | None:
    """Synthetic Instrument for an option ticker, inheriting its underlying."""
    spec = parse_option(symbol)
    if spec is None:
        return None
    base = BY_SYMBOL[spec["underlying"]]
    kind = "Call" if spec["is_call"] else "Put"
    return Instrument(
        symbol=symbol.upper().strip(),
        name=f"{base.name} {kind} {spec['strike']:g} exp {spec['expiry']}",
        asset_class="OPTION", source=base.source, series_id=base.series_id,
        quote_type="PRICE", risk_factor=base.risk_factor,
        unit=f"premium ({base.currency})", currency=base.currency,
        latency_class=base.latency_class, factor_sign=base.factor_sign)


def resolve(symbol: str) -> Instrument | None:
    """Desk ticker, legacy alias, raw provider series id, or option -> Instrument."""
    if not symbol:
        return None
    s = symbol.upper().strip()
    return (BY_SYMBOL.get(s) or BY_SYMBOL.get(ALIASES.get(s, ""))
            or BY_SERIES.get(s) or option_instrument(s))


def tradable() -> list[Instrument]:
    return [i for i in UNIVERSE if i.tradable]


def by_factor(factor: str) -> list[Instrument]:
    return [i for i in UNIVERSE if i.risk_factor == factor.upper()]


def factor_map() -> dict[str, str]:
    """symbol -> risk factor, including aliases. Consumed by stress testing."""
    m = {i.symbol: i.risk_factor for i in UNIVERSE}
    m.update({a: BY_SYMBOL[t].risk_factor for a, t in ALIASES.items() if t in BY_SYMBOL})
    return m


def factor_signs() -> dict[str, float]:
    """symbol -> signed exposure to its risk factor, including aliases."""
    m = {i.symbol: i.factor_sign for i in UNIVERSE}
    m.update({a: BY_SYMBOL[t].factor_sign for a, t in ALIASES.items() if t in BY_SYMBOL})
    return m


def series_map() -> dict[str, str]:
    """symbol -> FRED series id, for history lookups. FRED-sourced only."""
    m = {i.symbol: i.series_id for i in UNIVERSE if i.source == "fred"}
    m.update({a: BY_SYMBOL[t].series_id for a, t in ALIASES.items()
              if t in BY_SYMBOL and BY_SYMBOL[t].source == "fred"})
    return m


# ---------------------------------------------------------------------------
# Asset-class boundary
# ---------------------------------------------------------------------------
# Digital assets are a separate desk with separate venues, separate settlement,
# separate custody and 24/7 hours. Showing BTCUSDT in the same blotter row set
# as EURUSD and SPX is not a tidiness complaint - the two are not comparable
# positions, the marks come from different infrastructure with different
# latency classes, and a risk number computed across both silently assumes a
# correlation regime that does not hold through a crypto drawdown.
#
# The classification already existed on every instrument. What was missing was
# anything that *read* it, so crypto leaked into the FX/rates/credit screens.
# Both directions are enforced here, in one place, rather than as a filter
# repeated (and eventually forgotten) at each call site.

CRYPTO_ASSET_CLASS = "CRYPTO"


def is_crypto(symbol: str) -> bool:
    """True if `symbol` belongs to the digital-asset desk.

    Falls back to a shape test for symbols not in the master, because the OMS
    carries venue-native tickers (`BTCUSDT`) that never pass through the
    reference data. An unknown symbol is treated as non-crypto, which keeps
    traditional screens working when something new appears.
    """
    inst = BY_SYMBOL.get(symbol) or BY_SERIES.get(symbol)
    if inst is not None:
        return inst.asset_class == CRYPTO_ASSET_CLASS
    upper = symbol.upper().replace("/", "").replace("-", "")
    bases = ("BTC", "ETH", "SOL", "XRP", "PAXG", "USDT", "USDC", "DOGE", "ADA")
    return any(upper.startswith(b) or upper.endswith(b) for b in bases)


def crypto_symbols() -> set:
    """Every symbol the digital-asset desk owns, in both naming conventions."""
    out = set()
    for i in UNIVERSE:
        if i.asset_class == CRYPTO_ASSET_CLASS:
            out.add(i.symbol)
            out.add(i.series_id)
            out.add(i.symbol.replace("/", ""))
    return out


def split_by_desk(symbols) -> tuple:
    """Partition symbols into (traditional, digital_assets).

    Returning both halves rather than filtering one away means a caller cannot
    silently drop instruments it did not mean to - the counts have to add up.
    """
    trad, crypto = [], []
    for s in symbols:
        (crypto if is_crypto(s) else trad).append(s)
    return trad, crypto


def to_dict(i: Instrument) -> dict:
    return {"symbol": i.symbol, "name": i.name, "asset_class": i.asset_class,
            "source": i.source, "series_id": i.series_id,
            "quote_type": i.quote_type, "risk_factor": i.risk_factor,
            "unit": i.unit, "currency": i.currency,
            "latency_class": i.latency_class, "tradable": i.tradable,
            "factor_sign": i.factor_sign}
