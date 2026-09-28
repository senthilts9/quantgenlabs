# QuantGenLabs — Cross-Asset Execution & Risk Console

A production-shaped **cross-asset trading platform** whose primary workflow is
**ML/GenAI-driven Smart Order Routing (best execution)** across venues, built
on a verified derivatives-pricing and market-risk core in **numpy + C++**
(including a deterministic, threaded Longstaff-Schwartz American MC). Traditional
markets (equities, rates, FX) are the main product; **digital assets are one
self-contained module** (perpetual swaps, on-chain AMM liquidity, EVT tail risk).
Full observability (Prometheus + Grafana), a Claude copilot, and a React console.

![architecture](docs/architecture.svg)

**Documentation:** [architecture](docs/architecture.md) ·
[gap analysis vs the institutional spec](docs/gap-analysis.md) ·
[benchmark methodology](docs/benchmark-methodology.md)

> **Scope, stated up front.** FIX connectivity targets an **in-process simulated
> venue** only — nothing here reaches a real exchange and no money moves. Kafka,
> PostgreSQL, Delta Lake, gRPC, distributed tracing and auth/RBAC are **not built**;
> [`docs/gap-analysis.md`](docs/gap-analysis.md) says so section by section rather
> than leaving it to be inferred.

### Highlights a reviewer can verify in one clone

- **Smart Order Routing (primary)** — marginal-cost allocation across CLOB and
  AMM venues delivering **7.2 bps** implementation-shortfall improvement over the
  best single venue, an **ML fill-probability model (ROC-AUC 0.82)** that
  down-weights cheap-but-unreachable liquidity, and a **GenAI rationale** grounded
  in the routing result for the desk/compliance.
- **Deterministic parallel pricing** — Monte Carlo *and* Longstaff-Schwartz
  American MC are **bitwise-identical across 1/2/4/8/16 threads**; LSM validated
  against a binomial lattice to ~3 decimals.
- **Real performance story** — C++ Monte Carlo **39.4M paths/s** single-thread
  (median of 7; 41.0M best) rising to **70.9M with AVX2+FMA at zero accuracy cost
  — bitwise-identical price**; ~5× numpy, O(1) memory; the engine selector routes each call to its faster
  impl and Grafana shows the p50/p99 split.
- **A strategy that runs** — cash-and-carry basis backtest with honest P&L
  attribution (funding vs fees vs financing): Sharpe 0.98, Calmar 1.29, max DD −3.5%.
- **Tested** — **229 tests in CI**: 126 zero-dependency C++ tests (order book, feed
  quality gate, FIX wire + session, pre-trade risk, matching engine, end-to-end FIX
  venue) and 103 Python tests including a property-based suite (pytest + Hypothesis)
  and a C++/Python parity suite that runs the same assertions against both
  implementations. Covers put-call parity, VaR ≤ ES, routing optimality, IL
  symmetry, price-time priority and bitwise determinism.

```bash
python backend/run_sor.py                          # offline synthetic multi-venue demo
python backend/run_sor.py --symbol BTC/USDT --live # real books via CCXT (Binance/OKX/Kraken/KuCoin)
```
> Reported SOR figures (7.2 bps, AUC 0.82 — as `verify_sor.py` prints it) are from a representative multi-venue
> book; `--live` routes across real exchange order books via CCXT.


## Market connectivity core (C++20)

The low-latency path an order actually travels, header-only and separately tested.
`cpp/src/lifecycle_demo.cpp` walks all of it and prints every FIX message:

```bash
g++ -std=c++20 -O2 -pthread -Icpp/include cpp/src/lifecycle_demo.cpp -o demo && ./demo
```

