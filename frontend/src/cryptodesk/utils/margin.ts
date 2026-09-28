import type { BookLevel, MarginMode, Side } from "../types/domain";

/**
 * Perpetual margin arithmetic for the order ticket.
 *
 * These functions deliberately mirror `cryptodesk.core.types.Position` and
 * `Instrument` rather than re-deriving the model in the browser. A ticket that
 * quotes a liquidation price the engine disagrees with is worse than one that
 * quotes none: the operator sizes the position against the screen, and the
 * venue liquidates against the engine.
 *
 * Everything here is `number`, not `Decimal`. That is acceptable *for
 * estimates shown to a human* — a liquidation price is read to four figures,
 * not settled on — and unacceptable for anything that becomes a ledger entry,
 * which is why the backend keeps Decimals and this file never sends one back.
 */

/**
 * Initial margin fraction actually in force.
 *
 * The venue's floor and the operator's leverage choice are both binding, and
 * the tighter one wins. Selecting 50x on a market whose published initial
 * requirement is 4% does not create a 50x position; it creates a rejected one.
 */
export function imFractionFor(leverage: number, venueIm: number): number {
  if (!(leverage > 0)) return 1;
  return Math.max(venueIm, 1 / leverage);
}

/** Margin posted to open `notional` at `leverage`. */
export function marginRequired(notional: number, leverage: number, venueIm: number): number {
  return notional * imFractionFor(leverage, venueIm);
}

/**
 * Mark price at which equity falls to the maintenance requirement.
 *
 * Solving `collateral + (P - entry) * size == |size| * P * mmf` for P:
 *
 *     P = (entry * size - collateral) / (size * (1 - sign(size) * mmf))
 *
 * `size` is signed — positive long, negative short — and that sign is what
 * makes one expression cover both directions. A long liquidates on the way
 * down and a short on the way up, and in both cases the maintenance
 * requirement moves *with* the mark. Modelling it as a fixed percentage below
 * entry is the common shortcut, and it understates risk on shorts, where the
 * requirement grows as the price rises against the position.
 *
 * `collateral` must be the collateral backing this position, not the free
 * remainder left after margin is posted: free collateral puts a long's
 * liquidation price above its own mark, which is the wrong side of the market.
 *
 * Returns null for a flat position, for the degenerate `mmf == 1` long, and
 * for a negative result — a position whose collateral exceeds its whole
 * notional cannot be liquidated by an adverse move at all.
 */
export function liquidationPrice(
  entry: number, signedSize: number, collateral: number, mmf: number,
): number | null {
  if (!signedSize || !Number.isFinite(entry) || !Number.isFinite(collateral)) return null;
  const sign = signedSize > 0 ? 1 : -1;
  const denominator = signedSize * (1 - sign * mmf);
  if (denominator === 0) return null;
  const price = (entry * signedSize - collateral) / denominator;
  return price > 0 ? price : null;
}

/**
 * Collateral backing one position under each margin mode.
 *
 * Isolated is exact: the posted margin is the whole of what can be lost.
 * Cross is an approximation and is labelled as one on the screen — one pool
 * backs every position, so a per-position liquidation price is only exact when
 * it is the sole position. With several open, the account margin ratio is the
 * authoritative health measure and this ignores the others' P&L.
 */
export function backingCollateral(
  mode: MarginMode, margin: number, accountCollateral: number,
): number {
  return mode === "ISOLATED" ? margin : Math.max(margin, accountCollateral);
}

/* ------------------------------------------------------------- book walk */

export interface Sweep {
  /** Size-weighted price actually paid across the consumed levels. */
  avgPrice: number | null;
  /** How much the visible book could fill. */
  filled: number;
  /** Adverse move vs mid, in bps. Positive means the sweep paid up. */
  slippageBps: number | null;
  /** True when the book ran out before the order was filled. */
  exhausted: boolean;
  /** Levels consumed — the depth the order actually reaches into. */
  levelsUsed: number;
}

/**
 * Walk the visible book for a marketable order.
 *
 * This is the only honest source for a slippage estimate. A percentage of the
 * spread is not slippage; it is the first level's contribution to it, and on a
 * thin book the difference between the two is the entire trade. When the book
 * cannot fill the order, `exhausted` is set and the average price describes
 * only the part that filled — a ticket that reports a tidy average for size
 * the market does not have is the more dangerous failure.
 */
