"""Reference data API.

Wiring tests, not logic tests -- the logic is covered in test_refdata.py. What is
asserted here is the contract the frontend and any caller depend on: that the
as-of date is genuinely REQUIRED rather than defaulted, that a refusal comes back
as the right status code instead of a 500, and that the batch session's
idempotency guard survives the trip through HTTP.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------- symbology
def test_instruments_lists_the_fixture_master_and_says_what_it_is():
    r = client.get("/api/refdata/instruments")
    assert r.status_code == 200
    d = r.json()
    assert d["count"] >= 12
    assert "nothing here is a price" in d["note"]
    ids = {i["instrument_id"] for i in d["instruments"]}
    assert {"QGL-EQ-AAPL", "QGL-CRYPTO-BTCUSD", "QGL-ETF-SPY"} <= ids


def test_instruments_filters_by_asset_class_and_crypto_is_present():
    """Crypto resolves through the same master rather than being a special case."""
    r = client.get("/api/refdata/instruments", params={"asset_class": "crypto"})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] >= 2
    assert all(i["asset_class"] == "crypto" for i in d["instruments"])


def test_resolve_requires_an_as_of_date():
    """The whole point of the layer. A resolve endpoint that defaults to today is
    one that will be silently wrong on historical data."""
    assert client.get("/api/refdata/resolve", params={"symbol": "AAPL"}).status_code == 422


def test_resolve_returns_different_instruments_for_the_same_reused_ticker():
    old = client.get("/api/refdata/resolve", params={"symbol": "ZZZ", "on": "2000-01-03"})
    new = client.get("/api/refdata/resolve", params={"symbol": "ZZZ", "on": "2020-01-03"})
    assert old.status_code == new.status_code == 200
    assert old.json()["instrument_id"] != new.json()["instrument_id"]
    # The gap between them belongs to nobody, and 404 is the honest answer.
    gap = client.get("/api/refdata/resolve", params={"symbol": "ZZZ", "on": "2011-06-01"})
    assert gap.status_code == 404


def test_resolve_carries_the_full_cross_reference():
    r = client.get("/api/refdata/resolve", params={"symbol": "US0378331005", "on": "2024-01-02"})
    assert r.status_code == 200
    d = r.json()
    assert d["instrument_id"] == "QGL-EQ-AAPL"
    assert d["identifiers"]["ticker"] == "AAPL"
    assert d["identifiers"]["cusip"] == "037833100"


def test_two_tickers_for_one_instrument_are_not_ambiguous():
    """BTC-USD and BTCUSD are two aliases of the SAME instrument, so both resolve
    cleanly. Ambiguity is two DISTINCT instruments sharing a string, which the
    unit tests cover -- it cannot be built from the fixture master, which is
    itself the point: a security master with a live ambiguity is a broken one."""
    for sym in ("BTC-USD", "BTCUSD"):
        r = client.get("/api/refdata/resolve", params={"symbol": sym, "on": "2024-01-02"})
        assert r.status_code == 200
        assert r.json()["instrument_id"] == "QGL-CRYPTO-BTCUSD"


def test_unknown_id_type_is_422_not_500():
    r = client.get("/api/refdata/resolve",
                   params={"symbol": "AAPL", "on": "2024-01-02", "id_type": "nope"})
    assert r.status_code == 422
    assert "id_type" in r.json()["detail"]


def test_lineage_shows_the_ticker_change_with_no_gap_and_no_overlap():
    r = client.get("/api/refdata/lineage/QGL-EQ-META", params={"id_type": "ticker"})
    assert r.status_code == 200
    h = r.json()["history"]
    assert [x["value"] for x in h] == ["FB", "META"]
    assert h[0]["valid_to"] == h[1]["valid_from"]
    assert h[1]["valid_to"] is None          # still current


def test_lineage_for_an_unknown_instrument_is_404():
    assert client.get("/api/refdata/lineage/NOPE").status_code == 404


def test_validate_endpoint_rejects_a_transposed_isin():
    good = client.get("/api/refdata/validate", params={"isin": "US0378331005"})
    bad = client.get("/api/refdata/validate", params={"isin": "US0378313005"})
    assert good.json()["isin"]["valid"] is True
    assert bad.json()["isin"]["valid"] is False
    # And it does not overclaim: a valid check digit is not proof of existence.
    assert "does not mean the security exists" in good.json()["note"]


def test_validate_with_no_argument_is_422():
    assert client.get("/api/refdata/validate").status_code == 422


# ----------------------------------------------------------------- calendar
def test_calendar_reports_settlement_as_trading_days():
    r = client.get("/api/refdata/calendar/XNYS", params={"on": "2025-01-02"})
    assert r.status_code == 200
    d = r.json()
    assert d["is_trading_day"] is True
    assert d["settlement_t_plus_2"] == "2025-01-06"       # Monday, not Saturday
    assert "two TRADING days forward" in d["note"]


def test_calendar_on_a_holiday_reports_closed_and_no_settlement():
    r = client.get("/api/refdata/calendar/XNYS", params={"on": "2025-05-26"})
    d = r.json()
    assert d["is_trading_day"] is False
    assert d["close_time"] is None
    assert d["settlement_t_plus_2"] is None
    assert d["next_trading_day"] == "2025-05-27"


def test_calendar_marks_a_half_day_as_open_with_an_early_close():
    r = client.get("/api/refdata/calendar/XNYS", params={"on": "2025-11-28"})
    d = r.json()
    assert d["is_trading_day"] is True and d["is_half_day"] is True
    assert d["close_time"] == "13:00:00"


def test_xnas_and_xnys_cannot_drift_apart():
    a = client.get("/api/refdata/calendar/XNYS", params={"on": "2025-05-26"}).json()
    b = client.get("/api/refdata/calendar/XNAS", params={"on": "2025-05-26"}).json()
    assert a["is_trading_day"] == b["is_trading_day"]


def test_unknown_calendar_is_404_and_names_the_alternatives():
    r = client.get("/api/refdata/calendar/XXXX", params={"on": "2025-01-02"})
    assert r.status_code == 404
    assert "XNYS" in r.json()["detail"]


def test_session_state_is_interpreted_as_exchange_local():
    r = client.get("/api/refdata/calendar/XNYS/session",
                   params={"at": "2025-01-02T10:30:00"})
    d = r.json()
    assert d["state"] == "open" and d["is_open"] is True
    assert "not UTC" in d["note"]
    pre = client.get("/api/refdata/calendar/XNYS/session",
                     params={"at": "2025-01-02T05:00:00"}).json()
    assert pre["state"] == "pre_open" and pre["is_open"] is False


def test_trading_days_are_fewer_than_calendar_days():
    r = client.get("/api/refdata/calendar/XNYS/days",
                   params={"start": "2025-01-01", "end": "2025-03-31"})
    d = r.json()
    assert d["trading_days"] < d["calendar_days"]
    assert d["holidays_in_range"]


def test_crypto_calendar_is_open_on_christmas():
    r = client.get("/api/refdata/calendar/CRYPTO", params={"on": "2025-12-25"})
    assert r.json()["is_trading_day"] is True


def test_day_range_is_capped():
    r = client.get("/api/refdata/calendar/XNYS/days",
                   params={"start": "2020-01-01", "end": "2025-01-01"})
    assert r.status_code == 422


# -------------------------------------------------------- corporate actions
def test_corporate_actions_state_the_ratio_convention():
    r = client.get("/api/refdata/corporate-actions",
                   params={"instrument_id": "QGL-EQ-NVDA"})
    d = r.json()
    assert d["count"] == 2
    assert "NEW shares per OLD share" in d["note"]
    assert {a["ratio"] for a in d["actions"]} == {4.0, 10.0}


def test_adjustment_factor_compounds_both_splits():
    r = client.get("/api/refdata/adjustment-factor",
                   params={"instrument_id": "QGL-EQ-AAPL",
                           "from_date": "2013-01-02", "to_date": "2025-01-02"})
    d = r.json()
    assert d["price_factor"] == pytest.approx(1 / 28)
    assert d["quantity_factor"] == pytest.approx(28.0)
    assert "every volatility, VaR and beta" in d["note"]


def test_adjust_position_preserves_market_value_and_says_so():
    r = client.post("/api/refdata/adjust-position",
                    json={"instrument_id": "QGL-EQ-NVDA", "qty": 100,
                          "avg_cost": 400.0, "ex_date": "2024-06-10"})
    d = r.json()
    assert d["qty_after"] == 1000
    assert d["cost_after"] == pytest.approx(40.0)
    assert d["book_value_before"] == pytest.approx(d["book_value_after"])
    assert d["market_value_preserved"] is True


def test_adjust_position_with_no_action_on_that_date_is_404():
    r = client.post("/api/refdata/adjust-position",
                    json={"instrument_id": "QGL-EQ-NVDA", "qty": 100,
                          "avg_cost": 400.0, "ex_date": "2024-06-11"})
    assert r.status_code == 404


# ----------------------------------------------------------- reconciliation
def test_position_recon_classifies_rather_than_reporting_a_boolean():
    r = client.post("/api/refdata/reconcile/positions",
                    json={"ours": {"A": 100, "B": 100, "C": 0},
                          "theirs": {"A": -100, "B": 150, "D": 75},
                          "as_of": "2025-05-22"})
    d = r.json()
    assert d["clean"] is False
    kinds = {b["break_type"] for b in d["breaks"]}
    assert kinds == {"sign", "quantity", "missing_here"}
    assert d["by_severity"]["critical"] >= 2
    for b in d["breaks"]:
        assert b["explanation"] and b["suggested_action"]


def test_a_flat_book_reconciles_clean_over_http():
    r = client.post("/api/refdata/reconcile/positions",
                    json={"ours": {"A": 0.0}, "theirs": {}})
    assert r.status_code == 200
    assert r.json()["clean"] is True


def test_cash_recon_flags_a_dollar_on_ten_million():
    r = client.post("/api/refdata/reconcile/cash",
                    json={"ours": {"USD": 10000000.0}, "theirs": {"USD": 10000001.0}})
    d = r.json()
    assert d["clean"] is False
    assert d["breaks"][0]["severity"] == "critical"


def test_trade_recon_calls_a_date_difference_timing_and_stays_clean():
    r = client.post("/api/refdata/reconcile/trades",
                    json={"ours": [{"key": "T1", "qty": 100, "price": 50.0,
                                    "trade_date": "2025-03-03"}],
                          "theirs": [{"key": "T1", "qty": 100, "price": 50.0,
                                      "trade_date": "2025-03-04"}]})
    d = r.json()
    assert d["breaks"][0]["break_type"] == "timing"
    assert d["breaks"][0]["severity"] == "info"
    assert d["clean"] is True


# -------------------------------------------------------------- batch cycle
def test_eod_marks_values_and_rolls_over_a_holiday():
    r = client.post("/api/refdata/batch/eod",
                    json={"business_date": "2025-05-23",
                          "positions": [{"instrument_id": "QGL-EQ-AAPL",
                                         "qty": 100, "avg_cost": 150.0}],
                          "marks": {"QGL-EQ-AAPL": 200.0}})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["total_market_value"] == pytest.approx(20000.0)
    roll = [s for s in d["steps"] if s["step"] == "roll"][0]
    assert roll["next_business_date"] == "2025-05-27"      # skips Memorial Day
    assert d["simulated"] is True


def test_sod_applies_a_split_through_the_api():
    r = client.post("/api/refdata/batch/sod",
                    json={"business_date": "2024-06-10",
                          "positions": [{"instrument_id": "QGL-EQ-NVDA", "qty": 100,
                                         "avg_cost": 400.0, "mark": 1200.0}]})
    d = r.json()
    assert d["ok"] is True
    p = d["positions"][0]
    assert (p["qty"], p["avg_cost"], p["mark"]) == (1000.0, 40.0, 120.0)
    assert p["market_value"] == pytest.approx(120000.0)
    assert d["adjustments"][0]["market_value_preserved"] is True


def test_the_idempotency_guard_survives_http_and_returns_409():
    """A second EOD must not double-apply. 409 with an explanation, not a 500."""
    body = {"business_date": "2025-05-22", "positions": [], "marks": {}}
    first = client.post("/api/refdata/batch/eod", json=body)
    assert first.status_code == 200
    sid = first.json()["session_id"]
    again = client.post("/api/refdata/batch/eod", json={**body, "session_id": sid})
    assert again.status_code == 409
    assert "already run" in again.json()["detail"]


def test_a_new_session_is_not_blocked_by_another_session_s_run():
    body = {"business_date": "2025-05-21", "positions": [], "marks": {}}
    a = client.post("/api/refdata/batch/eod", json=body)
    b = client.post("/api/refdata/batch/eod", json=body)
    assert a.status_code == b.status_code == 200
    assert a.json()["session_id"] != b.json()["session_id"]


def test_a_cycle_on_a_closed_day_is_409():
    r = client.post("/api/refdata/batch/eod",
                    json={"business_date": "2025-05-24", "positions": [], "marks": {}})
    assert r.status_code == 409
    assert "not a trading day" in r.json()["detail"]


def test_a_missing_mark_returns_200_with_ok_false_not_an_error():
    """The cycle ran and reported a problem; that is a result, not a failure of
    the endpoint."""
    r = client.post("/api/refdata/batch/eod",
                    json={"business_date": "2025-05-20",
                          "positions": [{"instrument_id": "A", "qty": 100,
                                         "avg_cost": 10.0}],
                          "marks": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert [s for s in r.json()["steps"] if s["step"] == "marking"][0]["ok"] is False


def test_history_then_reverse_then_rerun():
    body = {"business_date": "2025-05-19", "positions": [], "marks": {}}
    sid = client.post("/api/refdata/batch/eod", json=body).json()["session_id"]

    h = client.get("/api/refdata/batch/history", params={"session_id": sid})
    assert h.status_code == 200 and len(h.json()["runs"]) == 1

    rev = client.delete(f"/api/refdata/batch/{sid}/2025-05-19/eod")
    assert rev.status_code == 200 and rev.json()["reversed"] is True

    again = client.post("/api/refdata/batch/eod", json={**body, "session_id": sid})
    assert again.status_code == 200


def test_reversing_something_that_never_ran_is_404():
    sid = client.post("/api/refdata/batch/eod",
                      json={"business_date": "2025-05-16", "positions": [],
                            "marks": {}}).json()["session_id"]
    assert client.delete(f"/api/refdata/batch/{sid}/2025-05-15/eod").status_code == 404
    assert client.delete(f"/api/refdata/batch/{sid}/2025-05-16/sod").status_code == 404


def test_unknown_phase_is_422_and_unknown_session_is_404():
    assert client.get("/api/refdata/batch/history",
                      params={"session_id": "nope"}).status_code == 404
    sid = client.post("/api/refdata/batch/eod",
                      json={"business_date": "2025-05-15", "positions": [],
                            "marks": {}}).json()["session_id"]
    r = client.delete(f"/api/refdata/batch/{sid}/2025-05-15/lunch")
    assert r.status_code == 422


def test_recon_inside_the_cycle_reports_the_break():
    r = client.post("/api/refdata/batch/eod",
                    json={"business_date": "2025-05-14",
                          "positions": [{"instrument_id": "A", "qty": 100,
                                         "avg_cost": 10.0}],
                          "marks": {"A": 11.0},
                          "external_positions": {"A": 150.0}})
    d = r.json()
    assert d["ok"] is False
    assert d["recon"]["breaks"][0]["break_type"] == "quantity"


# ------------------------------------------------- frontend response contract
# The Reference Data screen reads these exact fields. Playwright cannot run on
# this host, so instead of a screenshot that proves nothing durable, the contract
# the screen depends on is pinned here: renaming or dropping any field below
# breaks the UI silently -- the panel renders with `undefined` in it and no error
# anywhere -- and now breaks a test instead.
def _keys(d: dict, *required: str) -> None:
    missing = [k for k in required if k not in d]
    assert not missing, f"response is missing {missing}; the screen reads them"


def test_resolve_contract_matches_what_the_screen_reads():
    d = client.get("/api/refdata/resolve",
                   params={"symbol": "ZZZ", "on": "2000-01-03"}).json()
    _keys(d, "as_of", "instrument_id", "name", "asset_class", "exchange",
          "currency", "tick_size", "tradable_on", "identifiers")
    assert isinstance(d["identifiers"], dict)


def test_a_failed_resolve_carries_detail_for_the_screen():
    d = client.get("/api/refdata/resolve",
                   params={"symbol": "ZZZ", "on": "2011-06-01"}).json()
    _keys(d, "detail")


def test_lineage_contract():
    d = client.get("/api/refdata/lineage/QGL-EQ-META").json()
    _keys(d, "instrument_id", "id_type", "history")
    for h in d["history"]:
        _keys(h, "value", "valid_from", "valid_to")


def test_calendar_contract():
    d = client.get("/api/refdata/calendar/XNYS", params={"on": "2025-11-25"}).json()
    _keys(d, "mic", "date", "is_trading_day", "is_half_day", "open_time",
          "close_time", "previous_trading_day", "next_trading_day",
          "settlement_t_plus_2", "note")


def test_corporate_actions_contract():
    d = client.get("/api/refdata/corporate-actions").json()
    _keys(d, "count", "actions", "note")
    for a in d["actions"]:
        _keys(a, "instrument_id", "type", "ex_date", "ratio", "cash_amount")


def test_adjust_position_contract():
    d = client.post("/api/refdata/adjust-position",
                    json={"instrument_id": "QGL-EQ-NVDA", "qty": 100,
                          "avg_cost": 400.0, "ex_date": "2024-06-10"}).json()
    _keys(d, "qty_before", "qty_after", "cost_before", "cost_after",
          "book_value_before", "book_value_after", "cash_entitlement",
          "market_value_preserved", "explanation")


def test_recon_contract_including_every_break_field():
    d = client.post("/api/refdata/reconcile/positions",
                    json={"ours": {"A": 100}, "theirs": {"A": -100}}).json()
    _keys(d, "matched", "break_count", "clean", "breaks", "by_severity", "note")
    for b in d["breaks"]:
        _keys(b, "break_type", "key", "severity", "ours", "theirs",
              "difference", "explanation", "suggested_action")


def test_batch_contract_including_step_and_position_shape():
    d = client.post("/api/refdata/batch/sod",
                    json={"business_date": "2024-06-10",
                          "positions": [{"instrument_id": "QGL-EQ-NVDA", "qty": 100,
                                         "avg_cost": 400.0, "mark": 1200.0}]}).json()
    _keys(d, "session_id", "ok", "steps", "positions", "adjustments", "recon",
          "total_market_value", "total_unrealised", "simulated", "note")
    for s in d["steps"]:
        _keys(s, "step", "ok", "detail")
    for p in d["positions"]:
        _keys(p, "instrument_id", "qty", "avg_cost", "mark", "market_value",
              "unrealised")