| Component | What it does |
|---|---|
| `mkt::book::Ladder` | L2 book on **integer ticks** — a `double`-keyed book puts 1.10 on two levels. Flat array indexed by `tick - floor`: O(1) update, zero allocation. SoA, cached best with lazy repair, crossed-book detection. |
| `mkt::feed::Validator` | Separates the three faults that look identical: **gap** (lost — recover), **duplicate** (retransmitting), **reorder** (not preserving order). EWMA outlier detection, disarmed until warm. Every rejection counted. |
| `mkt::fix::Message` / `Builder` | Zero-copy parse into `(tag, string_view)`; allocation-free encode. Gets BodyLength, the 3-digit zero-padded CheckSum and mandatory field order right — pinned against the FIX spec's own reference Logon, not our own output. |
| `mkt::fix::Session` | Sequencing, heartbeats, TestRequest, resend with PossDupFlag. **Clock is injected**, so timeouts are tested at exact intervals with no sleeping. |
| `mkt::risk::PreTradeGate` | Kill switch (one atomic load, first and unconditional), fat-finger on both quantity *and* price, collars in bps, notional, position limits on the **resulting** position, rate limiting. Refuses when the feed is stale rather than waving orders through. |
| `mkt::venue::MatchingEngine` | Price-time priority with the FIFO in the data structure, not the iteration order. IOC/FOK (FOK decided before anything prints), self-match prevention, amend that loses priority on a size increase. No clocks, no randomness — a deterministic test oracle. |
| `mkt::venue::FixVenue` | Composes the three. Simulated venue only (spec rule 6): nothing real, no money. ExecutionReport invariants asserted — ExecType vs OrdStatus, `LeavesQty + CumQty == OrderQty`, cumulative AvgPx. |

### Measured performance

Reproduce with `./benchmarks/run.sh`. Full methodology, including why no 8-core
figure is published, in [`docs/benchmark-methodology.md`](docs/benchmark-methodology.md).

Intel i5-7360U, **2 physical / 4 logical cores**, clang 14, `-O3`, 20M paths,
median of 7 runs:

| Monte Carlo | Baseline | AVX2+FMA | gain |
|---|---|---|---|
| 1 thread | 39.4M paths/s | 70.9M paths/s | 1.80× |
| 2 threads (real cores) | 78.8M paths/s | 128.1M paths/s | 1.63× |
| 4 threads (**SMT, not 4 cores**) | 102.8M paths/s | 153.3M paths/s | 1.49× |

- The **~34M paths/sec single-thread objective is met with no SIMD flags at all**,
  and holds at worst case (slowest of 7 = 35.8M).
- AVX2 costs **no accuracy**: the vectorised build returns a price *bitwise
  identical* to the scalar one (9.415557633681 in both).
- **No 8-core figure is reported.** This host has 2 physical cores; the harness
  emits `eight_physical_core_measured: false` and fills it in automatically when
  run on suitable hardware. A per-core number multiplied by 8 would look like a
  measurement and would not be one.
- Every figure carries best/worst/spread. Spreads reach 43% on this throttling
  mobile CPU, so **the ratios are the result and the absolute values are
  approximate**. One row (depth snapshots, 79% spread) is labelled
  do-not-quote rather than quietly dropped.

| Connectivity path, single thread | Median |
|---|---|
| Top-of-book reads | 380.4M/s |
| Pre-trade risk checks | 89.0M/s |
| Feed validations | 29.3M/s |
| Order book updates | 24.8M/s |
| FIX encode (NewOrderSingle) | 6.6M/s |
| **End-to-end order path** | **5.2M/s** |
| FIX parse (BodyLength + CheckSum validated) | 2.6M/s |

End-to-end = validate tick → book update → top-of-book read → pre-trade risk →
encode NewOrderSingle. **Excludes network, kernel and venue time**, which dominate
real wire-to-wire latency.


