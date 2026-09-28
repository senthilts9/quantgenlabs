/**
 * DEX board providers — the venue's public read surface.
 *
 * Separate from `liveProviders` for the same reason the backend router is
 * separate from `cryptodesk.py`: those calls are venue-agnostic and take a
 * `?venue=` parameter, while everything here exists because *this venue*
 * publishes it. A listed option surface with greeks, strategy vaults and an
 * insurance fund have no cross-venue signature to share.
 *
 * **Every call is unauthenticated and read-only.** The console holds no
 * account on that venue, so its private surface — balances, positions,
 * fills, referral tiers — is not proxied and cannot be reached from here.
 *
 * **Money arrives as numbers already.** The backend has done the narrowing:
 * it parses the venue's strings, converts `""` to `null` rather than to 0, and
 * marks open interest to USD so a board can sort BTC against HYPE. The
 * `null`s are load-bearing and stay `null` — an option nobody is bidding for
 * has no bid, and rendering that as 0.00 claims a bid exists at zero.
 */

const P = "/api/cryptodesk/dex";

async function getJSON<T>(url: string, params: Record<string, string> = {}): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`${url}${qs ? `?${qs}` : ""}`, { credentials: "same-origin" });
  if (!res.ok) {
    // Surface the server's own message: "every listed BTC expiry has passed"
    // tells an operator what happened, "Request failed" does not.
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); detail = b.detail || b.error || detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

/* ---------------------------------------------------------------- markets */

export interface DexMarket {
  symbol: string; kind: string; base: string; quote: string; settlement: string;
  mark: number | null; underlying: number | null; last: number | null;
  bid: number | null; ask: number | null;
  bidSize: number | null; askSize: number | null; spreadBps: number | null;
  fundingRate: number | null; fundingPeriodHours: number | null;
  change24hPct: number | null; volume24h: number | null; totalVolume: number | null;
  openInterest: number | null; openInterestUsd: number | null;
  tickSize: number | null; stepSize: number | null; minNotional: number | null;
  positionLimit: number | null; marketKind: string | null;
  expiryMs: number | null; tags: string[];
}

export interface DexBoard {
  kind: string; listed: Record<string, number>; matched: number; staleS: number;
  aggregateOiUsd: number; aggregateVolume24h: number; rows: DexMarket[];
}

function toMarket(r: any): DexMarket {
  return {
    symbol: r.symbol, kind: r.kind, base: r.base, quote: r.quote,
    settlement: r.settlement, mark: r.mark, underlying: r.underlying,
    last: r.last, bid: r.bid, ask: r.ask,
    bidSize: r.bid_size, askSize: r.ask_size, spreadBps: r.spread_bps,
    fundingRate: r.funding_rate, fundingPeriodHours: r.funding_period_hours,
    change24hPct: r.change_24h_pct, volume24h: r.volume_24h,
    totalVolume: r.total_volume, openInterest: r.open_interest,
    openInterestUsd: r.open_interest_usd, tickSize: r.tick_size,
    stepSize: r.step_size, minNotional: r.min_notional,
    positionLimit: r.position_limit, marketKind: r.market_kind,
    expiryMs: r.expiry_ms, tags: r.tags || [],
  };
}

/* ---------------------------------------------------------------- options */

export interface OptionLeg {
  symbol: string; base: string; optionType: string;
  strike: number | null; expiryMs: number;
  mark: number | null; markIv: number | null; underlying: number | null;
  bid: number | null; ask: number | null;
  bidSize: number | null; askSize: number | null; last: number | null;
  volume24h: number | null; openInterest: number | null;
  delta: number | null; gamma: number | null; vega: number | null;
  theta: number | null; rho: number | null;
  vanna: number | null; volga: number | null;
}

export interface ChainRow { strike: number; call: OptionLeg | null; put: OptionLeg | null; }

export interface OptionChain {
  base: string; expiryMs: number; daysToExpiry: number;
  underlying: number | null; atmStrike: number | null;
  strikeCount: number; staleS: number; chain: ChainRow[];
}

