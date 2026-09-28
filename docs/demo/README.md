# Demo walkthrough

A 20–25s loop that shows the platform is real and runnable. Capture it with
the Playwright script (`capture_demo.mjs`) driving the live app, or record
manually following the beats below.

## The beats (what the GIF should show)

1. **Smart Order Routing** (4s) — *the primary workflow, lead with it*. Venue
   table (CLOB + AMM), the cost-minimising allocation, execution quality vs
   benchmarks (**7.2 bps** better than best-single-venue), and the ML fill model
   (AUC 0.81). Toggle the ML model to show the allocation shift live. Then read
   the GenAI execution rationale. **This is the opening hook.**
2. **Overview** (2s) — composite risk dial, VaR contribution, alerts.
3. **Market Risk** (2.5s) — cross-method VaR/ES table, the 250-day backtest
   exception strip, EVT tail diagnostics. Signals real risk depth.
4. **Pricing → American (LSM)** (4s) — toggle European→American; land on the
   LSM price, the early-exercise premium, and the "deterministic across
   threads · validated vs binomial" callout. **This is the money shot** for a
   quant-dev role.
5. **Digital Assets desk** (5s) — funding/carry, then drag the liquidation leverage and
   the impermanent-loss slider so the numbers move live.
6. **Observability** (3s) — engine p50/p95/p99 table, C++-vs-numpy speedup, and
   the determinism guarantees panel.
7. **Risk Copilot** (3s) — click a suggested question, show the desk-grounded
   answer streaming in.

## Capture

```bash
# 1. run the app
docker compose up --build            # or: cd frontend && npm run dev
# 2. record (drives the real UI)
npm i -D playwright && npx playwright install chromium
node docs/demo/capture_demo.mjs
# 3. webm → optimized GIF
ffmpeg -i docs/demo/_raw/*.webm -vf "fps=12,scale=1000:-1:flags=lanczos" -f gif - \
  | gifsicle --optimize=3 --colors 200 > docs/demo/demo.gif
```

Then reference it at the top of the README:  `![demo](docs/demo/demo.gif)`

## Talking points (for the interview, not the GIF)

- "Smart Order Routing is the primary workflow — marginal-cost allocation gets
  7.2 bps better execution than the best single venue on a representative
  multi-venue book, with an ML fill model (AUC 0.81) and a grounded GenAI
  rationale. Live books come from CCXT across Binance/OKX/Kraken/KuCoin."
- "Threaded Longstaff-Schwartz, deterministic across thread counts, validated
  against a binomial tree."
- "The basis backtest's edge is funding minus fees minus financing — price P&L
  is ≈0 by construction, and I test for it."
- "One `timed()` seam feeds both Prometheus and the in-terminal profiler, so
  Grafana and the console can't disagree."
- "C++ wins the Monte Carlo loop; numpy wins the VaR quantile — the selector
  routes each to its faster impl rather than assuming C++ is always faster."
