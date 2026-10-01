"""Cross-process invariant survival.

Carried over from senthilts9/cryptodesk, where it lives at tools/. It tests
something no in-process unit test can: that state survives the PROCESS, not just
the function call. Both halves must run as separate interpreters or it proves
nothing -- an in-process check passes on a cache that a real restart would lose.

Run as:
    PYTHONPATH=backend python backend/tools/crash_invariants.py /tmp/inv.db write
    PYTHONPATH=backend python backend/tools/crash_invariants.py /tmp/inv.db read

Verified against backend/cryptodesk/, whose store.py has diverged from upstream
(roughly 2.5x the size): all six invariants hold.

The two interesting invariants are the ones where losing state causes silent
*wrong behaviour* rather than data loss:

  * a latching kill switch that a restart re-arms is not latching
  * a high-water mark that resets lets a fund charge performance fees on the
    recovery from its own drawdown
"""
import os
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptodesk.chain import StrategyVault
from cryptodesk.core import InstrumentRegistry, OrderRequest, OrderType, Side
from cryptodesk.execution import OrderManager
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.risk.limits import RiskEngine, RiskLimits
from cryptodesk.store import Store
from cryptodesk.venues import PaperVenue

DB, MODE = sys.argv[1], sys.argv[2]
store = Store(DB)
reg = InstrumentRegistry()
venue = PaperVenue(reg, starting_collateral=Decimal("100000"), store=store)
venue.seed(synthetic_book("BTC-USD-PERP", Decimal("60000"),
                          levels=10, depth_per_level=Decimal("1")))
risk = RiskEngine(RiskLimits.permissive(), store=store)
oms = OrderManager(venue, risk, store=store)

if MODE == "write":
    oms.submit(OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.MARKET,
                            Decimal("0.01"), client_order_id="done-1", nonce=7))
    print("  order done-1 submitted, nonce 7 consumed")

    risk.kill_switch.trip("operator halted the desk")
    print("  kill switch tripped")

    vault = StrategyVault(management_fee_bps=Decimal("0"))
    vault.deposit(Decimal("100000"), "alice")
    vault.allocate(Decimal("100000"))
    vault.mark(collateral=Decimal("120000"))          # +20%, sets the HWM
    vault.save(store, name="demo")
    print("  vault HWM set to %s" % vault.high_water_mark)
    print("  fees so far %s" % vault.fees_accrued.quantize(Decimal("0.01")))

    sys.stdout.flush()
    os._exit(137)

# -- read side -------------------------------------------------------------
print("  store: %s" % store.stats())
ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and condition
    print("  %-46s %s %s" % (label, "HOLDS" if condition else "*** VIOLATED ***",
                             detail))


check("order blotter survived", oms.get("done-1") is not None,
      "status=%s" % (oms.get("done-1").status.value if oms.get("done-1") else "-"))
check("client_order_id still consumed",
      "done-1" in venue.replay.client_order_ids)
check("nonce still consumed", 7 in venue.replay.nonces)
check("kill switch still latched", risk.kill_switch.tripped,
      "(%s)" % risk.kill_switch.reason)

vault = StrategyVault(management_fee_bps=Decimal("0"))
vault.load(store, name="demo")
check("vault high-water mark preserved",
      vault.high_water_mark > Decimal("1"), "hwm=%s" % vault.high_water_mark)
check("vault share ledger preserved",
      "alice" in vault.holders and vault.holders["alice"].shares > 0,
      "alice=%s shares" % (vault.holders["alice"].shares
                           if "alice" in vault.holders else 0))

# The money consequence: a drawdown then recovery back below the old HWM must
# not be charged. With a reset HWM it would be.
fees_before = vault.fees_accrued
vault.mark(collateral=Decimal("100000"))   # drawdown
vault.mark(collateral=Decimal("118000"))   # recovery, still under the old HWM
check("no fee charged recovering below the HWM",
      vault.fees_accrued == fees_before,
      "delta=%s" % (vault.fees_accrued - fees_before).quantize(Decimal("0.01")))

sys.exit(0 if ok else 1)
