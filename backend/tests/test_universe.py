"""Instrument universe: classification, option tickers, and the stale-series rule.

These are the mistakes that stay plausible. A yield filed as a spread, an index
marked in the wrong currency, or a discontinued series sitting on a live board
all produce numbers that look like numbers -- nothing errors, and the P&L is
simply wrong by an amount nobody can account for.

No network: everything here reads the declared universe. Whether a series still
publishes is checked when it is added, not on every test run -- a suite that
fails because a government website is slow teaches people to ignore it.
"""
from __future__ import annotations

import pytest

from app.core.marketdata import universe as u


def test_symbols_and_series_ids_are_unique():
    """Two instruments on one series id means one of them is silently shadowed."""
    syms = [i.symbol for i in u.UNIVERSE]
    sids = [i.series_id for i in u.UNIVERSE]
    assert len(syms) == len(set(syms)), f"duplicate symbol: {[s for s in syms if syms.count(s) > 1]}"
    assert len(sids) == len(set(sids)), f"duplicate series_id: {[s for s in sids if sids.count(s) > 1]}"


def test_every_risk_factor_is_a_declared_factor():
    """Stress testing maps factor -> shock. An undeclared factor is not shocked
    at all, so the position silently carries no scenario risk."""
    for i in u.UNIVERSE:
        assert i.risk_factor in u.FACTORS, f"{i.symbol} has factor {i.risk_factor}"


def test_only_price_instruments_are_tradable():
    """'100 units of DGS10 at 4.77' is a category error, not a position."""
    for i in u.UNIVERSE:
        assert i.tradable == (i.quote_type == "PRICE"), i.symbol


def test_a_yield_is_not_filed_as_a_spread():
    """AAA_YIELD is an effective yield, not an OAS. Filing it beside the OAS
    series as a SPREAD adds the risk-free rate to the credit factor and
    overstates every credit shock by roughly that much."""
    aaa = u.resolve("AAA_YIELD")
    assert aaa is not None and aaa.quote_type == "RATE"
    for sym in ("AA_OAS", "BBB_OAS", "BB_OAS", "EM_OAS", "HY_OAS", "IG_OAS"):
        assert u.resolve(sym).quote_type == "SPREAD", sym


def test_non_usd_instruments_declare_their_currency():
    """A Nikkei position marked in USD is worth ~150x its actual value, and the
    error reads as a fat finger rather than a currency bug."""
    assert u.resolve("NKY").currency == "JPY"
    for sym, ccy in (("USDJPY", "JPY"), ("USDCAD", "CAD"), ("USDMXN", "MXN"),
                     ("USDINR", "INR")):
        assert u.resolve(sym).currency == ccy, sym


def test_fx_factor_signs_are_not_all_the_same():
    """The FX factor is USD APPRECIATION. It pushes USD/JPY up and EUR/USD down.
    Signing both +1 asserts the dollar rose and fell in one scenario, and nets
    two real exposures into an understated loss."""
    assert u.resolve("EURUSD").factor_sign == -1.0
    assert u.resolve("USDJPY").factor_sign == 1.0
    signs = {i.factor_sign for i in u.UNIVERSE if i.risk_factor == "FX"}
    assert signs == {1.0, -1.0}, "FX should carry both signs"


# ----------------------------------------------------------- option tickers
@pytest.mark.parametrize("ticker", [
    "SPX-C-5000-20261219",
    "SPX-P-4500-20261219",
    "NKY-P-70000-20261219",
    "VIX-C-20-20261219",
    "SPX-C-5000.5-20261219",      # fractional strike
])
def test_valid_option_tickers_parse(ticker):
    assert u.parse_option(ticker) is not None, ticker


def test_underscore_underlyings_parse_as_options():
    """Desk tickers contain underscores (DXY_BROAD, DXY_AFE). The underlying
    group once allowed only [A-Z0-9], so these silently failed to parse --
    resolve() returned None and the ticker read as unknown rather than as 'this
    underlying cannot carry options'. Unambiguous to allow: the field delimiter
    is '-', never '_'."""
    for sym in ("DXY_BROAD", "DXY_AFE"):
        spec = u.parse_option(f"{sym}-C-120-20261219")
        assert spec is not None, f"{sym} should parse as an option underlying"
        assert spec["underlying"] == sym
        assert spec["is_call"] is True


@pytest.mark.parametrize("ticker", [
    "SPX-X-5000-20261219",        # not C or P
    "SPX-C-5000-2026121",         # 7-digit date
    "NOTREAL-C-1-20261219",       # unknown underlying
    "SPX",                        # not an option at all
    "SPX-C--20261219",            # no strike
    "",
])
def test_malformed_option_tickers_are_rejected(ticker):
    """Rejecting is the point. A ticker that half-parses would price a contract
    nobody asked for."""
    assert u.parse_option(ticker) is None, ticker


def test_call_and_put_are_distinguished():
    call = u.parse_option("SPX-C-5000-20261219")
    put = u.parse_option("SPX-P-5000-20261219")
    assert call["is_call"] is True and put["is_call"] is False
    assert u.option_instrument("SPX-C-5000-20261219").name.endswith("exp 2026-12-19")
    assert "Call" in u.option_instrument("SPX-C-5000-20261219").name
    assert "Put" in u.option_instrument("SPX-P-5000-20261219").name


def test_index_options_carry_a_100_multiplier():
    """An index option controls 100 units. Pricing one as a single unit
    understates the position by two orders of magnitude."""
    assert u.parse_option("SPX-C-5000-20261219")["multiplier"] == 100.0
    assert u.parse_option("USDJPY-C-150-20261219")["multiplier"] == 1.0


def test_expired_options_still_resolve_but_are_flagged():
    """A historic fill must still price. Time to expiry floors at zero rather
    than going negative, which would make the pricer return nonsense."""
    spec = u.parse_option("SPX-C-5000-20200101")
    assert spec is not None
    assert spec["expired"] is True
    assert spec["years_to_expiry"] == 0.0


def test_discontinued_series_are_not_in_the_universe():
    """VXOCLS resolves and returns thousands of observations, so it looks like a
    working series -- it was discontinued in 2021. A discontinued series on a
    live board shows a years-old number in the same column as today's. Same for
    the monthly/quarterly commodity indices: real, and not daily marks."""
    banned = {"VXOCLS", "PCOPPUSDM", "PALLFNFINDEXQ", "GOLDAMGBD228NLBM"}
    present = {i.series_id for i in u.UNIVERSE} & banned
    assert not present, f"stale/discontinued series in the universe: {present}"


def test_the_curve_has_no_gap_at_the_butterfly_points():
    """3Y, 7Y and 20Y were missing. They are exactly the points a butterfly
    needs, so the curve could not express one."""
    tenors = {i.symbol for i in u.UNIVERSE if i.symbol.startswith("UST")}
    for t in ("UST1M", "UST3M", "UST6M", "UST1Y", "UST2Y", "UST3Y",
              "UST5Y", "UST7Y", "UST10Y", "UST20Y", "UST30Y"):
        assert t in tenors, f"curve is missing {t}"
