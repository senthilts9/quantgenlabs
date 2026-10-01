"""Durable state, framed as invariant survival.

Persistence is not "does the data come back" - it is "which properties does a
restart violate?". Four do, and two of them cause silent *wrong behaviour*
rather than visible data loss, so those get the most attention here:

* a latching kill switch that a restart re-arms is not latching;
* a high-water mark that resets lets a fund charge performance fees on the
  recovery from its own drawdown.

Restart is modelled by building new objects over the same store file. The
cross-process case - `os._exit` mid-send, then a fresh interpreter - is
exercised by the scripts in the repo's scratchpad; these tests pin the
contract those scripts demonstrate.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cryptodesk.chain import StrategyVault
from cryptodesk.core import (
    InstrumentRegistry, OrderRequest, OrderStatus, OrderType, Side,
)
from cryptodesk.execution import OrderManager
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.risk.limits import RiskEngine, RiskLimits
from cryptodesk.store import Store, StoreError, memory_store
from cryptodesk.venues import PaperVenue
from cryptodesk.venues.base import VenueError

M = "BTC-USD-PERP"
D = Decimal


class Harness:
    """One "process". Building a second over the same path is a restart."""

    def __init__(self, path: Path):
        self.store = Store(path, durable=False)
        self.reg = InstrumentRegistry()
        self.venue = PaperVenue(self.reg, starting_collateral=D("100000"),
                                store=self.store)
        self.venue.seed(synthetic_book(M, D("60000"), levels=10,
                                       depth_per_level=D("1")))
        self.risk = RiskEngine(RiskLimits.permissive(), store=self.store)
        self.oms = OrderManager(self.venue, self.risk, store=self.store)

    def close(self):
        self.store.close()


class PersistenceCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "state.db"

    def tearDown(self):
        self._dir.cleanup()

    def restart(self, old: Harness) -> Harness:
        old.close()
        return Harness(self.path)


class TestStore(PersistenceCase):

    def test_schema_version_mismatch_refuses_rather_than_guesses(self):
        """Reading a blotter written by a different schema half-correctly is
        worse than refusing to read it."""
        s = Store(self.path, durable=False)
        s.put("x", 1)
        s._conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        s.close()
        with self.assertRaises(StoreError):
            Store(self.path, durable=False)

    def test_kv_roundtrip_and_delete(self):
        s = memory_store()
        s.put("k", {"a": 1})
        self.assertEqual(s.get("k"), {"a": 1})
        s.delete("k")
        self.assertIsNone(s.get("k"))

    def test_replay_entries_expire_on_read(self):
        """Unbounded replay state is a leak; pruning on read keeps the table
        bounded without a background thread."""
        s = memory_store()
        s.save_replay(nonce=1, client_order_id="a", expiry_ms=1_000)
        nonces, coids = s.load_replay(now_ms=999)
        self.assertEqual((nonces, coids), ({1}, {"a"}))
        nonces, coids = s.load_replay(now_ms=2_000)
        self.assertEqual((nonces, coids), (set(), set()))

    def test_order_upsert_does_not_duplicate(self):
        s = memory_store()
        rec = {"client_order_id": "c1", "market": M, "status": "PENDING_NEW",
               "venue": "paper", "created_ms": 1, "updated_ms": 1}
        s.save_order(rec)
        s.save_order(dict(rec, status="FILLED", updated_ms=2))
        self.assertEqual(s.order_count(), 1)
        self.assertEqual(s.load_orders()["c1"]["status"], "FILLED")

    def test_malformed_order_record_is_refused(self):
        with self.assertRaises(StoreError):
            memory_store().save_order({"client_order_id": "c1"})


class TestWriteAhead(PersistenceCase):

    def test_record_is_committed_before_the_send(self):
        """The whole point of write-ahead. A crash between send and ack is the
        scenario UNKNOWN exists for; persisting after the send would leave no
        trace at all, which is strictly worse."""
        class Exploding(PaperVenue):
            def place_order(self, request):
                raise VenueError("connection died mid-send")

        h = Harness(self.path)
        h.venue = Exploding(h.reg, starting_collateral=D("100000"),
                            store=h.store)
        h.venue.seed(synthetic_book(M, D("60000"), levels=10,
                                    depth_per_level=D("1")))
        h.oms = OrderManager(h.venue, h.risk, store=h.store)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.5"),
                                  D("59000.0"), client_order_id="wal-1"))

        h2 = self.restart(h)
        self.assertIn("wal-1", h2.oms.orders,
                      "order vanished with the process")
        h2.close()

    def test_risk_rejected_order_is_still_recorded(self):
        """A refusal is part of the audit trail, not an absence of one."""
        h = Harness(self.path)
        h.risk = RiskEngine(RiskLimits(max_order_notional=D("1"),
                                       require_real_market_data=False),
                            store=h.store)
        h.oms = OrderManager(h.venue, h.risk, store=h.store)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.5"),
                                  D("59000.0"), client_order_id="risk-1"))
        h2 = self.restart(h)
        self.assertEqual(h2.oms.orders["risk-1"].status, OrderStatus.REJECTED)
        h2.close()


class TestInvariantsSurviveRestart(PersistenceCase):

    def test_blotter_survives(self):
        h = Harness(self.path)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.01"),
                                  client_order_id="b-1"))
        h2 = self.restart(h)
        t = h2.oms.get("b-1")
        self.assertIsNotNone(t)
        self.assertEqual(t.status, OrderStatus.FILLED)
        self.assertGreater(t.filled_size, 0)
        h2.close()

    def test_recovered_order_keeps_its_full_request(self):
        """A summary would round-trip the display fields and lose the request,
        leaving a restarted process unable to cancel what it recovered."""
        h = Harness(self.path)
        h.oms.submit(OrderRequest(M, Side.SELL, OrderType.LIMIT, D("0.25"),
                                  D("61000.0"), client_order_id="b-2",
                                  post_only=True))
        h2 = self.restart(h)
        req = h2.oms.get("b-2").request
        self.assertEqual(req.side, Side.SELL)
        self.assertEqual(req.size, D("0.25"))
        self.assertEqual(req.price, D("61000.0"))
        self.assertTrue(req.post_only)
        h2.close()

    def test_idempotency_survives(self):
        """Forgetting a client_order_id makes an already-sent id reusable."""
        h = Harness(self.path)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.01"),
                                  client_order_id="dup-1"))
        h2 = self.restart(h)
        self.assertIn("dup-1", h2.venue.replay.client_order_ids)
        before = len(h2.oms.orders)
        h2.oms.submit(OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.01"),
                                   client_order_id="dup-1"))
        self.assertEqual(len(h2.oms.orders), before, "duplicate was re-sent")
        h2.close()

    def test_nonce_survives(self):
        h = Harness(self.path)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.01"),
                                  nonce=8888))
        h2 = self.restart(h)
        self.assertIn(8888, h2.venue.replay.nonces)
        h2.close()

    def test_kill_switch_stays_latched(self):
        """Its own docstring says a switch that re-arms itself lets the same
        failure through repeatedly. A restart re-armed it."""
        h = Harness(self.path)
        h.risk.kill_switch.trip("operator halted the desk")
        h2 = self.restart(h)
        self.assertTrue(h2.risk.kill_switch.tripped)
        self.assertEqual(h2.risk.kill_switch.reason, "operator halted the desk")
        h2.close()

    def test_cleared_kill_switch_stays_cleared(self):
        h = Harness(self.path)
        h.risk.kill_switch.trip("transient")
        h.risk.kill_switch.clear("operator")
        h2 = self.restart(h)
        self.assertFalse(h2.risk.kill_switch.tripped)
        h2.close()

    def test_daily_loss_limit_code_survives(self):
        from cryptodesk.core import Code
        h = Harness(self.path)
        h.risk = RiskEngine(RiskLimits(max_daily_loss=D("100"),
                                       require_real_market_data=False),
                            store=h.store)
        h.risk.record_pnl(D("-500"))
        h2 = self.restart(h)
        h2.risk = RiskEngine(RiskLimits(max_daily_loss=D("100"),
                                        require_real_market_data=False),
                             store=h2.store)
        self.assertTrue(h2.risk.kill_switch.tripped)
        self.assertEqual(h2.risk.kill_switch.code, Code.DAILY_LOSS_LIMIT)
        h2.close()


class TestVaultDurability(PersistenceCase):

    def _funded(self, store) -> StrategyVault:
        v = StrategyVault(management_fee_bps=D("0"))
        v.deposit(D("100000"), "alice")
        v.allocate(D("100000"))
        v.mark(collateral=D("120000"))       # +20%, sets the HWM
        v.save(store, name="t")
        return v

    def test_share_ledger_survives(self):
        """Losing who owns what is the one failure with no recovery path."""
        s = Store(self.path, durable=False)
        self._funded(s)
        s.close()

        s2 = Store(self.path, durable=False)
        v = StrategyVault(management_fee_bps=D("0"))
        self.assertTrue(v.load(s2, name="t"))
        self.assertEqual(v.holders["alice"].shares, D("100000.000000"))
        self.assertEqual(v.total_supply, D("100000.000000"))
        s2.close()

    def test_high_water_mark_survives(self):
        s = Store(self.path, durable=False)
        original = self._funded(s)
        s.close()

        s2 = Store(self.path, durable=False)
        v = StrategyVault(management_fee_bps=D("0"))
        v.load(s2, name="t")
        self.assertEqual(v.high_water_mark, original.high_water_mark)
        self.assertGreater(v.high_water_mark, D("1"))
        s2.close()

    def test_restart_does_not_recharge_fees_on_a_recovery(self):
        """The money consequence. A reset HWM would charge the whole climb
        back from a drawdown as though it were new profit."""
        s = Store(self.path, durable=False)
        self._funded(s)
        s.close()

        s2 = Store(self.path, durable=False)
        v = StrategyVault(management_fee_bps=D("0"))
        v.load(s2, name="t")
        before = v.fees_accrued
        v.mark(collateral=D("100000"))   # drawdown
        v.mark(collateral=D("118000"))   # recovery, still under the old HWM
        self.assertEqual(v.fees_accrued, before)
        s2.close()

    def test_restored_nav_is_stale_until_re_marked(self):
        """After a restart the portfolio has moved. Converting at a pre-outage
        share price would hand the first depositor a stale rate."""
        from cryptodesk.chain.strategy_vault import StaleNav
        s = Store(self.path, durable=False)
        self._funded(s)
        s.close()

        s2 = Store(self.path, durable=False)
        v = StrategyVault(management_fee_bps=D("0"), max_nav_age_ms=0)
        v.load(s2, name="t")
        v._nav.timestamp_ms -= 10_000
        with self.assertRaises(StaleNav):
            v.deposit(D("1000"), "bob")
        s2.close()


class TestReconcileAfterRestart(PersistenceCase):

    def test_caught_send_failure_becomes_unknown_not_unacknowledged(self):
        """A VenueError we caught is different from a crash. We know the send
        was attempted and failed, so the order is UNKNOWN - a state the
        existing reconciliation path already handles."""
        class Exploding(PaperVenue):
            def place_order(self, request):
                raise VenueError("died mid-send")

        h = Harness(self.path)
        h.venue = Exploding(h.reg, starting_collateral=D("100000"),
                            store=h.store)
        h.venue.seed(synthetic_book(M, D("60000"), levels=10,
                                    depth_per_level=D("1")))
        h.oms = OrderManager(h.venue, h.risk, store=h.store)
        h.oms.submit(OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.5"),
                                  D("59000.0"), client_order_id="caught-1"))

        h2 = self.restart(h)
        self.assertEqual(h2.oms.get("caught-1").status, OrderStatus.UNKNOWN)
        findings = h2.oms.reconcile()
        self.assertTrue(any("caught-1" in f for f in findings["resolved_unknown"]))
        h2.close()

    def test_crash_artifact_is_surfaced_as_unacknowledged(self):
        """PENDING_NEW with no venue id is what a hard kill mid-send leaves:
        the write-ahead row proves we were about to send, nothing proves
        whether it landed, and there is no venue id to look it up by. Every
        other reconciliation check keys on order_id and would skip it in
        silence.

        Constructed by writing the row directly, because that is precisely the
        artifact a killed process leaves - it never reached any except block.
        """
        h = Harness(self.path)
        req = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.5"), D("59000.0"),
                           client_order_id="ghost-1")
        h.store.save_order({
            "client_order_id": "ghost-1", "order_id": None, "market": M,
            "status": OrderStatus.PENDING_NEW.value, "venue": "paper",
            "created_ms": 1, "updated_ms": 1, "attempts": 1,
            "filled_size": "0", "cost": "0",
            "request": req.to_dict(), "transitions": [], "fills": [],
        })

        h2 = self.restart(h)
        self.assertEqual(h2.oms.get("ghost-1").status, OrderStatus.PENDING_NEW)
        findings = h2.oms.reconcile()
        self.assertEqual(len(findings["unacknowledged"]), 1)
        self.assertIn("ghost-1", findings["unacknowledged"][0])
        h2.close()

    def test_no_store_behaves_exactly_as_before(self):
        """Persistence is opt-in; `store=None` must change nothing."""
        reg = InstrumentRegistry()
        v = PaperVenue(reg, starting_collateral=D("100000"))
        v.seed(synthetic_book(M, D("60000"), levels=10, depth_per_level=D("1")))
        oms = OrderManager(v, RiskEngine(RiskLimits.permissive()))
        t = oms.submit(OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.01")))
        self.assertEqual(t.status, OrderStatus.FILLED)
        self.assertIsNone(oms._store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
