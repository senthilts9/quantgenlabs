import { useState } from "react";
import { Empty, StatusPill } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { liveProviders } from "../../services/liveProviders";
import { ago, bps, clock, money, num, px } from "../../utils/format";

/**
 * The strip under the chart: positions, risk, orders, schedules, history.
 *
 * Every tab reads a real endpoint. The one worth calling out is **Risk**,
 * which runs the in-house VaR/ES engines (`/api/cryptodesk/risk` → `core.risk`
 * and `core.crypto.risk`, numpy with an optional compiled C++ path) over
 * public candles for the contract on screen. It reports which estimator
 * produced each number and whether the compiled path was available, because
 * "VaR 2.3%" without the method behind it is not a risk number, it is a
 * decoration — historical, Gaussian and EVT disagree most exactly when the
 * tail matters.
 *
 * The backtest column is the load-bearing one: each day's forecast is fitted
 * only to data strictly before that day, and the Kupiec test says whether the
 * exception count is consistent with the confidence level claimed. An
 * in-sample backtest is the commonest way to publish a VaR model that looks
 * calibrated and is not.
 */

const TABS = ["Positions", "Risk", "Open Orders", "TWAP", "Trade History", "Order History"] as const;
type Tab = typeof TABS[number];

export function DeskTabs({ symbol, refreshKey, positionSize, entryPrice, markPrice, collateral }: {
  symbol: string; refreshKey: number;
  positionSize: number; entryPrice: number | null; markPrice: number | null; collateral: number;
}) {
  const [tab, setTab] = useState<Tab>("Positions");

  const runs = useAsync(() => liveProviders.getRuns(100, symbol), [symbol, refreshKey], 15000);
  const rows = runs.data ?? [];

  // Risk is expensive (candles + several estimators + an out-of-sample
  // backtest) and cached server-side for 5 minutes; fetch it only when looked at.
  const risk = useAsync(
    async () => (tab === "Risk" ? liveProviders.getCryptoRisk(symbol, 0.99, Math.max(10000, Math.abs(positionSize) * (markPrice ?? 0) || 1_000_000)) : null),
    [tab, symbol]);

  // Fills for the newest run, which is what "Trade History" means here.
  const newest = rows[0] ?? null;
  const detail = useAsync(
    async () => (newest && (tab === "Trade History" || tab === "TWAP") ? liveProviders.getRun(newest.parentId) : null),
    [newest?.parentId, tab]);

  const unrealised = markPrice !== null && entryPrice !== null
    ? (markPrice - entryPrice) * positionSize : 0;

  return <div className="desk-tabs">
    <div className="ord-tabs strip">
      {TABS.map(t => <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
        {t}{t === "Open Orders" && rows.some(r => r.status === "WORKING") ? ` ${rows.filter(r => r.status === "WORKING").length}` : ""}</button>)}
      <span className="spacer" />
      <StatusPill status={runs.error ? "STORE UNAVAILABLE" : `${rows.length} RUNS`} />
    </div>

    <div className="tab-body">
      {tab === "Positions" && (positionSize === 0
        ? <Empty text="No open position. Positions are derived from persisted runs, not held in the browser." />
        : <table className="dense"><thead><tr>
            <th>MARKET</th><th>SIDE</th><th>SIZE</th><th>AVG ENTRY</th><th>MARK</th>
            <th>NOTIONAL</th><th>UNREALIZED</th><th>MARGIN</th>
          </tr></thead><tbody><tr>
            <td className="strong">{symbol}</td>
            <td className={positionSize > 0 ? "positive" : "negative"}>{positionSize > 0 ? "LONG" : "SHORT"}</td>
            <td>{num(Math.abs(positionSize), 6)}</td>
            <td>{entryPrice === null ? "—" : px(entryPrice)}</td>
            <td>{markPrice === null ? "—" : px(markPrice)}</td>
            <td>{markPrice === null ? "—" : money(Math.abs(positionSize) * markPrice)}</td>
            <td className={unrealised >= 0 ? "positive" : "negative"}>{money(unrealised)}</td>
            <td>{money(collateral)}</td>
          </tr></tbody></table>)}

      {tab === "Risk" && <>
        {risk.error && <div className="notice reject">{risk.error}</div>}
        {!risk.data && risk.loading && <Empty text="Running VaR/ES engines…" />}
        {risk.data && <>
          <div className="risk-head">
            <span>σ/bar <b>{num(risk.data.sigmaPct, 3)}%</b></span>
            <span>annualised <b>{num(risk.data.annualisedPct, 1)}%</b></span>
            <span>EWMA <b>{num(risk.data.ewmaAnnualisedPct, 1)}%</b></span>
            <span>regime <b>{risk.data.regime}</b></span>
            <span>excess kurtosis <b className={risk.data.fatTailed ? "warning" : ""}>
              {num(risk.data.excessKurtosis, 2)}{risk.data.fatTailed ? " · fat-tailed" : ""}</b></span>
            <span>n <b>{risk.data.nReturns}</b></span>
            <span>engine <b>{risk.data.cppAvailable ? "C++ available" : "numpy"}</b></span>
          </div>
          {risk.data.historyNote && <div className="notice warn">{risk.data.historyNote}</div>}
          <div className="risk-split">
            <table className="dense"><thead><tr>
              <th>METHOD</th><th>FAMILY</th><th>VAR %</th><th>ES %</th><th>VAR $</th><th>ES $</th>
            </tr></thead><tbody>
              {risk.data.methods.map(m => <tr key={m.method}>
                <td className="strong">{m.method}</td><td>{m.family}</td>
                <td>{num(m.var_pct, 3)}</td><td>{num(m.es_pct, 3)}</td>
                <td>{money(m.var_currency, 0)}</td><td>{money(m.es_currency, 0)}</td>
              </tr>)}
            </tbody></table>
            <table className="dense"><thead><tr>
              <th>BACKTEST</th><th>OBS</th><th>EXC</th><th>EXPECTED</th><th>KUPIEC p</th><th>BASEL</th><th>VERDICT</th>
            </tr></thead><tbody>
              {risk.data.backtests.map(b => <tr key={b.method}>
                <td className="strong">{b.method}</td><td>{b.observations}</td>
                <td>{b.exceptions}</td><td>{num(b.expected, 1)}</td>
                <td>{num(b.kupiec_p, 4)}</td>
                <td className={b.basel_zone === "GREEN" ? "positive" : b.basel_zone === "RED" ? "negative" : "warning"}>
                  {b.basel_zone}</td>
                <td>{b.coverage_verdict}</td>
              </tr>)}
            </tbody></table>
          </div>
          <div className="acct-note">
            Out-of-sample: each forecast is fitted only to data strictly before
            the day it predicts. Confidence {num(risk.data.confidence * 100, 0)}%,
            horizon {risk.data.horizon}, notional {money(risk.data.notional, 0)}.
          </div>
        </>}
      </>}

      {tab === "Open Orders" && (() => {
        const working = rows.filter(r => r.status !== "FILLED" && r.status !== "CANCELLED");
        return working.length === 0
          ? <Empty text="No working orders. Runs execute to completion against the local simulator." />
          : <RunTable rows={working} />;
      })()}

      {tab === "TWAP" && (() => {
        const sched = rows.filter(r => r.algo !== "DIRECT" && r.sliceCount > 1);
        if (!sched.length) return <Empty text="No scheduled parents. Submit with TWAP or VWAP execution from the Pro tab." />;
        return <>
          <RunTable rows={sched} />
          {detail.data && detail.data.children.length > 1 && <table className="dense"><thead><tr>
            <th>SLICE</th><th>SCHEDULED</th><th>CLIENT ID</th><th>TARGET</th><th>WEIGHT</th><th>FILLED</th><th>AVG PX</th><th>STATUS</th>
          </tr></thead><tbody>
            {detail.data.children.map(c => <tr key={c.sliceIndex}>
              <td>{c.sliceIndex}</td><td>{clock(c.scheduledMs)}</td>
              <td className="mono">{c.clientId ?? "—"}</td>
              <td>{num(c.scheduledSize, 6)}</td><td>{num(c.weight * 100, 2)}%</td>
              <td>{num(c.filledSize, 6)}</td>
              <td>{c.avgPrice === null ? "—" : px(c.avgPrice)}</td>
              <td><StatusPill status={c.status} /></td>
            </tr>)}
          </tbody></table>}
        </>;
      })()}

      {tab === "Trade History" && (!detail.data?.fills.length
        ? <Empty text={detail.loading ? "Loading fills…" : "No fills yet."} />
        : <table className="dense"><thead><tr>
            <th>TIME</th><th>FILL</th><th>SLICE</th><th>SIDE</th><th>QTY</th><th>PRICE</th><th>NOTIONAL</th><th>FEE</th><th>LIQUIDITY</th>
          </tr></thead><tbody>
            {detail.data.fills.map(f => <tr key={f.fillId}>
              <td>{clock(f.tsMs)}</td><td className="link mono">{f.fillId}</td>
              <td>{f.sliceIndex}</td>
              <td className={detail.data!.parent.side === "BUY" ? "positive" : "negative"}>
                {detail.data!.parent.side}</td>
              <td>{num(f.size, 6)}</td><td>{px(f.price)}</td>
              <td>{money(f.size * f.price)}</td><td>{money(f.fee)}</td><td>{f.liquidity}</td>
            </tr>)}
          </tbody></table>)}

      {tab === "Order History" && (rows.length === 0
        ? <Empty text={runs.loading ? "Loading runs…" : "No persisted runs for this market."} />
        : <RunTable rows={rows} />)}
    </div>
  </div>;
}