export interface OptionExpiry {
  expiryMs: number; daysToExpiry: number; contracts: number;
  calls: number; puts: number; openInterest: number; volume24h: number;
  strikeCount: number; strikeLow: number | null; strikeHigh: number | null;
}

function toLeg(r: any): OptionLeg | null {
  if (!r) return null;
  return {
    symbol: r.symbol, base: r.base, optionType: r.option_type,
    strike: r.strike, expiryMs: r.expiry_ms, mark: r.mark, markIv: r.mark_iv,
    underlying: r.underlying, bid: r.bid, ask: r.ask,
    bidSize: r.bid_size, askSize: r.ask_size, last: r.last,
    volume24h: r.volume_24h, openInterest: r.open_interest,
    delta: r.delta, gamma: r.gamma, vega: r.vega, theta: r.theta,
    rho: r.rho, vanna: r.vanna, volga: r.volga,
  };
}

/* ----------------------------------------------------------------- vaults */

export interface DexVault {
  address: string; name: string | null; kind: string | null;
  description: string | null; strategy: string | null; curator: string | null;
  owner: string | null; status: string | null;
  profitSharePct: number | null; lockupDays: number | null; createdMs: number | null;
  tvl: number | null; netDeposits: number | null; ownerEquity: number | null;
  vtokenPrice: number | null; vtokenSupply: number | null;
  roiTotalPct: number | null; roi24hPct: number | null;
  pnlTotal: number | null; pnl24h: number | null;
  maxDrawdownPct: number | null; volume24h: number | null;
}

/* ---------------------------------------------------------------- funding */

export interface FundingPoint {
  market: string; createdMs: number; fundingRate: number | null;
  fundingPremium: number | null; fundingIndex: number | null;
}

export interface FundingHistory {
  market: string; periodHours: number | null; observations: number;
  meanRate: number | null; meanAprPct: number | null;
  maxRate: number | null; minRate: number | null;
  positiveSharePct: number | null; points: FundingPoint[];
}

/* ------------------------------------------------------------------ trades */

export interface TapePrint {
  id: string; market: string; side: string;
  size: number | null; price: number | null; createdMs: number; tradeType: string | null;
}

export interface Tape {
  market: string; count: number; windowNotional: number;
  buySharePct: number | null; rows: TapePrint[];
}

