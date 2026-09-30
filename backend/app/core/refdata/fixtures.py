"""A small reference-data fixture set, and a note on where it comes from.

PROVENANCE, because this is reference data and the rules for it are not the rules
for market data. Nothing here is a price, a quote or a volume -- there are no
market observations in this file at all, and the platform's no-fabricated-data
rule is about exactly those. What is here is:

  * IDENTIFIERS (ISIN, CUSIP, ticker) -- public, static, and independently
    checkable: every ISIN and CUSIP below passes its own check digit, which a
    mistyped or invented one would not. `test_fixture_identifiers_validate`
    asserts that, so the fixtures cannot rot into plausible nonsense.
  * SPLIT RATIOS AND EX-DATES -- public corporate-action records, the kind a
    security master is loaded with rather than a feed. Each is labelled with the
    issuer's announcement so it can be checked.
  * ONE ILLUSTRATIVE DIVIDEND and ONE ILLUSTRATIVE TICKER REUSE, both marked
    ILLUSTRATIVE in their note field and both using synthetic identifiers. They
    exist to exercise code paths the real records do not cover. They are not
    claims about any issuer.

This is a fixture, not a vendor feed. A deployment would load a security master
from a provider; the interface is the same, which is the point of having it.
"""
from __future__ import annotations

from datetime import date

from .corporate_actions import ActionType, CorporateAction, CorporateActionBook
from .symbology import (Alias, AssetClass, IdType, Instrument, SecurityMaster)


def _eq(iid: str, name: str, ticker: str, isin: str, cusip: str,
        listed_from: date, exchange: str = "XNAS") -> Instrument:
    return Instrument(
        instrument_id=iid, name=name, asset_class=AssetClass.EQUITY,
        currency="USD", exchange=exchange, tick_size=0.01, lot_size=1.0,
        listed_from=listed_from,
        aliases=[
            Alias(IdType.TICKER, ticker, listed_from),
            Alias(IdType.ISIN, isin, listed_from),
            Alias(IdType.CUSIP, cusip, listed_from),
        ])


def demo_security_master() -> SecurityMaster:
    sm = SecurityMaster()

    sm.add(_eq("QGL-EQ-AAPL", "Apple Inc.", "AAPL",
               "US0378331005", "037833100", date(1980, 12, 12)))
    sm.add(_eq("QGL-EQ-MSFT", "Microsoft Corporation", "MSFT",
               "US5949181045", "594918104", date(1986, 3, 13)))
    sm.add(_eq("QGL-EQ-TSLA", "Tesla, Inc.", "TSLA",
               "US88160R1014", "88160R101", date(2010, 6, 29)))
    sm.add(_eq("QGL-EQ-NVDA", "NVIDIA Corporation", "NVDA",
               "US67066G1040", "67066G104", date(1999, 1, 22)))
    sm.add(_eq("QGL-EQ-AMZN", "Amazon.com, Inc.", "AMZN",
               "US0231351067", "023135106", date(1997, 5, 15)))

    # Alphabet's two share classes: the same issuer, DIFFERENT instruments, and
    # they trade at different prices. One ISIN per class is why ISIN cannot be
    # keyed on the company.
    sm.add(_eq("QGL-EQ-GOOGL", "Alphabet Inc. Class A", "GOOGL",
               "US02079K3059", "02079K305", date(2004, 8, 19)))
    sm.add(_eq("QGL-EQ-GOOG", "Alphabet Inc. Class C", "GOOG",
               "US02079K1079", "02079K107", date(2014, 4, 3)))

    # ETF: same interface, different asset class. SPY is NYSE Arca listed.
    sm.add(Instrument(
        instrument_id="QGL-ETF-SPY", name="SPDR S&P 500 ETF Trust",
        asset_class=AssetClass.ETF, exchange="ARCX",
        listed_from=date(1993, 1, 22),
        aliases=[Alias(IdType.TICKER, "SPY", date(1993, 1, 22)),
                 Alias(IdType.ISIN, "US78462F1030", date(1993, 1, 22)),
                 Alias(IdType.CUSIP, "78462F103", date(1993, 1, 22))]))

    # TICKER LINEAGE, the real case. Meta Platforms changed its ticker from FB to
    # META on 2022-06-09. Same instrument throughout -- same ISIN, same CUSIP,
    # same internal id -- with two ticker aliases whose validity windows do not
    # overlap. `resolve("FB", 2015)` and `resolve("META", 2024)` both give this
    # instrument; `resolve("FB", 2024)` correctly gives nothing, because FB was
    # not this company's ticker then.
    sm.add(Instrument(
        instrument_id="QGL-EQ-META", name="Meta Platforms, Inc.",
        asset_class=AssetClass.EQUITY, listed_from=date(2012, 5, 18),
        aliases=[
            Alias(IdType.TICKER, "FB", date(2012, 5, 18), date(2022, 6, 9)),
            Alias(IdType.TICKER, "META", date(2022, 6, 9)),
            Alias(IdType.ISIN, "US30303M1027", date(2012, 5, 18)),
            Alias(IdType.CUSIP, "30303M102", date(2012, 5, 18)),
        ]))

    # Crypto: no ISIN, no CUSIP, no listing date that means anything, 24/7
    # calendar. Present so the crypto desk resolves through the same master
    # instead of being a special case at every call site.
    sm.add(Instrument(
        instrument_id="QGL-CRYPTO-BTCUSD", name="Bitcoin / US Dollar",
        asset_class=AssetClass.CRYPTO, exchange="CRYPTO",
        tick_size=0.01, listed_from=date(2010, 7, 17),
        aliases=[Alias(IdType.TICKER, "BTC-USD", date(2010, 7, 17)),
                 Alias(IdType.TICKER, "BTCUSD", date(2010, 7, 17))]))
    sm.add(Instrument(
        instrument_id="QGL-CRYPTO-ETHUSD", name="Ether / US Dollar",
        asset_class=AssetClass.CRYPTO, exchange="CRYPTO",
        tick_size=0.01, listed_from=date(2015, 8, 7),
        aliases=[Alias(IdType.TICKER, "ETH-USD", date(2015, 8, 7)),
                 Alias(IdType.TICKER, "ETHUSD", date(2015, 8, 7))]))

    # ILLUSTRATIVE TICKER REUSE, with synthetic identifiers. Two unrelated
    # instruments share the ticker "ZZZ" in non-overlapping windows. This is the
    # case that makes ticker unusable as a key, and it needs a fixture because
    # the real examples of it are exactly the ones nobody has clean records for.
    sm.add(Instrument(
        instrument_id="QGL-DEMO-OLDCO", name="Oldco Industries (ILLUSTRATIVE)",
        listed_from=date(1998, 1, 2), listed_to=date(2009, 3, 2),
        aliases=[Alias(IdType.TICKER, "ZZZ", date(1998, 1, 2), date(2009, 3, 2))]))
    sm.add(Instrument(
        instrument_id="QGL-DEMO-NEWCO", name="Newco Holdings (ILLUSTRATIVE)",
        listed_from=date(2014, 6, 2),
        aliases=[Alias(IdType.TICKER, "ZZZ", date(2014, 6, 2))]))

    return sm