```
quantgenlabs/
├── cpp/                         C++20 engines (header-only) + benches + pybind11
│   ├── include/mkt/             MARKET CONNECTIVITY CORE
│   │   ├── book/                ladder — L2 order book, integer ticks, O(1) update
│   │   ├── feed/                validator — gap/dup/reorder/stale/outlier gate
│   │   ├── fix/                 message (zero-copy wire) · session (seq, resend)
│   │   ├── risk/                pretrade — kill switch, collars, position limits
│   │   └── venue/               matching_engine (price-time) · fix_venue
│   ├── include/quant/           quant_engine · parallel · quant_parallel · lsm
│   ├── include/xrpl/            crypto_engine
│   ├── src/                     *_bench / *_sim / *_bindings
│   │                            mkt_bench (JSON) · lifecycle_demo
│   └── tests/                   8 binaries, 126 tests, zero-dependency harness
├── backend/
│   ├── app/
│   │   ├── core/
│   │   │   ├── pricing/         BSM+Greeks · CRR binomial · Monte Carlo
│   │   │   ├── risk/            VaR/ES (hist·param·CF·MC) · component VaR · backtests
│   │   │   ├── crypto/          perps · amm · liquidation · EVT risk
│   │   │   └── engines.py       cpp|numpy selector, instrumented via timed()
│   │   ├── obs/                 Prometheus metrics + in-process profiler
│   │   ├── services/            tick_loop · copilot · desk_context
│   │   ├── api/                 pricing · risk · observability routers
│   │   └── main.py             unified FastAPI (pricing·risk·crypto·copilot·/metrics)
│   └── verify/                  verify_core.py · verify_crypto.py
├── frontend/                    React + Vite console (all menus, live demo screens)
├── benchmarks/                  run.sh + measured results-*.json (see docs)
├── docs/                        architecture · gap-analysis · benchmark-methodology
├── observability/               prometheus.yml + auto-provisioned Grafana dashboard
├── deploy/                      Dockerfile.api · Dockerfile.web
├── docker-compose.yml           api + web + prometheus + grafana
└── .github/workflows/ci.yml     builds C++, verifies math, builds frontend
```

## Quickstart

```bash
# 1. verify the math (numpy/scipy only, instant)
pip install numpy scipy
python backend/verify/verify_core.py     # pricing + VaR + component VaR
python backend/verify/verify_crypto.py   # perps · AMM · liquidation · EVT

# 2. C++ engines: compile clean (-O3 -Wall -Wextra), cross-check, prove determinism
g++ -std=c++20 -O3 -Wall -Wextra -pthread -Icpp/include cpp/src/lsm_bench.cpp -o /tmp/lsm && /tmp/lsm
cmake -S cpp -B cpp/build && cmake --build cpp/build -j    # also builds pybind11 modules

# 2b. the whole C++ test suite (126 tests, no test framework to install)
for t in test_engines test_book test_feed test_fix test_fix_session \
         test_pretrade test_matching test_fix_venue; do
  g++ -std=c++20 -O2 -Wall -Wextra -pthread -Icpp/include -Icpp/tests \
      cpp/tests/$t.cpp -o /tmp/$t && /tmp/$t
done

# 2c. watch a whole order lifecycle, with every FIX message printed
g++ -std=c++20 -O2 -pthread -Icpp/include cpp/src/lifecycle_demo.cpp -o /tmp/demo && /tmp/demo

# 2d. reproduce the benchmarks (run on an IDLE machine -- see docs/benchmark-methodology.md)
./benchmarks/run.sh

# 3. full stack (API + console + Prometheus + Grafana)
cp .env.example .env            # optionally set ANTHROPIC_API_KEY
docker compose up --build
# console → :5174   API → :8000/api/health   Prometheus → :9090   Grafana → :3000
```

## What's verified (reproduced in CI)

- **Pricing cross-check** — BSM ↔ binomial ↔ Monte Carlo agree to the cent;
  put-call parity to 1e-8; Greeks analytic.
- **American LSM** — Longstaff-Schwartz matches a fine binomial tree to ~3
  decimals (4.4877 vs 4.4867 at S=36, σ=0.20) with the expected early-exercise
  premium.
- **Determinism** — parallel Monte Carlo (7.018587264845) and parallel LSM
  (4.487705381988) are **bitwise-identical across 1/2/4/8/16 threads**.
- **Performance** — C++ Monte Carlo 39.4M paths/s single-thread, median of 7 runs
  (41.0M best case); 70.9M with AVX2+FMA (~5× numpy,
  O(1) memory); the selector routes each engine to its faster impl (numpy still
  wins `var_historical`).
- **Crypto** — impermanent loss exact & symmetric (−20% at 4× and 0.25×);
  liquidation/leverage inversion exact; EVT captures fatter tails than empirical.

## Strategy backtest — cash-and-carry basis

The narrative spine: a delta-neutral **long-spot / short-perp** basis trade
that harvests perpetual funding. An event-driven engine
(`backend/app/core/backtest/`) walks bar by bar, accrues funding on funding
bars, charges taker fees on entry and every rebalance, applies a financing
rate on the spot leg, and attributes P&L to each source. A one-line loader
swap (`simulate()` → `load_csv()`) points it at a real Binance/Deribit export.

