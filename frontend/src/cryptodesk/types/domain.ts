export type Status = "HEALTHY" | "NORMAL" | "WARNING" | "CRITICAL" | "OFFLINE";
export type OrderStatus = "NEW" | "ACCEPTED" | "WORKING" | "PARTIAL" | "FILLED" | "CANCELLED" | "REJECTED";
export type Side = "BUY" | "SELL";
export type AssetClass = "SPOT" | "PERPETUAL";
// STOP_MARKET / STOP_LIMIT are the engine's own names (`core.types.OrderType`),
// so a ticket can name an order type the validator will recognise. Plain
// "STOP" is retained for the Phase 1 fixtures, which predate the distinction.
export type OrderType = "MARKET" | "LIMIT" | "STOP" | "STOP_MARKET" | "STOP_LIMIT";
export type AlgoType = "DIRECT" | "TWAP" | "VWAP" | "POV" | "SOR";
export type MarginMode = "CROSS" | "ISOLATED";

/**
 * Instrument constraints as the venue publishes them, from `/markets`.
 *
 * The ticket prices margin, liquidation and fees off these rather than off
 * constants in the bundle: a browser quoting its own fee schedule is quoting
 * a venue that does not exist.
 */
export interface InstrumentSpec {
  symbol: string; kind: string;
  tickSize: number; stepSize: number; minSize: number; maxSize: number;
  minNotional: number; priceBandBps: number;
  maxLeverage: number; imFraction: number; mmFraction: number;
  makerFeeBps: number; takerFeeBps: number;
  maxSlippageBps: number | null; maxPositionSize: number | null;
  fundingPeriodHours: number;
  /** Order types the venue publishes. Empty means the venue did not say. */
  supportedOrderTypes: string[];
}

/** What the ticket emits on submit. */
export interface TicketOrder {
  symbol: string; side: Side; type: OrderType; algo: AlgoType;
  qty: number; limitPrice: number | null; triggerPrice: number | null;
  marginMode: MarginMode; leverage: number; reduceOnly: boolean; postOnly: boolean;
  takeProfit: number | null; stopLoss: number | null; maxSlippageBps: number;
  notional: number; marginRequired: number; liquidationPrice: number | null;
  slices: number; actor: string;
}

/* ----------------------------------------------------------- persisted runs */
// Mirrors the `algo_parents` / `algo_children` / `algo_fills` / `tca_results`
// tables. Money is TEXT in SQLite and arrives as strings; narrowing happens
// once, in liveProviders, for the same reason it does for books.

export interface RunFill {
  fillId: number; sliceIndex: number; tsMs: number;
  size: number; price: number; fee: number; liquidity: string;
}
export interface RunChild {
  sliceIndex: number; scheduledMs: number; scheduledSize: number; weight: number;
  clientId: string | null; status: string; filledSize: number; avgPrice: number | null;
}
export interface AlgoRun {
  parentId: string; createdMs: number; completedMs: number | null;
  venue: string; market: string; side: Side; algo: string;
  totalSize: number; filledSize: number; avgPrice: number | null;
  arrivalPrice: number; sliceCount: number; durationMs: number; status: string;
  fillCount: number;
  /** Address that signed in to place it. Null for runs predating sign-in. */
  actor: string | null;
  /** Where the fill happened. Null when it cannot be established. */
  network: "mainnet" | "testnet" | "simulated" | null;
  shortfallBps: number | null; vwapSlippageBps: number | null;
  feeBps: number | null; totalCostBps: number | null;
  participationPct: number | null; benchmarkPrints: number | null;
}
export interface RunDetail {
  parent: AlgoRun; children: RunChild[]; fills: RunFill[];
  tca: Record<string, unknown> | null;
}

/**
 * Client half of the entitlement model in TIER1_ARCHITECTURE §2.
 *
 * The API tier does not enforce this yet — §2 is design, not code — so the
 * token buys no access. What it does buy is an actor on every write, which is
 * the difference between a log and an audit trail, and it is why the actor is
 * carried on the ticket rather than assumed to be "the desk".
 */
export interface DeskSession {
  token: string; actor: string; expiresAt: number | null;
  /** Set when the session came from a wallet signature rather than a pasted token. */
  address?: string; chainId?: number; method?: "SIWE" | "TOKEN";
}

export interface Instrument {
  symbol: string; name: string; assetClass: AssetClass; price: number; change24h: number;
  bid: number; ask: number; volume24h: number; venue: string;
}
export interface BookLevel { price: number; size: number; total: number; side: "BID" | "ASK"; }
export interface Order {
  id: string; parentId?: string; time: string; symbol: string; side: Side; type: OrderType; algo: AlgoType;
  qty: number; filled: number; limitPrice?: number; avgPrice?: number; status: OrderStatus; venue: string;
  /** Who placed it. Absent on the Phase 1 fixtures, which predate the session. */
  actor?: string;
}
export interface Execution {
  id: string; orderId: string; time: string; symbol: string; side: Side; qty: number; price: number; fee: number; venue: string;
}
export interface Position {
  symbol: string; side: "LONG" | "SHORT"; qty: number; avgEntry: number; mark: number; unrealizedPnl: number;
  realizedPnl: number; notional: number; marginUsed: number;
}
export interface RiskMetric { label: string; value: string; limit?: string; utilization?: number; status: Status; }
export interface Vault { id: string; name: string; policy: string; value: number; available: number; reserved: number; status: Status; wallets: number; }
export interface Wallet { id: string; label: string; network: string; asset: string; address: string; balance: number; available: number; reserved: number; status: Status; }
export interface Transfer {
  id: string; time: string; type: "DEPOSIT" | "WITHDRAWAL" | "INTERNAL"; asset: string; amount: number;
  from: string; to: string; network: string; status: "PENDING" | "APPROVAL" | "CONFIRMED" | "FAILED"; txHash?: string;
}
export interface ChainTransaction {
  hash: string; time: string; network: string; type: string; asset: string; amount: number; confirmations: number; status: "VALIDATED" | "PENDING" | "FAILED";
}
export interface ReconBreak { id: string; source: string; target: string; field: string; expected: string; actual: string; severity: "LOW" | "MEDIUM" | "HIGH"; status: "OPEN" | "INVESTIGATING" | "RESOLVED"; }
export interface ServiceHealth { name: string; status: Status; latencyMs: number; throughput: string; lastHeartbeat: string; }
export interface AuditEvent { time: string; actor: string; action: string; entity: string; result: string; }
