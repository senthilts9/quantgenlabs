import type {
  AlgoRun, BookLevel, DeskSession, Instrument, InstrumentSpec, RunDetail,
} from "../types/domain";

/**
 * Phase 2 providers: real backends, same-origin.
 *
 * Two families sit behind this file.
 *
 * `/api/cryptodesk/*` is the stdlib desk package — public venue books, perp
 * funding, and an on-chain Chainlink cross-check. Its market data comes from
 * open public venue APIs (Coinbase, Paradex, Hyperliquid) and public Ethereum
 * RPC. No keys, no entitlements.
 *
 * `/api/risk/*` and `/api/observability/*` are the in-house engines — numpy
 * with an optional C++ (pybind11) fast path, instrumented so the cpp-vs-numpy
 * latency shows up in Prometheus.
 *
 * **Money arrives as strings.** The backend carries exact Decimals and
 * serialises losslessly. Narrowing to `number` happens once, here, because the
 * DOM cannot render a Decimal — doing it per-component would scatter the
 * precision loss across the UI instead of containing it at the boundary.
 */

const DESK = "/api/cryptodesk";

/** Venue default. The API accepts paper|binance|paradex|hyperliquid.
 *  Hyperliquid is the public perps venue with a genuine two-sided book and
 *  no keys; the paradex testnet book is known-broken (BTC ~39.5k vs a 77.5k
 *  mark), so it must not back a price display. */
export const VENUE = "hyperliquid";
const API = "/api";

/* ------------------------------------------------------------------ session */

/**
 * The session lives in an **httpOnly cookie the server sets**, so this module
 * deliberately holds no credential at all.
 *
 * The previous shape — a bearer token in `sessionStorage` — is the one thing
 * TIER1_ARCHITECTURE §2 rules out, and for a concrete reason: anything
 * JavaScript can read, injected JavaScript can exfiltrate, so a single XSS
 * hands over a trading session. An httpOnly cookie is invisible to script,
 * including this script, which is why `getSession()` has to *ask* the server
 * who we are rather than reading a variable.
 *
 * What the browser does keep is the CSRF token, which is meant to be readable:
 * the double-submit check needs same-origin script to echo it in a header, and
 * a cross-origin page can cause the cookie to be sent but cannot read this to
 * match it.
 */
const CSRF_KEY = "qgl.desk.csrf";

let csrfToken: string | null = null;
try { csrfToken = sessionStorage.getItem(CSRF_KEY); } catch { /* private mode */ }

export function setCsrfToken(token: string | null): void {
  csrfToken = token && token.trim() ? token.trim() : null;
  try {
    if (csrfToken) sessionStorage.setItem(CSRF_KEY, csrfToken);
    else sessionStorage.removeItem(CSRF_KEY);
  } catch { /* a non-persistent session is still a usable session */ }
}

export function getCsrfToken(): string | null { return csrfToken; }

async function getJSON<T>(url: string, params: Record<string, string> = {}): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  // `credentials: same-origin` is what actually sends the session cookie.
  // Without it fetch omits cookies and every write 401s while the user is
  // visibly signed in — a confusing failure worth naming.
  const res = await fetch(`${url}${qs ? `?${qs}` : ""}`, { credentials: "same-origin" });
  if (!res.ok) {
    // Surface the server's own message. "Request failed" tells an operator
    // nothing; "no aggregator configured for DOGE-USD" tells them everything.
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); detail = b.detail || b.error || detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      // Echoed from the readable cookie. The server compares the two; a
      // cross-origin page can trigger the cookie but cannot read it to match.
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); detail = b.detail || b.error || detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

/** `null` for absent, `Number` for present. `Number(null)` is 0, which is a price. */
const numOrNull = (x: unknown): number | null =>
  x === null || x === undefined || x === "" ? null : Number(x);

/* ---------------------------------------------------------------- market */