```bash
python backend/run_backtest.py --days 365            # demo (simulated series)
python backend/run_backtest.py --csv exchange.csv    # real data
python backend/run_backtest.py --xrpl-record live.csv --minutes 120  # record live, then backtest
# → console tearsheet + equity_curve.csv + tearsheet.png
```

### Data sources (honest about what lives where)

| Leg | Source | Method |
|---|---|---|
| Spot / AMM state | **XRPL public rippled** (`xrpl_feed`) | `amm_info` (real constant-product reserves → IL) · `book_offers` (DEX mid) |
| Perp mark + funding | **CeFi venue** (`cefi_feed`, Binance USD-M) | `premiumIndex` · `fundingRate` history |

XRPL is spot + AMM only — it has **no perpetuals and no funding rate**, so the
basis trade's short leg genuinely must come from a derivatives venue; the
adapter keeps the two explicit rather than faking funding from ledger data. A
rippled node serves *current* state, so `--xrpl-record` streams a live series
to CSV (use a Clio/indexer for deep history). All three paths emit the same
`MarketData` schema and degrade to `simulate()` offline.

Representative demo run (simulated XRP-like perp, 1y, $1M/leg, 3% borrow):

| Metric | Value |
|---|---|
| CAGR | 4.5% |
| Sharpe / Sortino / Calmar | 0.98 / 1.68 / 1.29 |
| Max drawdown | −3.5% |
| **P&L attribution** | funding **+$107k** · price +$2k · fees −$36k · financing −$30k |

The attribution is the point: price P&L is ≈0 (genuinely delta-neutral, and
tested), so the trade's edge is **funding minus fees minus financing** — the
exact sensitivity a basis desk manages. Turnover/fees are a real drag, which
is why the engine models them honestly rather than assuming frictionless fills.

![basis tearsheet](docs/basis_tearsheet.png)

## Testing

Two layers, both in CI:

- **Property-based tests** (`backend/tests/`, pytest + Hypothesis) assert the
  invariants that must hold for *any* inputs — put-call parity, price ≥ 0 and
  monotonic in vol, |delta| ≤ 1, American ≥ European, VaR ≤ ES, component VaR
  sums to portfolio VaR, impermanent loss ≤ 0 and symmetric in 1/r, liquidation
  ordering, funding clamp bounds, EVT tail coverage. Hypothesis shrinks any
  failure to a minimal counterexample. A dependency-free
  `backend/tests/run_local.py` runs the same properties over a random grid where
  pytest isn't available.
- **C++ engine tests** (`cpp/tests/`, zero-dependency `microtest.hpp`) cover
  pricing agreement, parity, the LSM-vs-tree match, the crypto invariants, and
  crucially the **parallel determinism** of both Monte Carlo and LSM
  (bitwise-identical across thread counts).

```bash
cd backend && PYTHONPATH=. pytest tests -q       # full property suite (CI)
python backend/tests/run_local.py                # offline, no test deps
g++ -std=c++20 -O2 -Wall -Wextra -pthread -Icpp/include -Icpp/tests \
    cpp/tests/test_engines.cpp -o /tmp/t && /tmp/t   # 9 C++ tests
```

## Observability

Every engine call runs through one `timed(engine, impl)` seam feeding a
Prometheus histogram, so Grafana shows p50/p95/p99 and the C++-vs-numpy
speedup for **every** hot path — pricing, risk, MC, LSM, and the crypto
tick loop — on one dashboard, auto-provisioned. The console's Observability
screen shows the same numbers in-terminal.

## Claude risk copilot

`/api/copilot` (+ SSE `/stream`) answers grounded in a live desk snapshot
(positions, funding carry, liquidation distance, EVT VaR/ES per symbol).
Model is env-configurable; without a key it degrades to a deterministic
local summary so the console is never dead in a demo.

## Deploy to cloud

The stack is plain containers on standard ports — Render/Railway/ECS/Fly all
work. `deploy/Dockerfile.api` serves the backend; `deploy/Dockerfile.web`
builds the console to static nginx. Set `ANTHROPIC_API_KEY` as a secret.

## Honest scope

The math cores are verified and the C++ compiles/runs clean here. The
pybind11 modules and full multi-core scaling need a build with pybind11 and
>1 core (the CI/your machine); Docker images build on any host with Docker.
Simulated market data is clearly labelled throughout — no live venue keys
are shipped.
