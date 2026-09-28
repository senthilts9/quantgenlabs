"""Route a parent order across LIVE multi-exchange books (CCXT), else synthetic.

    python backend/run_sor.py --symbol BTC/USDT --qty 5 --side buy --live
    python backend/run_sor.py                      # offline synthetic demo

Prints the venue snapshot, the cost-minimising allocation, execution quality
vs benchmarks, the ML fill probabilities, and the GenAI rationale.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core import sor


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--qty", type=float, default=2500.0)
    ap.add_argument("--side", default="buy", choices=["buy", "sell"])
    ap.add_argument("--live", action="store_true", help="pull real books via CCXT")
    ap.add_argument("--exchanges", nargs="*", default=None)
    args = ap.parse_args()

    side = sor.Side(args.side)
    if args.live:
        exs = tuple(args.exchanges) if args.exchanges else sor.ccxt_feed.DEFAULT_EXCHANGES
        venues, mid, source = sor.ccxt_feed.live_or_synth(args.symbol, side, exs)
    else:
        venues, mid = sor.synth_market(side, mid=100.0)
        source = "synthetic"

    print(f"data source: {source}  ·  {len(venues)} venues  ·  consolidated mid {mid:.4f}")
    for v in venues:
        print(f"  {v.venue:10s} {v.vtype.value:4s} fee={v.fee_bps:6.2f}bps "
              f"depth={v.depth():.4f} top={v.best_price():.4f} lat={v.latency_ms:.0f}ms")

    model = sor.train_fill_model()
    fp = sor.fill_probabilities(model, venues, args.qty)
    res = sor.route(args.qty, venues, side, mid, fill_prob=fp)
    s = res.summary()
    print(f"\nSOR: {s['slippage_bps']:.2f} bps shortfall  ·  "
          f"vs best-single {s['benchmarks_bps']['best_single_venue']:.2f}  ·  "
          f"improvement {s['improvement_vs_best_single_bps']:.2f} bps")
    print(f"allocation: {s['allocation']}")
    print(f"ML fill model: {sor.training_metrics(model)}")
    print(f"\nrationale: {sor.explain.explain(res, fp)['explanation']}")


if __name__ == "__main__":
    main()