interface RawLevel { price: string; size: string }
interface RawBook {
  market: string; source: string; bids: RawLevel[]; asks: RawLevel[];
  mid: string; mark: string; spread_bps: string;
}

export interface LiveBook {
  levels: BookLevel[]; mid: number; mark: number; spreadBps: number; source: string;
}

/** Cumulative depth, computed outward from the touch on each side. */
function withTotals(raw: RawLevel[], side: "BID" | "ASK"): BookLevel[] {
  let running = 0;
  return raw.map(l => {
    const size = Number(l.size);
    running += size;
    return { price: Number(l.price), size, total: running, side };
  });
}

/* ------------------------------------------------------------------- runs */

/**
 * One `algo_parents` row with its TCA join, narrowed to numbers.
 *
 * Every TCA column is nullable and stays nullable: a run whose benchmark
 * window held no prints has no VWAP slippage, and rendering that as 0.0 bps
 * claims the algo matched VWAP exactly when in fact nothing was measured.
 */
function toRun(r: any): AlgoRun {
  return {
    parentId: r.parent_id, createdMs: r.created_ms, completedMs: r.completed_ms ?? null,
    venue: r.venue, market: r.market, side: r.side, algo: r.algo,
    totalSize: Number(r.total_size), filledSize: Number(r.filled_size),
    avgPrice: numOrNull(r.avg_price), arrivalPrice: Number(r.arrival_price),
    sliceCount: r.slice_count, durationMs: r.duration_ms, status: r.status,
    fillCount: r.fill_count ?? 0,
    // Null for runs placed before wallet sign-in existed. Rendered as
    // "unattributed" rather than backfilled with a plausible name.
    actor: r.actor ?? null,
    // 'mainnet' | 'testnet' | 'simulated'. Null for runs stored before the
    // column existed *and* whose venue publishes both endpoints — for those
    // the network is genuinely unknowable after the fact, and a blank is more
    // honest than a guess about whether real money moved.
    network: r.network ?? null,
    shortfallBps: numOrNull(r.implementation_shortfall_bps),
    vwapSlippageBps: numOrNull(r.vwap_slippage_bps),
    feeBps: numOrNull(r.fee_bps), totalCostBps: numOrNull(r.total_cost_bps),
    participationPct: numOrNull(r.participation_rate_pct),
    benchmarkPrints: numOrNull(r.benchmark_prints),
  };
}

/* --------------------------------------------------------------- funding */

export interface FundingRate {
  venue: string; ratePct: number; periodHours: number; aprPct: number;
  costPer10kPerDay: number; direction: string;
}

/* ------------------------------------------------------------ on-chain */

export interface ChainCheck {
  market: string; chain: string; block: number;
  oraclePrice: number; oracleSource: string; oracleAgeS: number | null;
  cexPrice: number | null; cexSource: string | null;
  divergenceBps: number | null; toleranceBps: number; maxAgeS: number;
  diverged: boolean; stale: boolean; ok: boolean; reason: string | null;
}

/* ------------------------------------------------------------------ risk */

export interface VaRMethodResult {
  method: string; varPct: number; esPct: number; varCurrency: number; esCurrency: number;
}
export interface VaRSuite {
  symbols: string[]; grossExposure: number; confidence: number;
  nObservations: number; alignment: string; methods: VaRMethodResult[];
}

/* ------------------------------------------------------------- perps */

export interface PerpRow {
  market: string; coin: string; network: string;
  mark: number; index: number; basisBps: number;
  fundingAprPct: number | null; openInterestUsd: number;
  dayVolumeUsd: number; change24hPct: number | null; maxLeverage: number | null;
}
export interface PerpBoard {
  network: string; source: string; contractsTotal: number;
  aggregateOiUsd: number; aggregateVolUsd: number; rows: PerpRow[];
}

/* -------------------------------------------------------- crypto risk */

