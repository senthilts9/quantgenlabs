"""Reference data: symbology, calendars, corporate actions, reconciliation, batch.

The tests that matter here are the ones that assert a WRONG answer is refused
rather than returned. A symbology layer that resolves everything, a calendar that
treats T+2 as two days, and a split that adjusts positions but not history all
produce output that looks fine -- so most of what follows is about the cases where
looking fine is the failure mode.
"""
from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.core.refdata import (ActionType, Alias, AmbiguousSymbol, AlreadyRun,
                              CorporateAction, CorporateActionBook,
                              CorporateActionError, DailyCycle, DailyCycleError,
                              Holding, IdType, Instrument, Phase, Position,
                              SecurityMaster, Severity, SymbologyError,
                              TradingCalendar, crypto_calendar,
                              reconcile_cash, reconcile_positions,
                              reconcile_trades, us_equity_calendar,
                              us_equity_holidays, valid_cusip, valid_isin)
from app.core.refdata.fixtures import demo_corporate_actions, demo_security_master


# ========================================================= symbology
def test_resolution_is_as_of_a_date_and_a_reused_ticker_gives_both_answers():
    """The central claim of the module. The same string, two dates, two
    different instruments -- and the gap between them resolves to neither."""
    sm = demo_security_master()
    assert sm.resolve("ZZZ", date(2000, 1, 3)).instrument_id == "QGL-DEMO-OLDCO"
    assert sm.resolve("ZZZ", date(2020, 1, 3)).instrument_id == "QGL-DEMO-NEWCO"
    # Between the delisting and the reissue it belongs to nobody. Returning
    # either one here would be the bug.
    assert sm.try_resolve("ZZZ", date(2011, 6, 1)) is None


def test_ticker_change_keeps_one_instrument_with_two_windows():
    sm = demo_security_master()
    meta = "QGL-EQ-META"
    assert sm.resolve("FB", date(2015, 1, 2)).instrument_id == meta
    assert sm.resolve("META", date(2024, 1, 2)).instrument_id == meta
    # FB is not this company's ticker in 2024. Resolving it anyway is how a
    # backtest silently keys 2024 data onto a 2015 symbol.
    assert sm.try_resolve("FB", date(2024, 1, 2)) is None
    # ISIN is unchanged across the ticker change: that is the point of ISIN.
    assert sm.resolve("US30303M1027", date(2015, 1, 2)).instrument_id == meta
    assert sm.resolve("US30303M1027", date(2024, 1, 2)).instrument_id == meta


def test_alias_windows_are_half_open_so_the_change_date_is_unambiguous():
    sm = demo_security_master()
    change = date(2022, 6, 9)
    assert sm.try_resolve("FB", change) is None          # last day is exclusive
    assert sm.resolve("META", change).instrument_id == "QGL-EQ-META"
    assert sm.resolve("FB", date(2022, 6, 8)).instrument_id == "QGL-EQ-META"


def test_overlapping_alias_of_the_same_type_is_refused():
    sm = SecurityMaster()
    sm.add(Instrument("X", "X Corp",
                      aliases=[Alias(IdType.TICKER, "XX", date(2000, 1, 1),
                                     date(2010, 1, 1))]))
    with pytest.raises(SymbologyError):
        sm.add_alias("X", IdType.TICKER, "XY", date(2005, 1, 1))
    # Non-overlapping is fine.
    sm.add_alias("X", IdType.TICKER, "XY", date(2010, 1, 1))


def test_ambiguous_symbol_refuses_rather_than_picking():
    sm = SecurityMaster()
    sm.add(Instrument("A", "A", aliases=[Alias(IdType.TICKER, "DUP", date(2000, 1, 1))]))
    sm.add(Instrument("B", "B", aliases=[Alias(IdType.SEDOL, "DUP", date(2000, 1, 1))]))
    with pytest.raises(AmbiguousSymbol):
        sm.resolve("DUP", date(2020, 1, 1))
    # With the type given it is no longer ambiguous.
    assert sm.resolve("DUP", date(2020, 1, 1), IdType.TICKER).instrument_id == "A"


