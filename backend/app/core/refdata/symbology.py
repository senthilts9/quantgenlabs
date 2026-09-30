"""Security master and symbology.

THE TRAP THIS EXISTS TO AVOID: a ticker is not an identifier. It is a display
label that exchanges reuse. When a company delists, its ticker goes back in the
pool and is handed to something else — "TWTR" meant Twitter until 2022 and had
meant Tweeter Home Entertainment before that. A position keyed on ticker and
carried across that boundary is a position in the wrong company, and nothing in
the system notices because the string still matches.

So resolution is ALWAYS AS-OF A DATE. `resolve("TWTR", date(2015,1,1))` and
`resolve("TWTR", date(2024,1,1))` are different questions and may have different
answers. An API that resolves a ticker without a date is an API that will
silently be wrong eventually, which is why there is no such call here.

WHICH IDENTIFIER MEANS WHAT, because they are not interchangeable and picking the
wrong one is a data-quality incident rather than an inconvenience:

  ISIN   — 12 chars, globally unique per SECURITY, issued once. The right key for
           holdings and settlement. Does NOT distinguish listing venue: one ISIN
           can trade on several exchanges at different prices.
  CUSIP  — 9 chars, North America only. Superseded by ISIN for cross-border.
  SEDOL  — 7 chars, per exchange-and-currency, so it DOES distinguish listings.
  FIGI   — 12 chars, openly licensed, and unlike ISIN it is issued per listing,
           which makes it the best key when venue matters.
  Ticker — display only. Never a key.

The internal `instrument_id` is the actual primary key. Every external identifier
is an alias with a validity window onto it, because all of them change: tickers
are reused, ISINs are reissued after corporate actions, and a security that moves
exchange gets a new SEDOL while keeping its ISIN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class IdType(str, Enum):
    TICKER = "ticker"
    ISIN = "isin"
    CUSIP = "cusip"
    SEDOL = "sedol"
    FIGI = "figi"
    RIC = "ric"                  # Refinitiv Instrument Code
    BLOOMBERG = "bloomberg"


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    FUTURE = "future"
    OPTION = "option"
    FX = "fx"
    BOND = "bond"
    CRYPTO = "crypto"


# Far-future sentinel for "still current". A None end-date would need a null
# check at every comparison and one of them will be forgotten.
OPEN_ENDED = date(9999, 12, 31)


@dataclass(frozen=True)
class Alias:
    id_type: IdType
    value: str
    valid_from: date
    valid_to: date = OPEN_ENDED

    def active_on(self, d: date) -> bool:
        # Half-open interval [from, to): a security that changes ticker on a date
        # must not resolve to both the old and new one on that date.
        return self.valid_from <= d < self.valid_to


@dataclass
class Instrument:
    instrument_id: str                  # internal, stable, never reused
    name: str
    asset_class: AssetClass = AssetClass.EQUITY
    currency: str = "USD"
    exchange: str = "XNAS"
    tick_size: float = 0.01
    lot_size: float = 1.0
    contract_size: float = 1.0
    # A security that has been delisted must stop resolving for new trading while
    # remaining resolvable for historical positions — hence a date, not a flag.
    listed_from: date = date(1900, 1, 1)
    listed_to: date = OPEN_ENDED
    aliases: list[Alias] = field(default_factory=list)

    def tradable_on(self, d: date) -> bool:
        return self.listed_from <= d < self.listed_to

    def alias(self, id_type: IdType, on: date) -> str | None:
        for a in self.aliases:
            if a.id_type is id_type and a.active_on(on):
                return a.value
        return None


class SymbologyError(LookupError):
    pass


class AmbiguousSymbol(SymbologyError):
    """More than one instrument matches. Better to refuse than to guess."""


class SecurityMaster:
    def __init__(self) -> None:
        self._instruments: dict[str, Instrument] = {}
        # (id_type, value) -> list of (instrument_id, alias). A list because the
        # same ticker legitimately maps to different instruments over time.
        self._index: dict[tuple[IdType, str], list[tuple[str, Alias]]] = {}

    # ------------------------------------------------------------- loading
    def add(self, inst: Instrument) -> Instrument:
        if inst.instrument_id in self._instruments:
            raise SymbologyError(f"instrument_id '{inst.instrument_id}' already exists")
        self._instruments[inst.instrument_id] = inst
        for a in inst.aliases:
            self._add_alias_index(inst.instrument_id, a)
        return inst

    def add_alias(self, instrument_id: str, id_type: IdType, value: str,
                  valid_from: date, valid_to: date = OPEN_ENDED) -> Alias:
        inst = self._instruments.get(instrument_id)
        if inst is None:
            raise SymbologyError(f"unknown instrument_id '{instrument_id}'")
        # Two live aliases of the same type would make "the ticker" ambiguous for
        # the instrument itself, not just for lookup.
        for existing in inst.aliases:
            if existing.id_type is not id_type:
                continue
            if existing.valid_from < valid_to and valid_from < existing.valid_to:
                raise SymbologyError(
                    f"{instrument_id} already has an overlapping {id_type.value} "
                    f"alias '{existing.value}' valid {existing.valid_from}..{existing.valid_to}")
        a = Alias(id_type, value, valid_from, valid_to)
        inst.aliases.append(a)
        self._add_alias_index(instrument_id, a)
        return a

    def _add_alias_index(self, instrument_id: str, a: Alias) -> None:
        self._index.setdefault((a.id_type, a.value.upper()), []).append((instrument_id, a))

    # ---------------------------------------------------------- resolution
    def resolve(self, value: str, on: date, id_type: IdType | None = None) -> Instrument:
        """Resolve an identifier AS OF a date.

        The date is required, not optional with a today() default. A ticker
        resolved without one is resolved against whatever today happens to be,
        which for historical data is the wrong answer delivered confidently.
        """
        key_types = [id_type] if id_type else list(IdType)
        hits: list[tuple[str, Alias]] = []
        for t in key_types:
            for iid, a in self._index.get((t, value.upper()), []):
                if a.active_on(on):
                    hits.append((iid, a))

        if not hits:
            raise SymbologyError(
                f"'{value}' does not resolve on {on}. It may not have existed "
                f"yet, may have been delisted, or may belong to another "
                f"identifier type.")
        distinct = {iid for iid, _ in hits}
        if len(distinct) > 1:
            raise AmbiguousSymbol(
                f"'{value}' resolves to {sorted(distinct)} on {on}. Refusing to "
                f"guess -- pass id_type to disambiguate.")
        return self._instruments[hits[0][0]]

    def try_resolve(self, value: str, on: date,
                    id_type: IdType | None = None) -> Instrument | None:
        try:
            return self.resolve(value, on, id_type)
        except SymbologyError:
            return None

    def get(self, instrument_id: str) -> Instrument:
        inst = self._instruments.get(instrument_id)
        if inst is None:
            raise SymbologyError(f"unknown instrument_id '{instrument_id}'")
        return inst

    def cross_reference(self, value: str, on: date,
                        id_type: IdType | None = None) -> dict[str, str]:
        """Every identifier this instrument carried on that date."""
        inst = self.resolve(value, on, id_type)
        return {a.id_type.value: a.value for a in inst.aliases if a.active_on(on)}

    def history(self, instrument_id: str, id_type: IdType) -> list[Alias]:
        """Every value this instrument has had for one identifier type, in order.
        This is the lineage: it answers "what was this called in 2019"."""
        inst = self.get(instrument_id)
        return sorted((a for a in inst.aliases if a.id_type is id_type),
                      key=lambda a: a.valid_from)

    def all_instruments(self) -> list[Instrument]:
        return list(self._instruments.values())

    def __len__(self) -> int:
        return len(self._instruments)


# ------------------------------------------------------------- validation
def valid_isin(isin: str) -> bool:
    """ISIN check digit, Luhn over the alphabetic-expanded body.

    Worth validating on the way in: a transposed ISIN is a valid-looking string
    that resolves to nothing, and finding that out at settlement rather than at
    booking is expensive.
    """
    s = isin.strip().upper()
    if len(s) != 12 or not s[:2].isalpha() or not s[-1].isdigit():
        return False
    expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in s[:-1])
    total, double = 0, True
    for ch in reversed(expanded):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return (10 - total % 10) % 10 == int(s[-1])


def valid_cusip(cusip: str) -> bool:
    s = cusip.strip().upper()
    if len(s) != 9:
        return False
    total = 0
    for i, ch in enumerate(s[:8]):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch) - 55
        elif ch == "*":
            v = 36
        elif ch == "@":
            v = 37
        elif ch == "#":
            v = 38
        else:
            return False
        if i % 2:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(s[8]) if s[8].isdigit() else False