export interface RiskMethodRow {
  method: string; family: string; var_pct: number; es_pct: number;
  var_currency: number; es_currency: number; es_measured?: boolean;
  gpd_xi?: number | null; n_exceedances?: number | null;
}
export interface RiskBacktestRow {
  method: string; observations: number; exceptions: number; expected: number;
  kupiec_p: number; basel_zone: string; coverage_verdict: string;
}
export interface CryptoRisk {
  market: string; historySymbol: string; historyNote: string | null;
  horizon: string; confidence: number; notional: number;
  nReturns: number; clean: boolean; incompleteDropped: number;
  sigmaPct: number; annualisedPct: number; ewmaAnnualisedPct: number;
  regime: string; skew: number; excessKurtosis: number; fatTailed: boolean;
  methods: RiskMethodRow[]; backtests: RiskBacktestRow[]; cppAvailable: boolean;
}

export const liveProviders = {
  /**
   * One market's book, with cumulative totals and the desk's own mark.
   *
   * `depth` defaults to the endpoint's maximum rather than its default of 8.
   * The ticket walks these levels to estimate slippage, and a sweep that runs
   * off the end of a truncated snapshot cannot distinguish "the market is
   * thin" from "we only asked for eight levels".
   */
  async getBook(market: string, venue = VENUE, depth = 25): Promise<LiveBook> {
    const b = await getJSON<RawBook>(`${DESK}/book`, { venue, market, depth: String(depth) });
    // A listed contract with nobody quoting it returns 200 with empty sides
    // and a null mid. `Number(null)` is 0, not NaN, so this would silently
    // render a real-looking book at zero. Fail loudly instead.
    if (!b.bids?.length || !b.asks?.length || b.mid === null || b.mid === undefined) {
      throw new Error(`no two-sided book for ${market} on ${venue}`);
    }
    return {
      // Asks come back best-first; render them descending so the touch sits
      // against the mid, which is how a book is read.
      levels: [...withTotals(b.asks, "ASK").reverse(), ...withTotals(b.bids, "BID")],
      mid: Number(b.mid), mark: Number(b.mark),
      spreadBps: Number(b.spread_bps), source: b.source,
    };
  },

  /** Top-of-book quote for one symbol. */
  async getQuote(market: string, venue = VENUE): Promise<Pick<Instrument, "price" | "bid" | "ask">> {
    const b = await getJSON<RawBook>(`${DESK}/book`, { venue, market });
    return {
      price: Number(b.mid),
      bid: b.bids.length ? Number(b.bids[0].price) : 0,
      ask: b.asks.length ? Number(b.asks[0].price) : 0,
    };
  },

  /** Live perp funding, annualised, across every venue that publishes it. */
  async getFunding(market = "BTC-USD-PERP"): Promise<FundingRate[]> {
    const r = await getJSON<{ rates: any[] }>(`${DESK}/funding`, { market });
    return (r.rates || []).map(x => ({
      venue: x.venue,
      // Rate is per funding period, not annual. Shown as a percentage because
      // the raw decimal (0.00008785) is unreadable on a tile.
      ratePct: Number(x.rate) * 100,
      periodHours: Number(x.period_hours),
      aprPct: Number(x.apr_pct),
      costPer10kPerDay: Number(x.long_cost_per_10k_per_day),
      direction: x.direction,
    }));
  },

  /**
   * Venue mark cross-checked against an independent on-chain oracle.
   *
   * Divergence and staleness are reported separately because they fail
   * differently: a feed that agrees with an old world still agrees.
   */
  async getChainCheck(market = "BTC-USD", chain = "sepolia"): Promise<ChainCheck> {
    const c = await getJSON<any>(`${DESK}/chain`, { market, chain });
    return {
      market: c.market, chain: c.chain, block: c.block,
      oraclePrice: Number(c.oracle_price), oracleSource: c.oracle_source,
      oracleAgeS: c.oracle_age_s ?? null,
      cexPrice: c.cex_price === null ? null : Number(c.cex_price),
      cexSource: c.cex_source ?? null,
      divergenceBps: c.divergence_bps === null ? null : Number(c.divergence_bps),
      toleranceBps: Number(c.tolerance_bps), maxAgeS: Number(c.max_age_s),
      diverged: !!c.diverged, stale: !!c.stale, ok: !!c.ok, reason: c.reason ?? null,
    };
  },

  /** Stored oracle observations, newest first — the audit trail. */
  async getChainHistory(limit = 20, market?: string): Promise<any[]> {
    const p: Record<string, string> = { limit: String(limit) };
    if (market) p.market = market;
    const r = await getJSON<{ observations: any[] }>(`${DESK}/chain/history`, p);
    return r.observations || [];
  },

  /**
   * Instrument constraints, keyed by symbol.
   *
   * The ticket needs tick, step, leverage cap, margin fractions and the fee
   * schedule before it can quote margin or a liquidation price. All of it is
   * venue metadata; none of it is safe to hardcode, because a stale leverage
   * cap in the bundle produces a ticket that offers 50x on a 20x market.
   */
  async getSpecs(venue = VENUE): Promise<Record<string, InstrumentSpec>> {
    const r = await getJSON<{ markets: any[] }>(`${DESK}/markets`, { venue, limit: "400" });
    const out: Record<string, InstrumentSpec> = {};
    for (const m of r.markets || []) {
      out[m.symbol] = {
        symbol: m.symbol, kind: m.kind,
        tickSize: Number(m.tick_size), stepSize: Number(m.step_size),
        minSize: Number(m.min_size ?? 0), maxSize: Number(m.max_size ?? 0),
        minNotional: Number(m.min_notional), priceBandBps: Number(m.price_band_bps),
        maxLeverage: Number(m.max_leverage),
        imFraction: Number(m.initial_margin), mmFraction: Number(m.maintenance_margin),
        makerFeeBps: Number(m.maker_fee_bps ?? 0), takerFeeBps: Number(m.taker_fee_bps ?? 0),
        maxSlippageBps: numOrNull(m.max_slippage_bps),
        maxPositionSize: numOrNull(m.max_position_size),
        fundingPeriodHours: Number(m.funding_period_hours),
        supportedOrderTypes: m.supported_order_types ?? [],
      };
    }
    return out;
  },

  /** Persisted algo runs with TCA joined on, newest first. */
  async getRuns(limit = 20, market?: string): Promise<AlgoRun[]> {
    const p: Record<string, string> = { limit: String(limit) };
    if (market) p.market = market;
    const r = await getJSON<{ runs: any[] }>(`${DESK}/runs`, p);
    return (r.runs || []).map(toRun);
  },

  /** One run read back from the database: parent, slices, fills, TCA. */
  async getRun(parentId: string): Promise<RunDetail> {
    const r = await getJSON<any>(`${DESK}/runs/${encodeURIComponent(parentId)}`);
    return {
      parent: toRun(r.parent),
      children: (r.children || []).map((c: any) => ({
        sliceIndex: c.slice_index, scheduledMs: c.scheduled_ms,
        scheduledSize: Number(c.scheduled_size), weight: Number(c.weight),
        clientId: c.client_id ?? null, status: c.status,
        filledSize: Number(c.filled_size), avgPrice: numOrNull(c.avg_price),
      })),
      fills: (r.fills || []).map((f: any) => ({
        fillId: f.fill_id, sliceIndex: f.slice_index, tsMs: f.ts_ms,
        size: Number(f.size), price: Number(f.price), fee: Number(f.fee),
        liquidity: f.liquidity,
      })),
      tca: r.tca ?? null,
    };
  },

  /**
   * Pre-trade validation. **Nothing is transmitted to any venue.**
   *
   * The order is built, run through every rule the validator holds for that
   * venue, and discarded. `desk.rejections` carries the machine-readable codes;
   * `retail.message` is the one line an exchange app would show. The ticket
   * renders both, because the difference between them is the screen's point.
   */
  async validateOrder(o: {
    venue?: string; market: string; side: string; type: string; size: string;
    price?: string | null; triggerPrice?: string | null;
    maxSlippageBps?: string | null; leverage?: string | null;
    postOnly?: boolean; reduceOnly?: boolean;
  }): Promise<any> {
    return postJSON(`${DESK}/validate`, {
      venue: o.venue ?? "paper", market: o.market, side: o.side, type: o.type,
      size: o.size, price: o.price ?? null,
      trigger_price: o.triggerPrice ?? null,
      max_slippage_bps: o.maxSlippageBps ?? null,
      leverage: o.leverage ?? null,
      post_only: !!o.postOnly, reduce_only: !!o.reduceOnly,
    });
  },

  /**
   * Execute one parent order end to end and persist it.
   *
   * This is the only call on the desk that produces a fill. It runs against
   * the **local simulator** seeded with the live public book — the market
   * data, the oracle read and the block number are real, the counterparty is
   * not — and writes the parent, its slices, its fills and its TCA in one
   * transaction. That write is what `/runs` reads back, which is why a fill
   * shown in the blotter is a database row rather than a screen artefact.
   */
  async runLifecycle(o: {
    venue?: string; market: string; side: string; size: string;
    algo?: string; slices?: number;
  }): Promise<any> {
    return postJSON(`${DESK}/lifecycle`, {
      venue: o.venue ?? "paper", market: o.market, side: o.side, size: o.size,
      algo: o.algo ?? "TWAP", slices: o.slices ?? 6,
    });
  },

  /* ------------------------------------------------------ wallet sign-in */

  /**
   * Ask the server for a single-use nonce.
   *
   * It must come from the server. A nonce the browser invents proves freshness
   * to nobody — an attacker replaying a captured signature replays its nonce
   * along with it. Only the issuer can burn it after one use.
   */
  async authNonce(): Promise<{ nonce: string; expiresMs: number; statement: string }> {
    const r = await postJSON<any>(`${DESK}/auth/nonce`, {});
    return { nonce: r.nonce, expiresMs: r.expires_ms, statement: r.statement };
  },

  /**
   * Exchange a signed EIP-4361 message for a session.
   *
   * The session itself never reaches this code — it arrives as an httpOnly
   * `Set-Cookie` the browser stores and JavaScript cannot read. Only the CSRF
   * token comes back in the body, and that one is meant to be readable.
   */
  async authVerify(message: string, signature: string): Promise<{
    address: string; csrfToken: string; expiresMs: number;
  }> {
    const r = await postJSON<any>(`${DESK}/auth/verify`, { message, signature });
    setCsrfToken(r.csrf_token);
    return { address: r.address, csrfToken: r.csrf_token, expiresMs: r.expires_ms };
  },

  /** End the session server-side and drop the local CSRF token. */
  async authLogout(): Promise<void> {
    try { await postJSON(`${DESK}/auth/logout`, {}); } finally { setCsrfToken(null); }
  },

  /** Who the server thinks the caller is. Never 401s — absence is an answer. */
  async authSession(): Promise<{ authenticated: boolean; address: string | null; signatureBackend: string | null }> {
    const r = await getJSON<any>(`${DESK}/auth/session`);
    return { authenticated: !!r.authenticated, address: r.address ?? null,
             signatureBackend: r.signature_backend ?? null };
  },

  /** Native and ERC-20 balances for one address, at a named block. */
  async getWallet(address: string, chain = "sepolia"): Promise<any> {
    return getJSON(`${DESK}/wallet`, { address, chain });
  },

  /** Aggregate execution quality across stored runs. */
  async getAlgoStats(): Promise<any> {
    return getJSON(`${DESK}/stats`);
  },

  /** Desk capability + the mainnet guard. */
  async getHealth(): Promise<any> {
    return getJSON(`${DESK}/health`);
  },

  /* ------------------------------------------------- in-house engines */

  /** VaR/ES across every method the engine implements. */
  async getVaRSuite(): Promise<VaRSuite> {
    const s = await getJSON<any>(`${API}/portfolio/var-suite`);
    return {
      symbols: s.symbols || [], grossExposure: s.gross_exposure,
      confidence: s.confidence, nObservations: s.n_observations,
      alignment: s.alignment,
      methods: (s.methods || []).map((m: any) => ({
        method: m.method, varPct: m.var_pct, esPct: m.es_pct,
        varCurrency: m.var_currency, esCurrency: m.es_currency,
      })),
    };
  },

  /** Run the VaR engine over a caller-supplied return series. */
  async computeVaR(returns: number[], confidence = 0.99, methods?: string[]): Promise<any[]> {
    return postJSON(`${API}/risk/var`, { returns, confidence, methods });
  },

  /** Engine implementation report — which calls took the C++ path. */
  async getEngines(): Promise<any> {
    return getJSON(`${API}/bench/engines`);
  },

  /**
   * Perp board: mark, index, basis, funding, open interest, 24h volume.
   *
   * Defaults to mainnet because testnet is not a smaller version of this
   * market, it is a different one — BTC open interest there is ~$3.8m against
   * ~$2.8bn live, and funding annualises to nonsense off an empty book. The
   * network comes back on every row so the screen can say which it is.
   */
  async getPerps(limit = 10, network = "mainnet", markets?: string): Promise<PerpBoard> {
    const p: Record<string, string> = { limit: String(limit), network };
    // Naming specific markets turns a board fetch into a per-instrument stat
    // call — mark, index, basis, funding, OI and 24h volume for the one
    // contract the ticket is quoting, from the same upstream request.
    if (markets) p.markets = markets;
    const r = await getJSON<any>(`${DESK}/perps`, p);
    return {
      network: r.network, source: r.source,
      contractsTotal: r.contracts_total,
      aggregateOiUsd: Number(r.aggregate.open_interest_usd),
      aggregateVolUsd: Number(r.aggregate.day_notional_volume),
      rows: (r.rows || []).map((x: any) => ({
        market: x.market, coin: x.coin, network: x.network,
        mark: Number(x.mark), index: Number(x.index),
        basisBps: Number(x.basis_bps),
        fundingAprPct: x.funding_apr_pct === null ? null : Number(x.funding_apr_pct),
        openInterestUsd: Number(x.open_interest_usd),
        dayVolumeUsd: Number(x.day_notional_volume),
        change24hPct: x.change_24h_pct === null ? null : Number(x.change_24h_pct),
        maxLeverage: x.max_leverage,
      })),
    };
  },

  /** Crypto VaR/ES: public candles through the in-house engines. */
  async getCryptoRisk(market = "BTC-USD-PERP", confidence = 0.99,
                      notional = 1_000_000): Promise<CryptoRisk> {
    const r = await getJSON<any>(`${DESK}/risk`, {
      market, confidence: String(confidence), notional: String(notional),
    });
    return {
      market: r.market, historySymbol: r.history_symbol,
      historyNote: r.history_note, horizon: r.horizon,
      confidence: r.confidence, notional: r.notional,
      nReturns: r.series.n_returns, clean: r.series.clean,
      incompleteDropped: r.series.incomplete_dropped,
      sigmaPct: r.volatility.sigma_per_bar * 100,
      annualisedPct: r.volatility.annualised_pct,
      ewmaAnnualisedPct: r.volatility.ewma_annualised_pct,
      regime: r.volatility.regime,
      skew: r.moments.skew, excessKurtosis: r.moments.excess_kurtosis,
      fatTailed: r.moments.fat_tailed,
      methods: r.methods || [],
      backtests: r.backtests || [],
      cppAvailable: !!r.engine?.cpp_available,
    };
  },
};