def test_alphabet_share_classes_are_separate_instruments():
    """Same issuer, different securities, different ISINs. Keying on company
    would net two positions that do not net."""
    sm = demo_security_master()
    a = sm.resolve("GOOGL", date(2024, 1, 2))
    c = sm.resolve("GOOG", date(2024, 1, 2))
    assert a.instrument_id != c.instrument_id
    assert (sm.cross_reference("GOOGL", date(2024, 1, 2))["isin"] !=
            sm.cross_reference("GOOG", date(2024, 1, 2))["isin"])


def test_lineage_is_ordered_and_covers_the_whole_history():
    sm = demo_security_master()
    hist = sm.history("QGL-EQ-META", IdType.TICKER)
    assert [a.value for a in hist] == ["FB", "META"]
    assert hist[0].valid_to == hist[1].valid_from     # no gap, no overlap


def test_fixture_identifiers_validate():
    """Every ISIN and CUSIP in the fixtures passes its own check digit.

    This is what keeps the fixtures honest: a mistyped or invented identifier
    fails the check, so the file cannot rot into plausible nonsense.
    """
    sm = demo_security_master()
    checked = 0
    for inst in sm.all_instruments():
        for a in inst.aliases:
            if a.id_type is IdType.ISIN:
                assert valid_isin(a.value), f"{inst.instrument_id}: bad ISIN {a.value}"
                checked += 1
            elif a.id_type is IdType.CUSIP:
                assert valid_cusip(a.value), f"{inst.instrument_id}: bad CUSIP {a.value}"
                checked += 1
    assert checked >= 16


def test_check_digits_catch_a_transposition():
    assert valid_isin("US0378331005")
    # Transpose two body digits: still 12 chars, still looks like an ISIN.
    assert not valid_isin("US0378313005")
    assert valid_cusip("037833100")
    assert not valid_cusip("037833101")


def test_delisted_instrument_still_resolves_for_history_but_is_not_tradable():
    sm = demo_security_master()
    old = sm.resolve("ZZZ", date(2005, 6, 1))
    assert old.tradable_on(date(2005, 6, 1))
    assert not old.tradable_on(date(2012, 6, 1))


# ========================================================== calendar
def test_settlement_is_trading_days_not_calendar_days():
    """T+2 over a weekend. Adding timedelta(2) to a Thursday gives Saturday."""
    cal = us_equity_calendar()
    thu = date(2025, 1, 2)
    assert cal.is_trading_day(thu)
    assert cal.settlement_date(thu, 2) == date(2025, 1, 6)     # Monday, not Sat


def test_settlement_steps_over_a_holiday():
    cal = us_equity_calendar()
    # Thanksgiving 2025 is Thu 27 Nov. A trade on Tue 25 Nov settles Fri 28,
    # not Thu 27.
    assert date(2025, 11, 27) in cal.holidays
    assert cal.settlement_date(date(2025, 11, 25), 2) == date(2025, 11, 28)


def test_half_day_is_open_with_an_early_close_not_a_holiday():
    """Treating it as closed loses a real session; treating it as normal leaves
    a schedule working against a session that already ended."""
    cal = us_equity_calendar()
    day_after_thanksgiving = date(2025, 11, 28)
    assert cal.is_trading_day(day_after_thanksgiving)
    assert cal.is_half_day(day_after_thanksgiving)
    assert cal.close_time_on(day_after_thanksgiving) == time(13, 0)
    assert cal.state_at(datetime(2025, 11, 28, 11, 0)).value == "open"
    # 14:00 is after the early close: closed, where a normal day would be open.
    assert cal.state_at(datetime(2025, 11, 28, 14, 30)).value != "open"
    assert cal.state_at(datetime(2025, 11, 26, 14, 30)).value == "open"


def test_holidays_are_computed_so_the_calendar_does_not_expire():
    """A hardcoded table stops being right at its last year, silently."""
    for year in (2024, 2030, 2041):
        h = us_equity_holidays(year)
        assert len(h) >= 10
        assert all(d.weekday() < 5 for d in h), "a holiday landed on a weekend"


