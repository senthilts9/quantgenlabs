"""Run the cash-and-carry basis backtest end to end.

    python backend/run_backtest.py                 # demo (simulated series)
    python backend/run_backtest.py --csv data.csv  # real exchange export

Outputs: console tearsheet, equity_curve.csv, and tearsheet.png
(equity curve + drawdown + cumulative funding).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from app.core.backtest import (BasisConfig, load_csv, print_tearsheet,
                               run_basis_backtest, simulate, tearsheet)


def make_chart(res, ts, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bg, panel, ink, dim = "#0E1420", "#141C2A", "#E6ECF3", "#8A99AE"
    teal, amber, blue = "#38BFA6", "#E0A44E", "#5B9BF0"
    eq = res.equity
    peak = eq.cummax()
    dd = (eq / peak - 1.0) * 100
    # cumulative funding series (reconstruct from funding bars)
    md = res.data
    fund_notional = (md.funding * (res.equity.iloc[0]))  # approx notional≈capital
    cum_fund = np.cumsum(fund_notional)

    fig, ax = plt.subplots(3, 1, figsize=(10, 9), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1.3, 1.3]})
    fig.patch.set_facecolor(bg)
    for a in ax:
        a.set_facecolor(panel)
        a.tick_params(colors=dim, labelsize=8)
        for sp in a.spines.values():
            sp.set_color("#233144")
        a.grid(True, color="#1A2536", lw=0.6)

    ax[0].plot(eq.index, eq.values, color=teal, lw=1.6)
    ax[0].set_title("Cash-and-Carry Basis — Equity Curve", color=ink, fontsize=13, loc="left")
    ax[0].set_ylabel("Equity ($)", color=dim, fontsize=9)
    txt = (f"CAGR {ts['cagr_pct']:.1f}%   Sharpe {ts['sharpe']:.2f}   "
           f"Sortino {ts['sortino']:.2f}   Calmar {ts['calmar']:.2f}   "
           f"MaxDD {ts['max_drawdown_pct']:.1f}%")
    ax[0].text(0.01, 0.06, txt, transform=ax[0].transAxes, color=dim, fontsize=9,
               family="monospace")

    ax[1].fill_between(dd.index, dd.values, 0, color=amber, alpha=0.5, lw=0)
    ax[1].set_ylabel("Drawdown (%)", color=dim, fontsize=9)

    ax[2].plot(md.index, cum_fund, color=blue, lw=1.4)
    ax[2].set_ylabel("Cum. funding ($)", color=dim, fontsize=9)
    ax[2].set_xlabel("")

    plt.tight_layout()
    fig.savefig(out, dpi=130, facecolor=bg)
    print(f"saved chart → {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="real export (timestamp,spot,perp_mark,funding_rate)")
    ap.add_argument("--xrpl-record", metavar="OUT.csv",
                    help="record live XRPL spot + Binance funding to CSV, then backtest it")
    ap.add_argument("--network", default="mainnet", help="xrpl network or raw URL")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--borrow", type=float, default=0.03)
    ap.add_argument("--out", default="backend")
    args = ap.parse_args()

    if args.xrpl_record:
        from app.core.backtest import cefi_feed, xrpl_feed
        print(f"recording live XRPL spot + Binance funding → {args.xrpl_record} ...")
        xrpl_feed.record(args.xrpl_record, minutes=args.minutes, interval_s=60,
                         network=args.network, perp_source=cefi_feed.perp_source("XRPUSDT"))
        md = load_csv(args.xrpl_record)
    elif args.csv:
        md = load_csv(args.csv)
    else:
        md = simulate(days=args.days)
    print(f"loaded {len(md):,} hourly bars  ({md.index[0].date()} → {md.index[-1].date()})")
    res = run_basis_backtest(md, BasisConfig(borrow_apr=args.borrow))
    ts = tearsheet(res)
    print_tearsheet(ts)

    out = Path(args.out)
    res.equity.to_csv(out / "equity_curve.csv")
    make_chart(res, ts, out / "tearsheet.png")


if __name__ == "__main__":
    main()