export function sweepBook(levels: BookLevel[], side: Side, qty: number, mid: number | null): Sweep {
  const empty: Sweep = { avgPrice: null, filled: 0, slippageBps: null, exhausted: true, levelsUsed: 0 };
  if (!(qty > 0) || !levels.length) return empty;

  // A buy lifts asks from the lowest up; a sell hits bids from the highest
  // down. The incoming array is ordered for *display* (asks descending so the
  // touch sits against the mid), so it is re-sorted rather than trusted.
  const book = side === "BUY"
    ? levels.filter(l => l.side === "ASK").sort((a, b) => a.price - b.price)
    : levels.filter(l => l.side === "BID").sort((a, b) => b.price - a.price);
  if (!book.length) return empty;

  let remaining = qty, cost = 0, used = 0;
  for (const level of book) {
    if (remaining <= 0) break;
    const take = Math.min(remaining, level.size);
    cost += take * level.price;
    remaining -= take;
    used += 1;
  }
  const filled = qty - remaining;
  if (filled <= 0) return empty;

  const avgPrice = cost / filled;
  const slippageBps = mid && mid > 0
    ? (side === "BUY" ? (avgPrice - mid) / mid : (mid - avgPrice) / mid) * 10000
    : null;
  return { avgPrice, filled, slippageBps, exhausted: remaining > 1e-12, levelsUsed: used };
}

/* -------------------------------------------------------------- TP / SL */

export interface Bracket {
  takeProfitPnl: number | null;
  stopLossPnl: number | null;
  /** Reward-to-risk. Null unless both legs are set and the stop loses money. */
  rr: number | null;
  /** Legs placed on the wrong side of entry for the direction traded. */
  inverted: string[];
}

/**
 * P&L at each bracket leg, and whether either is on the wrong side of entry.
 *
 * The inversion check earns its place: a take-profit *below* entry on a long
 * is not an aggressive target, it is a stop wearing the wrong label, and it
 * closes the position at a loss the moment it is reached.
 */
export function bracket(
  side: Side, entry: number, qty: number, tp: number | null, sl: number | null,
): Bracket {
  const dir = side === "BUY" ? 1 : -1;
  const pnl = (exit: number) => (exit - entry) * qty * dir;
  const inverted: string[] = [];
  if (tp !== null && dir * (tp - entry) <= 0) inverted.push("take-profit is on the losing side of entry");
  if (sl !== null && dir * (sl - entry) >= 0) inverted.push("stop-loss is on the winning side of entry");

  const takeProfitPnl = tp === null ? null : pnl(tp);
  const stopLossPnl = sl === null ? null : pnl(sl);
  const rr = takeProfitPnl !== null && stopLossPnl !== null && stopLossPnl < 0
    ? Math.abs(takeProfitPnl / stopLossPnl) : null;
  return { takeProfitPnl, stopLossPnl, rr, inverted };
}

/* ------------------------------------------------------------- rounding */

/** Snap a price to the tick grid. Nearest — the ticket shows it before sending. */
export function roundTick(price: number, tick: number): number {
  if (!(tick > 0)) return price;
  return Math.round(price / tick) * tick;
}

/** Snap size down to the step grid. Never up: that submits more risk than asked. */
export function roundStep(size: number, step: number): number {
  if (!(step > 0)) return size;
  return Math.floor(size / step + 1e-9) * step;
}

/**
 * Decimal places implied by a tick or step size.
 *
 * Read off the exponential form rather than counting characters in
 * `String(increment)`, which reports 0.000001 as "1e-6" and would show a
 * six-decimal step to no decimals at all.
 */
export function precisionOf(increment: number): number {
  if (!(increment > 0)) return 2;
  const [mantissa, exponent] = increment.toExponential().split("e");
  const dot = mantissa.indexOf(".");
  const fractionDigits = dot >= 0 ? mantissa.length - dot - 1 : 0;
  return Math.max(0, Math.min(10, fractionDigits - Number(exponent)));
}