def test_observed_holidays_shift_off_the_weekend():
    # 2021-07-04 was a Sunday -> observed Monday the 5th.
    assert date(2021, 7, 5) in us_equity_holidays(2021)
    assert date(2021, 7, 4) not in us_equity_holidays(2021)
    # 2026-07-04 is a Saturday -> observed Friday the 3rd.
    assert date(2026, 7, 3) in us_equity_holidays(2026)


def test_good_friday_moves_with_easter():
    assert date(2025, 4, 18) in us_equity_holidays(2025)
    assert date(2024, 3, 29) in us_equity_holidays(2024)


def test_juneteenth_only_from_2022():
    assert date(2022, 6, 20) in us_equity_holidays(2022)   # 19th was a Sunday
    assert not any(d.month == 6 for d in us_equity_holidays(2019))


def test_a_half_day_that_is_also_a_holiday_is_a_holiday():
    """Christmas Eve on a Friday when the 25th falls on Saturday: observed
    closed, not a short session."""
    cal = us_equity_calendar()
    for d in cal.half_days:
        assert d not in cal.holidays
        assert cal.is_trading_day(d)


def test_next_trading_day_skips_weekends_and_holidays():
    cal = us_equity_calendar()
    fri = date(2025, 5, 23)                      # before Memorial Day Mon 26th
    assert date(2025, 5, 26) in cal.holidays
    assert cal.next_trading_day(fri) == date(2025, 5, 27)
    assert cal.previous_trading_day(date(2025, 5, 27)) == fri


def test_trading_days_between_is_fewer_than_calendar_days():
    cal = us_equity_calendar()
    days = cal.trading_days_between(date(2025, 1, 1), date(2025, 12, 31))
    # 2025 had 250 US equity trading days; assert the shape rather than a
    # number this code is the only source of.
    assert 245 <= len(days) <= 255
    assert all(d.weekday() < 5 for d in days)


def test_crypto_calendar_is_always_open():
    cal = crypto_calendar()
    assert cal.is_trading_day(date(2025, 12, 25))      # Christmas
    assert cal.is_trading_day(date(2025, 1, 4))        # Saturday
    assert cal.next_trading_day(date(2025, 1, 4)) == date(2025, 1, 5)


def test_settlement_can_use_a_different_calendar():
    """A security can trade on a day its settlement system is closed."""
    trading = crypto_calendar()
    settle = us_equity_calendar()
    sat = date(2025, 1, 4)
    assert trading.is_trading_day(sat)
    assert trading.settlement_date(sat, 2, settlement_calendar=settle) == date(2025, 1, 7)


# ================================================= corporate actions
def test_split_preserves_market_value():
    """The invariant that catches an inverted ratio."""
    book = CorporateActionBook()
    ca = book.add(CorporateAction("AAPL", ActionType.SPLIT, date(2020, 8, 31), ratio=4.0))
    adj = book.adjust_position(instrument_id="AAPL", qty=100, avg_cost=400.0, action=ca)
    assert adj.qty_after == 400
    assert adj.cost_after == 100.0
    assert adj.market_value_preserved
    assert adj.qty_before * adj.cost_before == adj.qty_after * adj.cost_after


def test_reverse_split_is_the_same_code_path():
    book = CorporateActionBook()
    ca = book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=0.1))
    adj = book.adjust_position(instrument_id="X", qty=1000, avg_cost=2.0, action=ca)
    assert adj.qty_after == 100
    assert adj.cost_after == pytest.approx(20.0)
    assert adj.market_value_preserved


def test_cash_dividend_changes_neither_quantity_nor_cost_basis():
    """Reducing the cost basis on a dividend is a common error: it understates
    every future realised gain."""
    book = CorporateActionBook()
    ca = book.add(CorporateAction("X", ActionType.CASH_DIVIDEND, date(2025, 2, 10),
                                  cash_amount=0.25, pay_date=date(2025, 2, 28)))
    adj = book.adjust_position(instrument_id="X", qty=1000, avg_cost=50.0, action=ca)
    assert adj.qty_after == 1000
    assert adj.cost_after == 50.0
    assert adj.cash == pytest.approx(250.0)