function RunTable({ rows }: { rows: any[] }) {
  return <table className="dense"><thead><tr>
    <th>TIME</th><th>PARENT ID</th><th>NETWORK</th><th>VENUE</th><th>ACTOR</th><th>SIDE</th><th>ALGO</th><th>SIZE</th>
    <th>FILLED</th><th>AVG PX</th><th>ARRIVAL</th><th>SHORTFALL</th><th>FILLS</th><th>STATUS</th>
  </tr></thead><tbody>
    {rows.map(r => <tr key={r.parentId}>
      <td title={clock(r.createdMs)}>{ago(r.createdMs)}</td>
      <td className="link mono">{r.parentId}</td>
      {/* The most consequential column on the screen: real money and a
          rehearsal are otherwise both just a row with a price. */}
      <td><span className={`net ${r.network ?? "unknown"}`}>
        {r.network ?? "unknown"}</span></td>
      <td className={r.venue === "paper" ? "" : "strong"}>{r.venue}</td>
      <td className="mono" title={r.actor ?? undefined}>
        {r.actor ? `${r.actor.slice(0, 6)}…${r.actor.slice(-4)}`
          : <span className="muted">unattributed</span>}</td>
      <td className={r.side === "BUY" ? "positive" : "negative"}>{r.side}</td>
      <td>{r.algo}</td><td>{num(r.totalSize, 6)}</td><td>{num(r.filledSize, 6)}</td>
      <td>{r.avgPrice === null ? "—" : px(r.avgPrice)}</td>
      <td>{px(r.arrivalPrice)}</td>
      <td className={r.shortfallBps === null ? "" : r.shortfallBps <= 0 ? "positive" : "negative"}>
        {bps(r.shortfallBps)}</td>
      <td>{r.fillCount}</td><td><StatusPill status={r.status} /></td>
    </tr>)}
  </tbody></table>;
}
