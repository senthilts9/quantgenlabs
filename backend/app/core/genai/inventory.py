"""Model inventory — every model in the platform, with its validation status.

Framed as a model register in the SR 11-7 sense rather than a glossary: for
each model, what it does, what it assumes, how it was validated, and where it
breaks. A model library that lists formulas without limitations is marketing;
the limitations are the part a model-risk reviewer reads first.

`validation` records checks that were ACTUALLY RUN against this codebase, with
the observed number. `status` is deliberately conservative:

    validated   -- an independent check reproduces the result (a second method,
                   an analytic identity, or an invariant that must hold)
    tested      -- exercised and behaving sensibly, but with no independent
                   cross-check to confirm it
    illustrative-- runs, but on synthetic or uncalibrated inputs; the output
                   demonstrates the mechanism, not a market-ready number
"""
from __future__ import annotations

MODELS = [
    {
        "id": "bsm", "name": "Black-Scholes-Merton", "category": "Pricing",
        "engine": "C++ / numpy",
        "purpose": "Closed-form European option price and Greeks.",
        "assumptions": "Lognormal spot, constant vol and rates, no early exercise, "
                       "frictionless continuous hedging.",
        "limitations": "No volatility smile; European only; constant-vol assumption "
                       "is wrong in every real market.",
        "status": "validated",
        "validation": [
            "8,400-case Greeks boundary sweep (vol 0.001-3.0, T 1d-30y, "
            "rates -2% to +20%): 0 violations of delta in [-1,1], gamma >= 0, vega >= 0",
            "put-call parity exact to 0.00e+00",
            "call_delta - put_delta = 1.000000000000 at q=0",
            "agrees with binomial and Monte Carlo to the cent",
        ],
    },
    {
        "id": "crr", "name": "Cox-Ross-Rubinstein binomial", "category": "Pricing",
        "engine": "C++ / numpy",
        "purpose": "Lattice pricing; handles American early exercise.",
        "assumptions": "Discrete recombining tree converging to lognormal.",
        "limitations": "Convergence oscillates with odd/even step counts -- the "
                       "'sawtooth'. At extreme vol x maturity the error at a fixed "
                       "step count can exceed a cent, which is why the parity test "
                       "against BSM uses a price-scaled tolerance rather than absolute.",
        "status": "validated",
        "validation": ["matches BSM within a price-scaled tolerance across the "
                       "full property-test input range",
                       "American >= European asserted for all inputs"],
    },
    {
        "id": "mc", "name": "Monte Carlo (antithetic)", "category": "Pricing",
        "engine": "C++ parallel",
        "purpose": "Path pricing where closed form does not exist.",
        "assumptions": "GBM paths; variance reduction via antithetic variates.",
        "limitations": "Standard error falls as 1/sqrt(N) -- narrowing the CI by 10x "
                       "costs 100x the paths. Quote the CI, not just the point estimate.",
        "status": "validated",
        "validation": ["bitwise-identical price across 1/2/4/8/16 threads",
                       "measured 48.3M paths/s single-thread on m7i-flex.large",
                       "converges to the BSM analytic price within its stated CI"],
    },
    {
        "id": "lsm", "name": "Longstaff-Schwartz American MC", "category": "Pricing",
        "engine": "C++ parallel",
        "purpose": "American optionality by regression on continuation value.",
        "assumptions": "Basis functions span the continuation value adequately.",
        "limitations": "Biased low with too few paths or a poor basis; the regression "
                       "step is where the model risk actually sits.",
        "status": "validated",
        "validation": ["matches a fine binomial lattice to ~3 decimals "
                       "(4.4864 vs 4.4867 at S=36, sigma=0.20)",
                       "deterministic across thread counts"],
    },
    {
        "id": "bootstrap", "name": "Par-to-zero bootstrap", "category": "Rates",
        "engine": "numpy",
        "purpose": "Zero curve from quoted par yields; discounting for all bond maths.",
        "assumptions": "Semiannual Treasury conventions; linear interpolation in "
                       "zero space with flat extrapolation past the ends.",
        "limitations": "Flat extrapolation beyond 30Y is a choice, not a fact -- "
                       "linear extrapolation there can drive discount factors negative.",
        "status": "validated",
        "validation": ["reprices every quoted par bond back to par at 0.0000bp"],
    },
    {
        "id": "krd", "name": "Key rate durations", "category": "Rates",
        "engine": "numpy",
        "purpose": "Per-tenor rate sensitivity via triangular par-curve bumps.",
        "assumptions": "Tent-shaped bump weights partition the quoted tenor grid.",
        "limitations": "Defined on the quoted grid, so the sum reconciles to "
                       "effective duration only up to grid resolution.",
        "status": "validated",
        "validation": ["sum(KRD) 8.139443 vs effective duration 8.139445 "
                       "-> residual 1e-06"],
    },
    {
        "id": "curve_pca", "name": "Curve PCA (Litterman-Scheinkman)",
        "category": "Rates / ML", "engine": "numpy SVD",
        "purpose": "Level/slope/curvature factors; coherent shocks and factor VaR.",
        "assumptions": "Covariance of daily CHANGES is stable over the window.",
        "limitations": "3 factors explain 91.3% here, below the textbook 95-99%, "
                       "because the 1M-4M bills move idiosyncratically on policy and "
                       "supply. Fitted on changes, not levels -- PCA on near-unit-root "
                       "levels mostly recovers the sample mean.",
        "status": "validated",
        "validation": ["Level loads all-positive; Slope flips -0.22 -> +0.38; "
                       "Curvature shows the wings-vs-belly shape",
                       "reconstruction RMSE 0.98bp at 3 factors",
                       "factor VaR/DV01 implies a 7.88bp adverse move at 99% 1d "
                       "-- a sane magnitude, and the check that caught a 100x "
                       "bp/percent scaling bug"],
    },
    {
        "id": "ns", "name": "Nelson-Siegel", "category": "Rates",
        "engine": "numpy",
        "purpose": "Smooth parametric curve; residuals are the rich/cheap signal.",
        "assumptions": "Three-factor exponential form describes the term structure.",
        "limitations": "Deliberately does NOT reprice inputs exactly -- the residual "
                       "IS the output. Lambda is grid-searched with an exact linear "
                       "solve because a joint non-linear fit on 14 noisy points "
                       "converges to local minima with absurd lambda.",
        "status": "tested",
        "validation": ["fit RMSE 7.67bp, R^2 0.974 on the live curve"],
    },
    {
        "id": "var", "name": "VaR / ES suite", "category": "Market risk",
        "engine": "numpy",
        "purpose": "Historical, parametric, Cornish-Fisher and Monte Carlo VaR/ES.",
        "assumptions": "Parametric assumes normality; historical assumes the past "
                       "window represents the future.",
        "limitations": "Parametric understates fat tails by construction. Both are "
                       "reported side by side so the gap is visible rather than "
                       "hidden behind whichever number looks better.",
        "status": "validated",
        "validation": ["VaR <= ES asserted for all inputs (property test)",
                       "Kupiec and Christoffersen backtests implemented with "
                       "Basel traffic-light zones"],
    },
    {
        "id": "component_var", "name": "Component VaR (Euler)",
        "category": "Market risk", "engine": "numpy",
        "purpose": "Attribute portfolio risk to each position; additive by construction.",
        "assumptions": "Parametric (normal) VaR, which is what makes Euler exact.",
        "limitations": "Exact additivity holds only for the parametric measure; the "
                       "historical VaR shown alongside decomposes only approximately.",
        "status": "validated",
        "validation": ["sum(component VaR) - portfolio VaR = 0.0 exactly",
                       "hedged book shows lower VaR than long-only (sign convention)",
                       "date-alignment fix: SP500/VIX correlation -0.83 (was +0.63 "
                       "under positional alignment -- a hedge appeared to ADD risk)"],
    },
    {
        "id": "risk_parity", "name": "Risk parity / min-variance / max-Sharpe",
        "category": "Portfolio", "engine": "numpy",
        "purpose": "Portfolio construction on the live covariance estimate.",
        "assumptions": "Long-only, fully invested; covariance is stable.",
        "limitations": "Max-Sharpe uses sample-mean expected returns and will "
                       "overfit them badly -- it is the least stable of the three "
                       "out of sample. Min-variance and risk parity use only "
                       "covariance and are far more robust.",
        "status": "validated",
        "validation": ["risk parity contributions equal to 0.00000 dispersion",
                       "min-variance achieves the lowest vol of all methods",
                       "fixed-point solver replaced a projected-gradient version "
                       "that converged to a corner (100% in the highest-vol asset)"],
    },
    {
        "id": "sor", "name": "Smart order router", "category": "Execution",
        "engine": "numpy",
        "purpose": "Marginal-cost allocation across CLOB and AMM venues.",
        "assumptions": "Displayed depth is executable, discounted by fill probability.",
        "limitations": "Static snapshot -- no queue position, no adverse selection "
                       "model, and it does not model the market's reaction to the "
                       "order beyond walking the book.",
        "status": "tested",
        "validation": ["live CCXT books across Binance/OKX/Kraken/KuCoin",
                       "improvement measured against best-single-venue and pro-rata "
                       "benchmarks on the same book"],
    },
    {
        "id": "fill_model", "name": "Fill-probability model (logistic)",
        "category": "Execution / ML", "engine": "numpy",
        "purpose": "Discount venue size by P(fill) so the router avoids cheap but "
                   "unreachable liquidity.",
        "assumptions": "Fill probability is logistic in size/depth, latency, spread "
                       "and volatility.",
        "limitations": "TRAINED ON SYNTHETIC DATA from a logistic generator -- "
                       "fitting a logistic model to a logistic DGP mostly proves the "
                       "optimiser works. It demonstrates the pipeline, not predictive "
                       "power on real fills. Retraining on live OMS fills begins once "
                       "there are enough labelled rows.",
        "status": "illustrative",
        "validation": ["OUT-OF-SAMPLE on a 30% held-out split: AUC 0.82, "
                       "Brier 0.165 vs 0.230 base rate (skill 0.28)",
                       "calibration curve reported -- the router multiplies size by "
                       "this probability, so over-confidence means over-allocation"],
    },
    {
        "id": "tca", "name": "Implementation shortfall / TCA",
        "category": "Execution", "engine": "python",
        "purpose": "Execution cost vs arrival price, attributed by algo and venue.",
        "assumptions": "Arrival mid is the correct decision-time benchmark.",
        "limitations": "Child fills book at the routed VWAP because the router does "
                       "not publish per-venue prints, so venue attribution is "
                       "coarser than a real print-level TCA.",
        "status": "tested",
        "validation": ["costs signed so positive is a cost on both sides",
                       "opportunity cost charged on the unfilled remainder",
                       "parents excluded from venue stats to avoid double-counting"],
    },
    {
        "id": "amm", "name": "Constant-product AMM / impermanent loss",
        "category": "Digital assets", "engine": "C++ / python",
        "purpose": "On-chain swap pricing and LP impermanent loss.",
        "assumptions": "x*y=k with a fixed fee; no concentrated liquidity.",
        "limitations": "Does not model Uniswap-v3 style ranges or MEV.",
        "status": "validated",
        "validation": ["IL exact and symmetric: -20.00% at both 4x and 0.25x"],
    },
    {
        "id": "evt", "name": "EVT peaks-over-threshold", "category": "Digital assets",
        "engine": "numpy",
        "purpose": "Tail risk where the Gaussian assumption fails hardest.",
        "assumptions": "Excesses over a high threshold follow a GPD.",
        "limitations": "Threshold choice materially changes the estimate and there "
                       "is no objective rule for it; small samples make xi unstable.",
        "status": "tested",
        "validation": ["EVT ES 22.90% exceeds empirical ES 22.10% -- captures the "
                       "fatter tail, as intended"],
    },
    {
        "id": "copilot", "name": "Claude risk copilot", "category": "GenAI",
        "engine": "claude-haiku-4-5",
        "purpose": "Natural-language questions grounded in live desk state.",
        "assumptions": "The injected desk snapshot is complete and current.",
        "limitations": "No answer-quality score. Grounding constrains but does not "
                       "guarantee correctness; the model can still misread the JSON "
                       "it is given. Treat output as a reading aid, not a control.",
        "status": "tested",
        "validation": ["reproduces blotter figures that tie out to the OMS and "
                       "Limits screens",
                       "degrades to a deterministic local summary with no API key"],
    },
]


def inventory() -> dict:
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for m in MODELS:
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
        by_category[m["category"]] = by_category.get(m["category"], 0) + 1
    return {
        "models": MODELS,
        "total": len(MODELS),
        "by_status": by_status,
        "by_category": by_category,
        "status_definitions": {
            "validated": "an independent check reproduces the result -- a second "
                         "method, an analytic identity, or an invariant that must hold",
            "tested": "exercised and behaving sensibly, but with no independent "
                      "cross-check to confirm it",
            "illustrative": "runs, but on synthetic or uncalibrated inputs; shows "
                            "the mechanism, not a market-ready number",
        },
    }