def test_cumulative_factor_makes_returns_continuous_across_a_split():
    """Without this a 2-for-1 shows as a -50% return that never happened, and
    every vol, VaR, beta and correlation over the window is wrong."""
    book = CorporateActionBook()
    book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 6, 10), ratio=2.0))
    before, after = date(2024, 6, 7), date(2024, 6, 10)
    px_before, px_after = 200.0, 100.0        # same value, split in between
    raw_return = px_after / px_before - 1
    assert raw_return == pytest.approx(-0.5)  # the phantom move

    adj_before = px_before * book.cumulative_price_factor("X", before, after)
    assert adj_before == pytest.approx(100.0)
    assert px_after / adj_before - 1 == pytest.approx(0.0)


def test_factors_compound_in_the_right_direction():
    book = demo_corporate_actions()
    # AAPL: 7-for-1 in 2014 then 4-for-1 in 2020 => 28x shares, price /28.
    f = book.cumulative_price_factor("QGL-EQ-AAPL", date(2013, 1, 2), date(2025, 1, 2))
    assert f == pytest.approx(1 / 28)
    # A window that starts after both splits has no adjustment at all.
    assert book.cumulative_price_factor(
        "QGL-EQ-AAPL", date(2021, 1, 4), date(2025, 1, 2)) == pytest.approx(1.0)


def test_adjust_price_series_leaves_the_latest_price_untouched():
    book = CorporateActionBook()
    book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 6, 10), ratio=2.0))
    series = [(date(2024, 6, 7), 200.0), (date(2024, 6, 10), 100.0),
              (date(2024, 6, 11), 102.0)]
    out = dict(book.adjust_price_series("X", series))
    assert out[date(2024, 6, 11)] == pytest.approx(102.0)   # most recent is as-is
    assert out[date(2024, 6, 7)] == pytest.approx(100.0)    # back-adjusted


def test_two_quantity_actions_on_one_ex_date_are_refused():
    """They compound, and the order changes the answer."""
    book = CorporateActionBook()
    book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=2.0))
    with pytest.raises(CorporateActionError):
        book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=3.0))
    # A cash dividend on the same date is fine: it does not touch quantity.
    book.add(CorporateAction("X", ActionType.CASH_DIVIDEND, date(2024, 1, 5),
                             cash_amount=0.5))


def test_malformed_actions_are_refused_at_construction():
    with pytest.raises(CorporateActionError):
        CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=0.0)
    with pytest.raises(CorporateActionError):
        CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=1.0)   # no-op
    with pytest.raises(CorporateActionError):
        CorporateAction("X", ActionType.CASH_DIVIDEND, date(2024, 1, 5), cash_amount=0)
    with pytest.raises(CorporateActionError):
        # Cash cannot arrive before the entitlement is fixed.
        CorporateAction("X", ActionType.CASH_DIVIDEND, date(2024, 1, 5),
                        cash_amount=1.0, pay_date=date(2024, 1, 2))


def test_action_for_the_wrong_instrument_is_refused():
    book = CorporateActionBook()
    ca = book.add(CorporateAction("X", ActionType.SPLIT, date(2024, 1, 5), ratio=2.0))
    with pytest.raises(CorporateActionError):
        book.adjust_position(instrument_id="Y", qty=1, avg_cost=1, action=ca)


# ==================================================== reconciliation
def test_matching_positions_reconcile_clean():
    r = reconcile_positions({"A": 100, "B": -50}, {"A": 100, "B": -50})
    assert r.clean and r.matched == 2 and not r.breaks


def test_missing_here_is_critical_and_missing_there_is_not():
    """Asymmetric on purpose: a position we do not know we hold is unhedged
    market risk; one they have not seen yet is usually just a booking lag."""
    r = reconcile_positions({}, {"A": 100})
    assert r.breaks[0].break_type.value == "missing_here"
    assert r.breaks[0].severity is Severity.CRITICAL

    r2 = reconcile_positions({"A": 100}, {})
    assert r2.breaks[0].break_type.value == "missing_there"
    assert r2.breaks[0].severity is Severity.HIGH


