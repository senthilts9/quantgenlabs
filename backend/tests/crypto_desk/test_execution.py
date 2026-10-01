"""Paper matching engine, risk limits and OMS lifecycle.

All offline and deterministic - no network, no credentials. The point is that
the full order path can be regression-tested in CI.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.core import (
    Code, InstrumentRegistry, OrderRequest, OrderStatus, OrderType, Position,
    Side, TimeInForce,
)
from cryptodesk.execution.oms import LEGAL_TRANSITIONS, OrderManager, TrackedOrder
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.risk.limits import KillSwitch, RiskEngine, RiskLimits
from cryptodesk.venues import PaperVenue

PERP = "BTC-USD-PERP"


def fresh_venue(collateral="100000", **kw) -> PaperVenue:
    reg = InstrumentRegistry()
    v = PaperVenue(reg, starting_collateral=Decimal(collateral), **kw)
    v.seed(synthetic_book(PERP, mid=Decimal("60000"), spread_bps=Decimal("2"),
                          levels=8, depth_per_level=Decimal("0.5")))
    return v


class TestPaperMatching(unittest.TestCase):

    def test_market_order_walks_multiple_levels(self):
        v = fresh_venue()
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                         Decimal("1.2")))
        self.assertEqual(ack.status, OrderStatus.FILLED)
        self.assertEqual(ack.filled_size, Decimal("1.2"))
        self.assertGreater(len(ack.fills), 1, "1.2 should span more than one level")
        # Average must sit between the first and last level touched.
        self.assertGreater(ack.avg_price, ack.fills[0].price)

    def test_book_is_consumed(self):
        """Without depletion, ten orders all 'fill' at the same top level and
        the simulation flatters any strategy that sends size."""
        v = fresh_venue()
        before = v.fetch_book(PERP).best_ask
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("1.2")))
        self.assertGreater(v.fetch_book(PERP).best_ask, before)

    def test_limit_order_rests_when_not_marketable(self):
        v = fresh_venue()
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                                         Decimal("0.5"), Decimal("59000.0")))
        self.assertEqual(ack.status, OrderStatus.NEW)
        self.assertIn(ack.order_id, v.fetch_open_orders())

    def test_resting_order_fills_on_incoming_flow(self):
        v = fresh_venue()
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                                         Decimal("0.5"), Decimal("59900.0"),
                                         post_only=True))
        fills = v.simulate_flow(PERP, Side.SELL, Decimal("0.3"), Decimal("59900.0"))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].liquidity, "maker")
        self.assertEqual(fills[0].size, Decimal("0.3"))
        # Partially filled, so still resting.
        self.assertIn(ack.order_id, v.fetch_open_orders())

    def test_maker_fill_earns_rebate(self):
        """BTC-USD-PERP has a negative maker fee; a passive fill must credit,
        not debit."""
        v = fresh_venue()
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                                   Decimal("0.5"), Decimal("59900.0"),
                                   post_only=True))
        fills = v.simulate_flow(PERP, Side.SELL, Decimal("0.5"), Decimal("59900.0"))
        self.assertLess(fills[0].fee, 0, "maker fee should be a rebate")

    def test_fok_unwinds_completely_when_unfillable(self):
        v = fresh_venue()
        v.seed(synthetic_book(PERP, mid=Decimal("60000"), levels=3,
                              depth_per_level=Decimal("0.1")))
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                         Decimal("5.0"),
                                         time_in_force=TimeInForce.FOK))
        self.assertEqual(ack.status, OrderStatus.CANCELLED)
        self.assertEqual(len(v.fetch_fills()), 0, "FOK must leave no fills behind")
        self.assertTrue(v.fetch_account().position(PERP).is_flat)

    def test_ioc_partially_fills_and_cancels_remainder(self):
        v = fresh_venue()
        v.seed(synthetic_book(PERP, mid=Decimal("60000"), levels=2,
                              depth_per_level=Decimal("0.1")))
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                         Decimal("5.0"),
                                         time_in_force=TimeInForce.IOC))
        self.assertEqual(ack.status, OrderStatus.PARTIALLY_FILLED)
        self.assertGreater(ack.filled_size, 0)
        self.assertLess(ack.filled_size, Decimal("5.0"))
        self.assertEqual(len(v.fetch_open_orders()), 0, "IOC must not rest")

    def test_position_averaging_on_add(self):
        v = fresh_venue()
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5")))
        first = v.fetch_account().position(PERP).entry_price
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5")))
        pos = v.fetch_account().position(PERP)
        self.assertEqual(pos.size, Decimal("1.0"))
        self.assertGreater(pos.entry_price, first, "adding higher must raise entry")

    def test_closing_returns_to_flat(self):
        v = fresh_venue()
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5")))
        v.place_order(OrderRequest(PERP, Side.SELL, OrderType.MARKET, Decimal("0.5")))
        pos = v.fetch_account().position(PERP)
        self.assertTrue(pos.is_flat)
        self.assertEqual(pos.entry_price, 0)

    def test_position_flip_resets_entry(self):
        v = fresh_venue()
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5")))
        v.place_order(OrderRequest(PERP, Side.SELL, OrderType.MARKET, Decimal("1.5")))
        pos = v.fetch_account().position(PERP)
        self.assertEqual(pos.size, Decimal("-1.0"))
        self.assertGreater(pos.entry_price, 0)

    def test_cancel_unknown_order_is_not_reported_as_cancelled(self):
        """Reporting CANCELLED for an order that actually traded is how a
        phantom flat position appears."""
        v = fresh_venue()
        ack = v.cancel_order("paper-does-not-exist")
        self.assertEqual(ack.status, OrderStatus.UNKNOWN)

    def test_cancel_removes_resting_order(self):
        v = fresh_venue()
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                                         Decimal("0.5"), Decimal("59000.0")))
        cancelled = v.cancel_order(ack.order_id)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        self.assertNotIn(ack.order_id, v.fetch_open_orders())

    def test_rejected_order_does_not_move_position(self):
        v = fresh_venue(collateral="10")
        ack = v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                         Decimal("50")))
        self.assertEqual(ack.status, OrderStatus.REJECTED)
        self.assertTrue(v.fetch_account().position(PERP).is_flat)


class TestRiskEngine(unittest.TestCase):

    def setUp(self):
        self.engine = RiskEngine(RiskLimits(
            max_order_notional=Decimal("50000"),
            max_position_notional=Decimal("100000"),
            max_daily_loss=Decimal("1000"),
            require_real_market_data=True,
        ))

    def test_order_notional_limit(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("10"),
                         Decimal("60000"))
        codes = [r.code for r in self.engine.check(o)]
        self.assertIn(Code.ORDER_NOTIONAL_LIMIT, codes)

    def test_within_limit_passes(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.1"),
                         Decimal("60000"))
        self.assertEqual(self.engine.check(o), [])

    def test_position_limit_uses_projected_position(self):
        from cryptodesk.core import AccountState
        acct = AccountState(positions={
            PERP: Position(PERP, Decimal("1.5"), Decimal("60000"), Decimal("60000"))})
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.5"),
                         Decimal("60000"))
        codes = [r.code for r in self.engine.check(o, account=acct)]
        self.assertIn(Code.POSITION_LIMIT_EXCEEDED, codes)

    def test_unpriceable_order_fails_closed(self):
        """A market order carries no price. With no mark either, the notional
        limits have nothing to measure - and silently skipping a configured
        limit is worse than rejecting, because a market order is exactly what
        an unchecked notional cap lets through."""
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("1000"))
        codes = [r.code for r in self.engine.check(o, mark=None)]
        self.assertIn(Code.ORDER_NOTIONAL_LIMIT, codes)

    def test_unpriceable_order_passes_when_no_notional_limits(self):
        engine = RiskEngine(RiskLimits.permissive())
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("1000"))
        self.assertEqual(engine.check(o, mark=None), [])

    def test_synthetic_book_refused(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.1"),
                         Decimal("60000"))
        codes = [r.code for r in self.engine.check(o, book_source="synthetic")]
        self.assertIn(Code.STALE_MARK_PRICE, codes)

    def test_daily_loss_trips_kill_switch(self):
        self.assertFalse(self.engine.kill_switch.tripped)
        self.engine.record_pnl(Decimal("-1500"))
        self.assertTrue(self.engine.kill_switch.tripped)

    def test_kill_switch_blocks_everything_and_reports_alone(self):
        self.engine.kill_switch.trip("manual")
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("1000"),
                         Decimal("60000"))
        rejections = self.engine.check(o)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].code, Code.KILL_SWITCH_ACTIVE)

    def test_kill_switch_latches(self):
        """A switch that re-arms itself lets the same failure through twice."""
        ks = KillSwitch()
        ks.trip("first")
        ks.trip("second")
        self.assertEqual(ks.reason, "first")
        ks.clear("operator")
        self.assertFalse(ks.tripped)
        self.assertEqual(len(ks.history), 2)

    def test_venue_whitelist(self):
        engine = RiskEngine(RiskLimits(allowed_venues=frozenset({"paper"}),
                                       require_real_market_data=False))
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.1"),
                         Decimal("60000"))
        self.assertEqual(engine.check(o, venue="paper"), [])
        codes = [r.code for r in engine.check(o, venue="binance")]
        self.assertIn(Code.VENUE_NOT_ALLOWED, codes)


class TestOrderManager(unittest.TestCase):

    def setUp(self):
        self.venue = fresh_venue()
        self.oms = OrderManager(self.venue, RiskEngine(RiskLimits.permissive()))

    def test_submit_and_fill(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5"))
        t = self.oms.submit(o)
        self.assertEqual(t.status, OrderStatus.FILLED)
        self.assertEqual(t.filled_size, Decimal("0.5"))
        self.assertIsNotNone(t.avg_price)

    def test_idempotency_blocks_duplicate_submit(self):
        """The single most common network failure - request sent, response
        lost - must not double the position on resend."""
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5"),
                         client_order_id="fixed-id")
        first = self.oms.submit(o)
        second = self.oms.submit(o)
        self.assertIs(first, second)
        self.assertEqual(len(self.venue.fetch_fills()), len(first.fills))
        self.assertIn("duplicate submit ignored", " ".join(second.transitions))

    def test_risk_rejection_never_reaches_venue(self):
        oms = OrderManager(self.venue, RiskEngine(RiskLimits(
            max_order_notional=Decimal("100"), require_real_market_data=False)))
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("1.0"))
        t = oms.submit(o)
        self.assertEqual(t.status, OrderStatus.REJECTED)
        self.assertEqual(len(self.venue.fetch_fills()), 0)

    def test_illegal_transition_is_refused_not_applied(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5"))
        t = self.oms.submit(o)
        self.assertEqual(t.status, OrderStatus.FILLED)
        applied = t.transition(OrderStatus.NEW)
        self.assertFalse(applied)
        self.assertEqual(t.status, OrderStatus.FILLED)
        self.assertIn("REJECTED(illegal)", " ".join(t.transitions))

    def test_unknown_can_resolve_to_anything(self):
        for target in (OrderStatus.NEW, OrderStatus.FILLED, OrderStatus.CANCELLED,
                       OrderStatus.REJECTED):
            self.assertIn(target, LEGAL_TRANSITIONS[OrderStatus.UNKNOWN])

    def test_cancel_lifecycle(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.5"),
                         Decimal("59000.0"))
        t = self.oms.submit(o)
        self.assertEqual(t.status, OrderStatus.NEW)
        cancelled = self.oms.cancel(t.client_order_id)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)

    def test_cancel_terminal_order_is_noop(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.5"))
        t = self.oms.submit(o)
        again = self.oms.cancel(t.client_order_id)
        self.assertEqual(again.status, OrderStatus.FILLED)
        self.assertIn("cancel ignored", " ".join(again.transitions))

    def test_reconcile_detects_orphan_at_venue(self):
        """An order the venue has and we do not is real size nobody is
        watching - it must be surfaced, not silently adopted."""
        self.venue.place_order(OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                                            Decimal("0.5"), Decimal("59000.0")))
        findings = self.oms.reconcile()
        self.assertEqual(len(findings["orphaned_at_venue"]), 1)

    def test_reconcile_clean_when_in_sync(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.5"),
                         Decimal("59000.0"))
        self.oms.submit(o)
        findings = self.oms.reconcile()
        self.assertEqual(findings["orphaned_at_venue"], [])
        self.assertEqual(findings["missing_at_venue"], [])

    def test_reconcile_flags_locally_live_but_gone_at_venue(self):
        o = OrderRequest(PERP, Side.BUY, OrderType.LIMIT, Decimal("0.5"),
                         Decimal("59000.0"))
        t = self.oms.submit(o)
        self.venue.cancel_order(t.order_id)  # cancelled behind the OMS's back
        findings = self.oms.reconcile()
        self.assertEqual(len(findings["missing_at_venue"]), 1)

    def test_stats_aggregate_rejection_codes(self):
        oms = OrderManager(fresh_venue(collateral="10"),
                           RiskEngine(RiskLimits.permissive()))
        oms.submit(OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("50")))
        stats = oms.stats()
        self.assertEqual(stats["rejected"], 1)
        self.assertIn(Code.INSUFFICIENT_MARGIN.value, stats["rejections_by_code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReplayProtection(unittest.TestCase):
    """Regression: the replay rules must fire in the *live* path.

    These rules existed and passed their unit tests for a while whilst being
    dead code in production - every venue adapter built a validation context
    with an empty nonce set, so nothing was ever compared against anything.
    A unit test of a rule proves the rule; only these prove it is wired.
    """

    def test_duplicate_client_order_id_rejected_at_venue(self):
        v = fresh_venue()
        o1 = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.01"),
                          client_order_id="dup-1")
        self.assertEqual(v.place_order(o1).status, OrderStatus.FILLED)
        o2 = OrderRequest(PERP, Side.BUY, OrderType.MARKET, Decimal("0.01"),
                          client_order_id="dup-1")
        ack = v.place_order(o2)
        self.assertEqual(ack.status, OrderStatus.REJECTED)
        self.assertIn(Code.DUPLICATE_CLIENT_ORDER_ID,
                      [r.code for r in ack.rejections])

    def test_idempotency_survives_dropping_the_auth_stage(self):
        """The local engine has no auth surface, so its ruleset drops AUTH.
        Idempotency must not go with it - it is a correctness property of the
        request, not an authentication one."""
        from cryptodesk.validation import for_venue
        rules = for_venue("paper", InstrumentRegistry()).rules_by_stage
        from cryptodesk.core.errors import Stage
        from cryptodesk.validation.rules import rule_client_id_unique
        self.assertIn(rule_client_id_unique, rules.get(Stage.SCHEMA, []))

    def test_rejected_order_does_not_burn_its_nonce(self):
        """An order refused before reaching the book never existed, so its
        nonce must stay usable - otherwise a rejected order permanently
        consumes a nonce the client still believes is free."""
        v = fresh_venue(collateral="10")
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                   Decimal("50"), nonce=999))
        self.assertNotIn(999, v.replay.nonces)

    def test_accepted_order_records_its_nonce(self):
        v = fresh_venue()
        v.place_order(OrderRequest(PERP, Side.BUY, OrderType.MARKET,
                                   Decimal("0.01"), nonce=1234))
        self.assertIn(1234, v.replay.nonces)

    def test_replay_guard_prunes_by_time(self):
        """Unbounded replay state is a leak. A nonce older than the retention
        window cannot be replayed successfully anyway - the freshness rule
        rejects it first - so it is forgotten."""
        from cryptodesk.core import ReplayGuard
        g = ReplayGuard(ttl_ms=1000)
        now = 1_000_000
        g.record(nonce=1, client_order_id="a", now=now)
        self.assertIn(1, g._nonce_set)
        g._prune(now + 2000)
        self.assertNotIn(1, g._nonce_set)
        self.assertEqual(len(g._nonces), 0)

    def test_replay_guard_respects_hard_ceiling(self):
        from cryptodesk.core import ReplayGuard
        g = ReplayGuard(ttl_ms=10_000_000, max_entries=10)
        for i in range(50):
            g.record(nonce=i)
        self.assertLessEqual(len(g._nonces), 11)
