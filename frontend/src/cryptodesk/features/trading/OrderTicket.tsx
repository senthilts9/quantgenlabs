import { useMemo, useState } from "react";
import type { LiveBook } from "../../services/liveProviders";
import type {
  AlgoType, DeskSession, InstrumentSpec, MarginMode, OrderType, Side, TicketOrder,
} from "../../types/domain";
import { money, num, px } from "../../utils/format";
import {
  backingCollateral, bracket, imFractionFor, liquidationPrice, marginRequired,
  precisionOf, roundStep, roundTick, sweepBook,
} from "../../utils/margin";

/**
 * Perpetuals order ticket, laid out as a venue ticket is laid out.
 *
 * The ordering is not cosmetic. Mode pills (leverage, margin, position mode)
 * sit at the top because they change the meaning of everything below them;
 * the read-outs sit *under* the confirm button because they are consequences
 * of the ticket, not inputs to it. An operator reads down: what am I trading,
 * how much, and what does it cost me if I am wrong.
 *
 * Every figure is derived from venue metadata (`/markets`) and the live book.
 * The one number the browser owns is account collateral, because this host has
 * no custody — it comes from Deposit and is marked as an operator input.
 */

const SIZE_STEPS = [0, 25, 50, 75, 100];

/** Engine order-type names, so the validator recognises what it is sent. */
const TAKES_LIQUIDITY = new Set<OrderType>(["MARKET", "STOP_MARKET"]);
const NEEDS_LIMIT = new Set<OrderType>(["LIMIT", "STOP_LIMIT"]);
const NEEDS_TRIGGER = new Set<OrderType>(["STOP_MARKET", "STOP_LIMIT"]);

export interface TicketReport {
  ok: boolean; message: string;
  rejections?: { code?: string; stage?: string; message?: string }[];
  warnings?: string[]; repairs?: string[]; parentId?: string | null;
}

export interface TicketProps {
  symbol: string;
  book: LiveBook | null;
  spec: InstrumentSpec | undefined;
  specError: string | null;
  collateral: number;
  positionSize: number;
  session: DeskSession | null;
  venue: string;
  submitting: boolean;
  report: TicketReport | null;
  onDeposit: () => void;
  onConnect: () => void;
  onSubmit: (order: TicketOrder) => void;
}