def test_sign_flip_is_classified_separately_from_a_quantity_break():
    """100 vs -100 is not a 200-share quantity difference to chase; it is a
    convention mismatch, and calling it a quantity break sends someone looking
    for 200 shares that were never traded."""
    r = reconcile_positions({"A": 100}, {"A": -100})
    b = r.breaks[0]
    assert b.break_type.value == "sign"
    assert b.severity is Severity.CRITICAL
    assert b.difference == 200


def test_zero_on_both_sides_is_a_match_not_two_missing_breaks():
    r = reconcile_positions({"A": 0.0}, {"A": 0.0})
    assert r.clean and r.matched == 1


def test_a_flat_position_against_a_real_one_is_still_a_break():
    r = reconcile_positions({"A": 0.0}, {"A": 100.0})
    assert len(r.breaks) == 1
    assert r.breaks[0].break_type.value == "missing_here"
    r2 = reconcile_positions({"A": 100.0}, {"A": 0.0})
    assert r2.breaks[0].break_type.value == "missing_there"


def test_an_absent_key_and_an_explicit_zero_are_the_same_fact():
    """A broker reporting a closed position as 0.0 rather than dropping the row
    is the normal end state of every position that was ever flattened. Treating
    a missing key as anything other than flat made this crash."""
    assert reconcile_positions({}, {"A": 0.0}).clean
    assert reconcile_positions({"A": 0.0}, {}).clean
    assert reconcile_positions({}, {"A": 0.0}).matched == 1
    assert reconcile_positions({"A": 0.0}, {}).matched == 1


def test_a_flat_book_on_both_sides_reconciles_clean():
    """The state after a desk goes home flat. Every key is zero on both sides and
    none of it is a break."""
    flat = {s: 0.0 for s in ("A", "B", "C")}
    r = reconcile_positions(flat, {"A": 0.0, "D": 0.0})
    assert r.clean and r.matched == 4 and not r.breaks


def test_cash_tolerance_is_absolute_not_relative():
    """A percentage tolerance on a large balance permits real money."""
    r = reconcile_cash({"USD": 10_000_000.00}, {"USD": 10_000_000.004})
    assert r.clean
    r2 = reconcile_cash({"USD": 10_000_000.00}, {"USD": 10_000_001.00})
    assert not r2.clean
    assert r2.breaks[0].severity is Severity.CRITICAL


def test_a_date_difference_alone_is_a_timing_break_not_an_error():
    ours = [Holding("T1", 100, 50.0, date(2025, 3, 3))]
    theirs = [Holding("T1", 100, 50.0, date(2025, 3, 4))]
    r = reconcile_trades(ours, theirs)
    assert r.breaks[0].break_type.value == "timing"
    assert r.breaks[0].severity is Severity.INFO
    # INFO alone does not make the recon dirty -- otherwise every cutoff
    # difference turns the report red and people stop reading it.
    assert r.clean


def test_price_break_is_reported_when_quantities_agree():
    r = reconcile_trades([Holding("T1", 100, 50.00)], [Holding("T1", 100, 50.02)])
    assert r.breaks[0].break_type.value == "price"
    assert not r.clean


def test_price_within_tolerance_matches():
    r = reconcile_trades([Holding("T1", 100, 50.000)], [Holding("T1", 100, 50.004)])
    assert r.clean and r.matched == 1


def test_trade_recon_sees_offsetting_errors_a_position_recon_cannot():
    """Two errors that net to zero are invisible at position level. This is why
    both reconciliations exist rather than one."""
    ours = [Holding("T1", 100, 10.0), Holding("T2", 100, 10.0)]
    theirs = [Holding("T1", 150, 10.0), Holding("T2", 50, 10.0)]
    pos = reconcile_positions({"NET": 200}, {"NET": 200})
    assert pos.clean                                   # nets out, looks fine
    trades = reconcile_trades(ours, theirs)
    assert len([b for b in trades.breaks if b.break_type.value == "quantity"]) == 2