export const dex = {
  /** Reachability plus cache age — the screen's "is this feed alive" answer. */
  async getHealth(): Promise<any> { return getJSON(`${P}/health`); },

  /**
   * The tradable universe for one asset class.
   *
   * Defaults to perps: 63 perps against 2,980 listed options, nearly all of
   * which have never traded. Opening on everything would bury the markets
   * anyone is actually quoting under a screen of empty option rows.
   */
  async getMarkets(kind = "PERP", limit = 60, base?: string): Promise<DexBoard> {
    const p: Record<string, string> = { kind, limit: String(limit) };
    if (base) p.base = base;
    const r = await getJSON<any>(`${P}/markets`, p);
    return {
      kind: r.kind, listed: r.listed || {}, matched: r.matched,
      staleS: r.stale_s ?? 0,
      aggregateOiUsd: r.aggregate?.open_interest_usd ?? 0,
      aggregateVolume24h: r.aggregate?.volume_24h ?? 0,
      rows: (r.rows || []).map(toMarket),
    };
  },

  /** Listed expiries for one underlying, nearest first. */
  async getExpiries(base = "BTC"): Promise<OptionExpiry[]> {
    const r = await getJSON<any>(`${P}/options/expiries`, { base });
    return (r.expiries || []).map((e: any) => ({
      expiryMs: e.expiry_ms, daysToExpiry: e.days_to_expiry,
      contracts: e.contracts, calls: e.calls, puts: e.puts,
      openInterest: e.open_interest, volume24h: e.volume_24h,
      strikeCount: e.strike_count, strikeLow: e.strike_low, strikeHigh: e.strike_high,
    }));
  },

  /**
   * One expiry's chain, calls and puts paired by strike.
   *
   * Omitting `expiryMs` lets the server pick — it defaults to the nearest
   * expiry with more than a day left rather than the nearest outright,
   * because a chain in its final hours has collapsed to delta 1 and delta 0
   * with vega at ~1e-26. That is a settlement exercise, not a vol surface.
   */
  async getChain(base = "BTC", expiryMs?: number, limit = 80): Promise<OptionChain> {
    const p: Record<string, string> = { base, limit: String(limit) };
    if (expiryMs) p.expiry_ms = String(expiryMs);
    const r = await getJSON<any>(`${P}/options/chain`, p);
    return {
      base: r.base, expiryMs: r.expiry_ms, daysToExpiry: r.days_to_expiry,
      underlying: r.underlying, atmStrike: r.atm_strike,
      strikeCount: r.strike_count, staleS: r.stale_s ?? 0,
      chain: (r.chain || []).map((c: any) => ({
        strike: c.strike, call: toLeg(c.call), put: toLeg(c.put),
      })),
    };
  },

  /** Strategy vaults, definitions joined to performance, by TVL. */
  async getVaults(limit = 25, sort = "tvl"): Promise<{
    total: number; funded: number; aggregateTvl: number; staleS: number; rows: DexVault[];
  }> {
    const r = await getJSON<any>(`${P}/vaults`, { limit: String(limit), sort });
    return {
      total: r.total, funded: r.funded, aggregateTvl: r.aggregate_tvl,
      staleS: r.stale_s ?? 0,
      rows: (r.rows || []).map((v: any) => ({
        address: v.address, name: v.name, kind: v.kind,
        description: v.description, strategy: v.strategy, curator: v.curator,
        owner: v.owner, status: v.status,
        profitSharePct: v.profit_share_pct, lockupDays: v.lockup_days,
        createdMs: v.created_ms, tvl: v.tvl, netDeposits: v.net_deposits,
        ownerEquity: v.owner_equity, vtokenPrice: v.vtoken_price,
        vtokenSupply: v.vtoken_supply, roiTotalPct: v.roi_total_pct,
        roi24hPct: v.roi_24h_pct, pnlTotal: v.pnl_total, pnl24h: v.pnl_24h,
        maxDrawdownPct: v.max_drawdown_pct, volume24h: v.volume_24h,
      })),
    };
  },

  /** Realised funding history for one perp, newest first. */
  async getFunding(market = "BTC-USD-PERP", limit = 60): Promise<FundingHistory> {
    const r = await getJSON<any>(`${P}/funding`, { market, limit: String(limit) });
    return {
      market: r.market, periodHours: r.period_hours, observations: r.observations,
      meanRate: r.mean_rate, meanAprPct: r.mean_apr_pct,
      maxRate: r.max_rate, minRate: r.min_rate,
      positiveSharePct: r.positive_share_pct,
      points: (r.points || []).map((p: any) => ({
        market: p.market, createdMs: p.created_ms, fundingRate: p.funding_rate,
        fundingPremium: p.funding_premium, fundingIndex: p.funding_index,
      })),
    };
  },

  /** The public tape for one market — prints, not quotes. */
  async getTrades(market = "BTC-USD-PERP", limit = 50): Promise<Tape> {
    const r = await getJSON<any>(`${P}/trades`, { market, limit: String(limit) });
    return {
      market: r.market, count: r.count, windowNotional: r.window_notional,
      buySharePct: r.buy_share_pct,
      rows: (r.rows || []).map((t: any) => ({
        id: t.id, market: t.market, side: t.side, size: t.size,
        price: t.price, createdMs: t.created_ms, tradeType: t.trade_type,
      })),
    };
  },

  /** The venue's insurance fund — the backstop behind every open position. */
  async getInsurance(): Promise<{
    account: string; accountValue: number | null; settlementAsset: string;
    updatedMs: number; staleS: number;
  }> {
    const r = await getJSON<any>(`${P}/insurance`);
    return {
      account: r.account, accountValue: r.account_value,
      settlementAsset: r.settlement_asset, updatedMs: r.updated_ms,
      staleS: r.stale_s ?? 0,
    };
  },
};