export function OrderTicket(p: TicketProps) {
  const [side, setSide] = useState<Side>("BUY");
  const [tab, setTab] = useState<"MARKET" | "LIMIT" | "PRO">("MARKET");
  const [proType, setProType] = useState<OrderType>("STOP_MARKET");
  const [algo, setAlgo] = useState<AlgoType>("DIRECT");
  const [marginMode, setMarginMode] = useState<MarginMode>("CROSS");
  const [positionMode, setPositionMode] = useState<"ONE-WAY" | "HEDGE">("ONE-WAY");
  const [leverage, setLeverage] = useState(10);
  const [levOpen, setLevOpen] = useState(false);
  const [unit, setUnit] = useState<"BASE" | "USD">("BASE");
  const [sizeRaw, setSizeRaw] = useState("");
  const [priceRaw, setPriceRaw] = useState<string | null>(null);
  const [triggerRaw, setTriggerRaw] = useState("");
  const [reduceOnly, setReduceOnly] = useState(false);
  const [postOnly, setPostOnly] = useState(false);
  const [tpsl, setTpsl] = useState(false);
  const [tpRaw, setTpRaw] = useState("");
  const [slRaw, setSlRaw] = useState("");
  const [slipPct, setSlipPct] = useState("0.10");
  const [slipEdit, setSlipEdit] = useState(false);
  const [slices, setSlices] = useState(6);

  const spec = p.spec;
  const mid = p.book?.mid ?? null;
  const priced = mid !== null;
  const maxLeverage = Math.max(1, Math.floor(spec?.maxLeverage ?? 20));
  const lev = Math.min(leverage, maxLeverage);
  const [base] = p.symbol.split("-");
  const sizeDp = precisionOf(spec?.stepSize ?? 0.001);

  const type: OrderType = tab === "PRO" ? proType : tab;
  const marketable = TAKES_LIQUIDITY.has(type);
  const needsLimit = NEEDS_LIMIT.has(type);
  const needsTrigger = NEEDS_TRIGGER.has(type);

  const supportsType = (t: OrderType) =>
    !spec?.supportedOrderTypes?.length || spec.supportedOrderTypes.includes(t);

  const limitPrice = priceRaw === null
    ? (mid !== null ? roundTick(mid, spec?.tickSize ?? 0.01) : 0)
    : Number(priceRaw) || 0;
  const trigger = triggerRaw.trim() === "" ? null : Number(triggerRaw);
  const takeProfit = tpsl && tpRaw.trim() !== "" ? Number(tpRaw) : null;
  const stopLoss = tpsl && slRaw.trim() !== "" ? Number(slRaw) : null;
  // The venue quotes a slippage *cap* as a percentage; the engine takes bps.
  const maxSlippageBps = (Number(slipPct) || 0) * 100;

  /* ------------------------------------------------------------ pricing */

  // Size is entered in base units or quote; the rest of the ticket only ever
  // sees base, so the conversion happens once, here.
  const refPrice = needsLimit ? limitPrice : needsTrigger ? (trigger ?? mid ?? 0) : (mid ?? 0);
  const typedSize = Number(sizeRaw) || 0;
  const qty = unit === "BASE" ? typedSize : (refPrice > 0 ? typedSize / refPrice : 0);

  const sweep = useMemo(
    () => (p.book && marketable ? sweepBook(p.book.levels, side, qty, mid) : null),
    [p.book, marketable, side, qty, mid]);

  // A market order enters at what the book gives, not the mid; a stop enters
  // around its trigger, not around today's mark.
  const entry = needsLimit ? limitPrice
    : needsTrigger ? (trigger ?? mid ?? 0)
    : (sweep?.avgPrice ?? mid ?? 0);
  const notional = qty * entry;

  const venueIm = spec?.imFraction ?? 1 / maxLeverage;
  const mmf = spec?.mmFraction ?? venueIm / 2;
  const margin = marginRequired(notional, lev, venueIm);
  const backing = backingCollateral(marginMode, margin, p.collateral);
  const liq = liquidationPrice(entry, side === "BUY" ? qty : -qty, backing, mmf);

  const feeBps = postOnly && !marketable ? (spec?.makerFeeBps ?? 0) : (spec?.takerFeeBps ?? 0);
  const fee = notional * (feeBps / 10000);
  const availableAfter = p.collateral - margin - Math.max(0, fee);

  // Worst price the order may fill at once the slippage cap is applied. This
  // is the number that answers "what am I actually agreeing to" on a market
  // order, which the mid does not.
  const maxFillPrice = entry > 0
    ? entry * (side === "BUY" ? 1 + maxSlippageBps / 10000 : 1 - maxSlippageBps / 10000)
    : null;

  const brk = useMemo(
    () => bracket(side, entry, qty, takeProfit, stopLoss),
    [side, entry, qty, takeProfit, stopLoss]);

  const maxQty = useMemo(() => {
    if (!(refPrice > 0) || !(p.collateral > 0)) return 0;
    return roundStep((p.collateral * lev) / refPrice, spec?.stepSize ?? 0.001);
  }, [refPrice, p.collateral, lev, spec]);

  const sizePct = maxQty > 0 ? Math.min(100, (qty / maxQty) * 100) : 0;
  const setSizePct = (pct: number) => {
    const q = roundStep(maxQty * (pct / 100), spec?.stepSize ?? 0.001);
    setSizeRaw(unit === "BASE" ? String(q) : String(Math.round(q * refPrice * 100) / 100));
  };

  /* --------------------------------------------------------- pre-flight */
  // Courtesy checks, not controls: /validate on the API tier is the authority
  // and runs on submit regardless. These exist so problems surface while typing.

  const blocks: string[] = [];
  const warns: string[] = [];
  if (!priced) blocks.push("no live book for this market");
  if (!(qty > 0)) blocks.push("size must be positive");
  if (!supportsType(type))
    blocks.push(`${p.venue} publishes only ${spec!.supportedOrderTypes.join(", ")} on this market`);
  if (spec && qty > 0 && qty < spec.minSize)
    blocks.push(`below venue minimum size of ${num(spec.minSize, sizeDp)} ${base}`);
  if (spec && spec.maxSize > 0 && qty > spec.maxSize)
    blocks.push(`above venue maximum order size of ${num(spec.maxSize, sizeDp)} ${base}`);
  if (spec && notional > 0 && notional < spec.minNotional)
    blocks.push(`notional below venue minimum of ${money(spec.minNotional)}`);
  if (needsLimit && spec && limitPrice > 0 && mid) {
    const bandBps = Math.abs((limitPrice - mid) / mid) * 10000;
    if (bandBps > spec.priceBandBps)
      blocks.push(`limit is ${num(bandBps, 0)} bps from mark, outside the ±${num(spec.priceBandBps, 0)} bps band`);
  }
  if (needsTrigger && trigger === null) blocks.push(`${type} requires a trigger price`);
  if (needsTrigger && trigger !== null && mid !== null &&
      (side === "BUY" ? trigger <= mid : trigger >= mid))
    warns.push("trigger is already through the mark and will fire immediately");
  if (sweep?.exhausted && qty > 0)
    warns.push(`order exceeds the ${sweep.levelsUsed} visible levels; slippage shown is a lower bound`);
  if (sweep?.slippageBps != null && maxSlippageBps > 0 && sweep.slippageBps > maxSlippageBps)
    blocks.push(`estimated slippage ${num(sweep.slippageBps / 100, 3)}% exceeds the ${slipPct}% cap`);
  if (margin > p.collateral && !reduceOnly)
    blocks.push(`margin ${money(margin)} exceeds available ${money(p.collateral)}`);
  brk.inverted.forEach(x => blocks.push(x));
  if (reduceOnly && p.positionSize === 0)
    warns.push("reduce-only with no open position — nothing to reduce");
  if (marginMode === "CROSS")
    warns.push("cross margin: liquidation assumes this is the only open position");
  if (!p.session) warns.push("not signed in — this order is recorded against actor 'anonymous'");

  const canSend = blocks.length === 0 && !p.submitting;

  const submit = () => {
    if (!canSend) return;
    p.onSubmit({
      symbol: p.symbol, side, type, algo,
      qty: roundStep(qty, spec?.stepSize ?? 0.001),
      limitPrice: needsLimit ? limitPrice : null,
      triggerPrice: trigger,
      marginMode, leverage: lev, reduceOnly, postOnly,
      takeProfit, stopLoss, maxSlippageBps,
      notional, marginRequired: margin, liquidationPrice: liq,
      slices: algo === "DIRECT" ? 1 : slices,
      actor: p.session?.actor ?? "anonymous",
    });
  };

  /* -------------------------------------------------------------- render */

  return <div className="ticket-panel">
    {/* mode pills — these change what everything below means, so they lead */}
    <div className="mode-row">
      <button className={`pill ${levOpen ? "open" : ""}`} onClick={() => setLevOpen(v => !v)}>{lev}x</button>
      <button className="pill" onClick={() => setMarginMode(m => m === "CROSS" ? "ISOLATED" : "CROSS")}>
        {marginMode === "CROSS" ? "Cross" : "Isolated"}</button>
      <button className="pill" onClick={() => setPositionMode(m => m === "ONE-WAY" ? "HEDGE" : "ONE-WAY")}>
        {positionMode === "ONE-WAY" ? "One-Way" : "Hedge"}</button>
    </div>

    {levOpen && <div className="lev-pop">
      <div className="lev-head"><span>Leverage</span><b>{lev}x</b></div>
      <input type="range" min={1} max={maxLeverage} step={1} value={lev}
        onChange={e => setLeverage(+e.target.value)} />
      <div className="lev-scale"><span>1x</span><span>{maxLeverage}x max</span></div>
      <div className="lev-note">
        Initial margin {num(imFractionFor(lev, venueIm) * 100, 2)}% ·
        maintenance {num(mmf * 100, 2)}% — both published by {p.venue}.
      </div>
    </div>}

    <div className="ord-tabs">
      {(["MARKET", "LIMIT", "PRO"] as const).map(t =>
        <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
          {t === "PRO" ? "Pro" : t === "MARKET" ? "Market" : "Limit"}</button>)}
    </div>

    {tab === "PRO" && <div className="pro-row">
      <label>Type
        <select value={proType} onChange={e => setProType(e.target.value as OrderType)}>
          {(["STOP_MARKET", "STOP_LIMIT"] as OrderType[]).map(t =>
            <option key={t} value={t}>{t}{supportsType(t) ? "" : " (not on venue)"}</option>)}
        </select></label>
      <label>Execution
        <select value={algo} onChange={e => setAlgo(e.target.value as AlgoType)}>
          <option>DIRECT</option><option>TWAP</option><option>VWAP</option>
        </select></label>
      {algo !== "DIRECT" && <label>Slices
        <input type="number" min={2} max={24} value={slices}
          onChange={e => setSlices(Math.max(2, Math.min(24, +e.target.value)))} /></label>}
    </div>}

    <div className="bs-row">
      <button className={`bs buy ${side === "BUY" ? "active" : ""}`} onClick={() => setSide("BUY")}>Buy</button>
      <button className={`bs sell ${side === "SELL" ? "active" : ""}`} onClick={() => setSide("SELL")}>Sell</button>
    </div>

    <Row label="Current Position" value={`${num(p.positionSize, sizeDp)} ${base}`} />
    <div className="t-row">
      <span>Available to trade</span>
      {/* Doubles as the way to fund it. An operator who finds the balance too
          low is already looking at this row. */}
      <b>{money(p.collateral)}
        <button className="mini inline" onClick={p.onDeposit}>Deposit</button></b>
    </div>

    <div className="size-field">
      <label>Size</label>
      <input type="number" min={0} value={sizeRaw} placeholder="0"
        step={unit === "BASE" ? (spec?.stepSize ?? 0.001) : 1}
        onChange={e => setSizeRaw(e.target.value)} />
      <div className="unit-toggle">
        {(["BASE", "USD"] as const).map(u =>
          <button key={u} className={unit === u ? "active" : ""} onClick={() => setUnit(u)}>
            {u === "BASE" ? base : "USD"}</button>)}
      </div>
    </div>

    <input className="size-slider" type="range" min={0} max={100} step={1}
      value={Math.round(sizePct)} disabled={!(maxQty > 0)}
      onChange={e => setSizePct(+e.target.value)} />
    <div className="slider-scale">
      {SIZE_STEPS.map(s => <button key={s} disabled={!(maxQty > 0)}
        onClick={() => setSizePct(s)}>{s}%</button>)}
    </div>

    {needsLimit && <div className="size-field">
      <label>Limit Price</label>
      <input type="number" value={priceRaw ?? (limitPrice || "")} step={spec?.tickSize ?? 0.01}
        onChange={e => setPriceRaw(e.target.value)} />
    </div>}

    {needsTrigger && <div className="size-field">
      <label>Trigger Price</label>
      <input type="number" value={triggerRaw} step={spec?.tickSize ?? 0.01}
        placeholder={mid ? px(mid) : ""} onChange={e => setTriggerRaw(e.target.value)} />
    </div>}

    <label className="chk"><input type="checkbox" checked={reduceOnly}
      onChange={e => setReduceOnly(e.target.checked)} /> Reduce Only</label>
    {needsLimit && <label className="chk"><input type="checkbox" checked={postOnly}
      onChange={e => setPostOnly(e.target.checked)} /> Post Only</label>}
    <label className="chk"><input type="checkbox" checked={tpsl}
      onChange={e => setTpsl(e.target.checked)} /> TP/SL</label>

    {tpsl && <div className="tpsl">
      <div className="tpsl-row">
        <input type="number" placeholder="Take Profit" value={tpRaw}
          step={spec?.tickSize ?? 0.01} onChange={e => setTpRaw(e.target.value)} />
        <span className={brk.takeProfitPnl === null ? "muted" : brk.takeProfitPnl >= 0 ? "positive" : "negative"}>
          {brk.takeProfitPnl === null ? "—" : money(brk.takeProfitPnl)}</span>
      </div>
      <div className="tpsl-row">
        <input type="number" placeholder="Stop Loss" value={slRaw}
          step={spec?.tickSize ?? 0.01} onChange={e => setSlRaw(e.target.value)} />
        <span className={brk.stopLossPnl === null ? "muted" : brk.stopLossPnl >= 0 ? "positive" : "negative"}>
          {brk.stopLossPnl === null ? "—" : money(brk.stopLossPnl)}</span>
      </div>
      {brk.rr !== null && <div className="rr">Reward : Risk <b>{num(brk.rr, 2)} : 1</b></div>}
    </div>}

    {p.session
      ? <button className={`confirm ${side.toLowerCase()}`} disabled={!canSend} onClick={submit}>
          {p.submitting ? "Validating & executing…"
            : !priced ? "Awaiting market data"
            // An empty ticket is not a rejected one. "Blocked — see below"
            // on a freshly opened form reads as a refusal the user has to
            // diagnose, when the only thing missing is the field they have
            // not filled in yet — so name that instead.
            : !(qty > 0) ? "Enter a size to trade"
            : blocks.length ? "Blocked — see below"
            : `Confirm ${side === "BUY" ? "Buy" : "Sell"}`}
        </button>
      : <button className="confirm connect" onClick={p.onConnect}>Connect Wallet</button>}

    {/* consequences of the ticket, under the button that commits to them */}
    <div className="readout">
      <Read label="Liquidation Price" value={liq === null ? "—" : `$${px(liq)}`} tone={liq === null ? "" : "warning"} />
      <Read label="Order Value (M)" value={notional > 0 ? money(notional) : "—"} />
      <Read label="Margin Req." value={margin > 0 ? money(margin) : "—"} />
      <Read label="Available After"
        value={notional > 0 ? money(availableAfter) : money(p.collateral)}
        tone={availableAfter < 0 ? "negative" : ""} />
      <div className="read">
        <span>Max Slippage</span>
        {slipEdit
          ? <input className="slip" autoFocus type="number" step="0.01" min="0" value={slipPct}
              onChange={e => setSlipPct(e.target.value)} onBlur={() => setSlipEdit(false)}
              onKeyDown={e => { if (e.key === "Enter") setSlipEdit(false); }} />
          : <b onClick={() => setSlipEdit(true)} className="editable">{slipPct}% ✎</b>}
      </div>
      <div className="read">
        <span>Fees ({postOnly && !marketable ? "maker" : "taker"})</span>
        <b>{notional > 0 ? `${money(fee)} · ${num(feeBps, 1)} bps` : `${num(feeBps, 1)} bps`}</b>
      </div>
      <Read label={`Max ${side === "BUY" ? "Buy" : "Sell"} Price`}
        value={maxFillPrice === null ? "—" : `$${px(maxFillPrice)}`} />
      {marketable && <Read label="Est. Slippage"
        value={sweep?.slippageBps == null ? "—" : `${num(sweep.slippageBps / 100, 3)}%`}
        tone={sweep?.slippageBps != null && sweep.slippageBps > maxSlippageBps ? "negative" : ""} />}
    </div>

    {p.specError && <div className="notice warn">
      Instrument specs unavailable — {p.specError}. Leverage cap, tick and fees
      fall back to defaults and may not match the venue.
    </div>}

    {blocks.length > 0 && <ul className="notice reject">
      {blocks.map((b, i) => <li key={i}>{b}</li>)}</ul>}
    {warns.length > 0 && <ul className="notice warn">
      {warns.map((w, i) => <li key={i}>{w}</li>)}</ul>}

    {p.report && <div className={`notice ${p.report.ok ? "ok" : "reject"}`}>
      <b>{p.report.message}</b>
      {p.report.parentId && <div className="mono small">run {p.report.parentId}</div>}
      {p.report.rejections?.map((r, i) => <div key={i}>{r.code ? `${r.code} · ` : ""}{r.message}</div>)}
      {p.report.repairs?.map((r, i) => <div key={`f${i}`}>repaired: {r}</div>)}
      {p.report.warnings?.map((w, i) => <div key={`w${i}`}>{w}</div>)}
    </div>}

    <small className="disclaimer">
      Live venue book and the venue's own instrument constraints. On confirm the
      order is validated by the API tier, then executed against the <b>local
      simulator</b> and persisted — the fill in the blotter is a database row.
      Nothing reaches an exchange: this host holds no signing material and
      exposes no submission endpoint.
    </small>
  </div>;
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="t-row"><span>{label}</span><b>{value}</b></div>;
}
function Read({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return <div className="read"><span>{label}</span><b className={tone}>{value}</b></div>;
}
