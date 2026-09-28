"""Tearsheet — the numbers a PM reads first.

All annualised on a 365-day crypto clock. Sharpe/Sortino use rf=0 (add a
funding-of-cash term if you want excess returns). Calmar = CAGR / |maxDD|,
the ratio that punishes deep drawdowns the way a risk manager actually cares
about.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestResult

CRYPTO_DAYS = 365


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def tearsheet(res: BacktestResult) -> dict:
    eq = res.equity
    daily = res.daily_returns
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = (eq.index[-1] - eq.index[0]).days / 365.0
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    vol = float(daily.std() * np.sqrt(CRYPTO_DAYS))
    mean = float(daily.mean() * CRYPTO_DAYS)
    downside = daily[daily < 0].std() * np.sqrt(CRYPTO_DAYS)
    mdd = max_drawdown(eq)
    total_pnl = float(eq.iloc[-1] - eq.iloc[0])
    a = res.attribution
    return {
        "total_return_pct": round(total_return * 100, 3),
        "cagr_pct": round(cagr * 100, 3),
        "ann_vol_pct": round(vol * 100, 3),
        "sharpe": round(mean / vol, 3) if vol else 0.0,
        "sortino": round(mean / float(downside), 3) if downside else 0.0,
        "calmar": round(cagr / abs(mdd), 3) if mdd else 0.0,
        "max_drawdown_pct": round(mdd * 100, 3),
        "win_rate_pct": round(float((daily > 0).mean()) * 100, 1),
        "trades": res.trades,
        "turnover_x": round(res.turnover, 2),
        "pnl_total": round(total_pnl, 0),
        "pnl_attribution": {k: round(v, 0) for k, v in a.items()},
        "funding_capture_pct": round(a["funding"] / total_pnl * 100, 1) if total_pnl else 0.0,
    }


def print_tearsheet(ts: dict) -> None:
    print("\n" + "=" * 46)
    print("  CASH-AND-CARRY BASIS — TEARSHEET")
    print("=" * 46)
    rows = [
        ("Total return", f"{ts['total_return_pct']:>8.2f}%"),
        ("CAGR", f"{ts['cagr_pct']:>8.2f}%"),
        ("Ann. volatility", f"{ts['ann_vol_pct']:>8.2f}%"),
        ("Sharpe", f"{ts['sharpe']:>8.2f}"),
        ("Sortino", f"{ts['sortino']:>8.2f}"),
        ("Calmar", f"{ts['calmar']:>8.2f}"),
        ("Max drawdown", f"{ts['max_drawdown_pct']:>8.2f}%"),
        ("Win rate (daily)", f"{ts['win_rate_pct']:>8.1f}%"),
        ("Trades / turnover", f"{ts['trades']} / {ts['turnover_x']}x"),
    ]
    for k, v in rows:
        print(f"  {k:<20}{v}")
    print("  " + "-" * 42)
    print(f"  {'Total P&L':<20}${ts['pnl_total']:>12,.0f}")
    for k, v in ts["pnl_attribution"].items():
        print(f"    {k:<18}${v:>12,.0f}")
    print(f"  Funding capture: {ts['funding_capture_pct']}% of P&L")
    print("=" * 46)