def test_every_break_carries_an_actionable_explanation():
    r = reconcile_positions({"A": 100, "B": 0}, {"A": -100, "B": 75})
    assert r.breaks
    for b in r.breaks:
        assert len(b.explanation) > 20
        assert b.suggested_action


# ======================================================= daily cycle
def _cycle() -> DailyCycle:
    return DailyCycle(us_equity_calendar(), demo_corporate_actions())


def test_eod_marks_values_and_rolls_to_the_next_trading_day():
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 23),
                      [Position("QGL-EQ-AAPL", 100, 150.0)],
                      {"QGL-EQ-AAPL": 200.0})
    assert res.ok
    assert res.total_market_value == pytest.approx(20_000.0)
    assert res.total_unrealised == pytest.approx(5_000.0)
    roll = [s for s in res.steps if s.name == "roll"][0]
    # Memorial Day is the Monday: the roll must skip it.
    assert roll.data["next_business_date"] == "2025-05-27"


def test_a_missing_mark_fails_the_cycle_rather_than_carrying_a_stale_one():
    """A stale mark reports a P&L that looks alive because it moves with the
    quantity, so the position in a halted name never looks wrong."""
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 22),
                      [Position("A", 100, 10.0), Position("B", 50, 20.0)],
                      {"A": 11.0})
    assert not res.ok
    assert "B" in [s for s in res.steps if s.name == "marking"][0].data["missing"]


def test_stale_marks_are_allowed_only_explicitly_and_are_named():
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 22),
                      [Position("A", 100, 10.0, mark=9.0), Position("B", 50, 20.0, mark=21.0)],
                      {"A": 11.0}, require_full_marks=False)
    assert res.ok
    assert [s for s in res.steps if s.name == "marking"][0].data["stale"] == ["B"]


def test_sod_applies_a_split_to_quantity_cost_and_mark_together():
    """Adjusting the position without the mark doubles the portfolio's value
    for the rest of the day."""
    cyc = _cycle()
    before = Position("QGL-EQ-NVDA", 100, 400.0, mark=1200.0)
    mv_before = before.market_value
    res = cyc.run_sod(date(2024, 6, 10), [before])      # NVDA 10-for-1
    assert res.ok
    p = res.positions[0]
    assert p.qty == 1000
    assert p.avg_cost == pytest.approx(40.0)
    assert p.mark == pytest.approx(120.0)
    assert p.market_value == pytest.approx(mv_before)   # nothing of value happened
    assert res.adjustments[0].market_value_preserved


def test_sod_books_a_dividend_as_cash_without_touching_the_position():
    cyc = _cycle()
    res = cyc.run_sod(date(2025, 2, 10), [Position("QGL-DEMO-NEWCO", 1000, 10.0, 12.0)])
    assert res.ok
    p = res.positions[0]
    assert (p.qty, p.avg_cost) == (1000, 10.0)
    assert res.cash_entitlements == pytest.approx(250.0)


def test_an_action_on_an_instrument_we_do_not_hold_is_a_no_op():
    cyc = _cycle()
    res = cyc.run_sod(date(2024, 6, 10), [Position("QGL-EQ-MSFT", 10, 1.0, 1.0)])
    assert res.ok and not res.adjustments


def test_running_a_phase_twice_is_refused():
    """A double-applied 10-for-1 gives a 100x position. Refusing is the feature."""
    cyc = _cycle()
    cyc.run_sod(date(2024, 6, 10), [Position("QGL-EQ-NVDA", 100, 400.0, 1200.0)])
    with pytest.raises(AlreadyRun):
        cyc.run_sod(date(2024, 6, 10), [Position("QGL-EQ-NVDA", 100, 400.0, 1200.0)])