def demo_corporate_actions() -> CorporateActionBook:
    """Public split records, plus one illustrative dividend.

    Ratios are stated as NEW shares per OLD share, so a 4-for-1 is 4.0.
    """
    book = CorporateActionBook()

    # Splits: public corporate-action records, each announced by the issuer.
    book.add(CorporateAction("QGL-EQ-AAPL", ActionType.SPLIT, date(2014, 6, 9),
                             ratio=7.0, note="Apple 7-for-1 split."))
    book.add(CorporateAction("QGL-EQ-AAPL", ActionType.SPLIT, date(2020, 8, 31),
                             ratio=4.0, note="Apple 4-for-1 split."))
    book.add(CorporateAction("QGL-EQ-TSLA", ActionType.SPLIT, date(2020, 8, 31),
                             ratio=5.0, note="Tesla 5-for-1 split."))
    book.add(CorporateAction("QGL-EQ-TSLA", ActionType.SPLIT, date(2022, 8, 25),
                             ratio=3.0, note="Tesla 3-for-1 split."))
    book.add(CorporateAction("QGL-EQ-NVDA", ActionType.SPLIT, date(2021, 7, 20),
                             ratio=4.0, note="NVIDIA 4-for-1 split."))
    book.add(CorporateAction("QGL-EQ-NVDA", ActionType.SPLIT, date(2024, 6, 10),
                             ratio=10.0, note="NVIDIA 10-for-1 split."))
    book.add(CorporateAction("QGL-EQ-AMZN", ActionType.SPLIT, date(2022, 6, 6),
                             ratio=20.0, note="Amazon 20-for-1 split."))
    book.add(CorporateAction("QGL-EQ-GOOGL", ActionType.SPLIT, date(2022, 7, 18),
                             ratio=20.0, note="Alphabet Class A 20-for-1 split."))
    book.add(CorporateAction("QGL-EQ-GOOG", ActionType.SPLIT, date(2022, 7, 18),
                             ratio=20.0, note="Alphabet Class C 20-for-1 split."))

    # ILLUSTRATIVE dividend on the synthetic instrument, so the cash-entitlement
    # and ex-date/pay-date paths have a fixture without attributing an amount to
    # a real issuer. Note the gap between the dates: that gap is the receivable.
    book.add(CorporateAction(
        "QGL-DEMO-NEWCO", ActionType.CASH_DIVIDEND, date(2025, 2, 10),
        cash_amount=0.25, currency="USD", pay_date=date(2025, 2, 28),
        announcement_date=date(2025, 1, 15),
        note="ILLUSTRATIVE. Not a real distribution by any issuer; present so the "
             "ex-date/pay-date receivable path has a fixture."))

    return book
