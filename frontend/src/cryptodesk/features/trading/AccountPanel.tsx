import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { liveProviders } from "../../services/liveProviders";
import { money, num } from "../../utils/format";

/**
 * Account health and contract terms, below the ticket.
 *
 * Margin ratio and account leverage are the two numbers that decide whether
 * the *next* order is survivable, which is why a venue keeps them beside the
 * ticket rather than on a portfolio page. Both are computed here from
 * collateral and the open position rather than fetched, because this host has
 * no custody service to fetch them from — and that is stated rather than
 * implied by a confident-looking figure.
 *
 * Contract terms come from `/markets` and `/funding`: the funding period is
 * shown next to the rate because the same per-period rate means eight times
 * the annual cost when it settles hourly rather than eight-hourly.
 */
export function AccountPanel({ symbol, collateral, positionSize, markPrice, entryPrice, marginUsed }: {
  symbol: string; collateral: number; positionSize: number;
  markPrice: number | null; entryPrice: number | null; marginUsed: number;
}) {
  const [tab, setTab] = useState<"ACCOUNT" | "CONTRACT">("ACCOUNT");
  const specs = useAsync(() => liveProviders.getSpecs(), []);
  const funding = useAsync(() => liveProviders.getFunding(symbol), [symbol], 60000);
  const spec = specs.data?.[symbol];
  const f = funding.data?.find(x => x.venue === "hyperliquid") ?? funding.data?.[0] ?? null;

  const notional = markPrice !== null ? Math.abs(positionSize) * markPrice : 0;
  const unrealised = markPrice !== null && entryPrice !== null
    ? (markPrice - entryPrice) * positionSize : 0;
  const equity = collateral + unrealised;
  // Maintenance requirement over equity. Null rather than 0 when flat: a flat
  // account has no margin ratio, and 0.0% reads as "maximally safe" when it
  // actually means "not applicable".
  const marginRatio = notional > 0 && equity > 0 && spec
    ? (notional * spec.mmFraction) / equity * 100 : null;
  const accountLeverage = equity > 0 && notional > 0 ? notional / equity : null;

  return <div className="acct-panel">
    <div className="ord-tabs sub">
      {(["ACCOUNT", "CONTRACT"] as const).map(t =>
        <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
          {t === "ACCOUNT" ? "Account" : "Contract"}</button>)}
    </div>

    {tab === "ACCOUNT" ? <>
      <Row label="Trading Equity" value={money(equity)} />
      <Row label="Unrealized P&L" value={positionSize === 0 ? "—" : money(unrealised)}
        tone={positionSize === 0 ? "" : unrealised >= 0 ? "positive" : "negative"} />
      <Row label="Margin Used" value={money(marginUsed)} />
      <Row label="Cross Margin Ratio"
        value={marginRatio === null ? "—" : `${num(marginRatio, 2)}%`}
        tone={marginRatio !== null && marginRatio > 60 ? "negative" : ""} />
      <Row label="Cross Account Leverage"
        value={accountLeverage === null ? "—" : `${num(accountLeverage, 2)}x`} />
      <div className="acct-note">
        Collateral is an operator input on this host — there is no custody
        service behind it. Position and P&L derive from persisted runs.
      </div>
    </> : <>
      <Row label="Funding Period" value={f ? `${f.periodHours} Hours` : spec ? `${spec.fundingPeriodHours} Hours` : "—"} />
      <Row label="Funding Rate" value={f ? `${f.ratePct.toFixed(4)}%` : "—"}
        tone={f ? (f.ratePct >= 0 ? "positive" : "negative") : ""} />
      <Row label="Funding APR" value={f ? `${num(f.aprPct, 2)}%` : "—"} />
      <Row label="Direction" value={f ? f.direction : "—"} />
      <Row label="Max Leverage" value={spec ? `${num(spec.maxLeverage, 0)}x` : "—"} />
      <Row label="Initial Margin" value={spec ? `${num(spec.imFraction * 100, 2)}%` : "—"} />
      <Row label="Maintenance Margin" value={spec ? `${num(spec.mmFraction * 100, 2)}%` : "—"} />
      <Row label="Tick / Step" value={spec ? `${spec.tickSize} / ${spec.stepSize}` : "—"} />
      <Row label="Maker / Taker" value={spec ? `${num(spec.makerFeeBps, 1)} / ${num(spec.takerFeeBps, 1)} bps` : "—"} />
    </>}
  </div>;
}

function Row({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return <div className="t-row"><span>{label}</span><b className={tone}>{value}</b></div>;
}