def test_a_failed_run_still_blocks_a_blind_retry():
    """A retry that reruns the successful steps before hitting the same failure
    double-applies them."""
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 22), [Position("A", 1, 1.0)], {})
    assert not res.ok
    with pytest.raises(AlreadyRun):
        cyc.run_eod(date(2025, 5, 22), [Position("A", 1, 1.0)], {"A": 1.0})


def test_reversing_a_run_allows_it_again_and_is_explicit():
    cyc = _cycle()
    cyc.run_sod(date(2024, 6, 10), [Position("QGL-EQ-NVDA", 100, 400.0, 1200.0)])
    assert cyc.reverse(date(2024, 6, 10), Phase.SOD)
    assert not cyc.reverse(date(2024, 6, 10), Phase.SOD)   # already gone
    res = cyc.run_sod(date(2024, 6, 10), [Position("QGL-EQ-NVDA", 100, 400.0, 1200.0)])
    assert res.ok


def test_sod_and_eod_are_independent_claims_on_the_same_date():
    cyc = _cycle()
    cyc.run_sod(date(2025, 5, 22), [])
    res = cyc.run_eod(date(2025, 5, 22), [], {})
    assert res.ok


def test_a_cycle_on_a_closed_day_is_refused():
    cyc = _cycle()
    with pytest.raises(DailyCycleError):
        cyc.run_eod(date(2025, 5, 24), [], {})          # Saturday
    with pytest.raises(DailyCycleError):
        cyc.run_eod(date(2025, 5, 26), [], {})          # Memorial Day


def test_an_inverted_split_ratio_stops_the_cycle():
    """The market-value invariant is load-bearing, not decoration."""
    book = CorporateActionBook()
    book.add(CorporateAction("X", ActionType.SPLIT, date(2025, 5, 22), ratio=2.0))
    cyc = DailyCycle(us_equity_calendar(), book)
    # Patch the adjustment to apply the ratio in the same direction to both,
    # which is exactly the bug the invariant exists to catch.
    orig = book.adjust_position

    def broken(*, instrument_id, qty, avg_cost, action):
        adj = orig(instrument_id=instrument_id, qty=qty, avg_cost=avg_cost, action=action)
        adj.cost_after = avg_cost * action.ratio
        return adj

    book.adjust_position = broken
    res = cyc.run_sod(date(2025, 5, 22), [Position("X", 100, 50.0, 50.0)])
    assert not res.ok
    assert "inverted" in [s for s in res.steps if s.name == "corporate_actions"][0].detail


def test_reconciliation_runs_inside_the_cycle_when_an_external_record_is_given():
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 22), [Position("A", 100, 10.0)], {"A": 11.0},
                      external_positions={"A": 150.0})
    assert not res.ok
    assert res.recon and res.recon.breaks[0].break_type.value == "quantity"


def test_a_skipped_recon_is_reported_not_omitted():
    """A cycle with no recon is not a clean cycle, it is an unchecked one."""
    cyc = _cycle()
    res = cyc.run_eod(date(2025, 5, 22), [], {})
    step = [s for s in res.steps if s.name == "reconciliation"][0]
    assert "Skipped" in step.detail


def test_split_then_recon_against_a_counterparty_that_has_not_adjusted():
    """The realistic SOD break: they apply the split a day later."""
    cyc = _cycle()
    res = cyc.run_sod(date(2024, 6, 10),
                      [Position("QGL-EQ-NVDA", 100, 400.0, 1200.0)],
                      external_positions={"QGL-EQ-NVDA": 100.0})
    assert res.positions[0].qty == 1000
    assert not res.ok
    assert res.recon.breaks[0].break_type.value == "quantity"


def test_history_records_every_run():
    cyc = _cycle()
    cyc.run_sod(date(2025, 5, 22), [])
    cyc.run_eod(date(2025, 5, 22), [], {})
    h = cyc.history()
    assert len(h) == 2
    assert {r["phase"] for r in h} == {"sod", "eod"}


def test_cycle_result_is_labelled_simulated():
    cyc = _cycle()
    d = cyc.run_eod(date(2025, 5, 22), [], {}).to_dict()
    assert d["simulated"] is True
    assert "No money moves" in d["note"]
