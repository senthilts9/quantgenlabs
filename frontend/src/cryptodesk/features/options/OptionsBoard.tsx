import { useState } from "react";
import { Panel, Metric, StatusPill, Tabs, Empty } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { dex } from "../../services/dexProviders";
import type { OptionLeg } from "../../services/dexProviders";
import { money, num, px } from "../../utils/format";

const BASES = ["BTC", "ETH", "SOL", "HYPE"];

/** IV arrives as a rate (0.4053), and a vol surface is read in percent. */
const iv = (x: number | null) => x === null ? "—" : `${(x * 100).toFixed(1)}%`;

/**
 * Greeks span many orders of magnitude, so they take two formatters.
 *
 * Delta, vega and theta sit in human ranges on a live chain — 0.004 to 0.995,
 * 0.5 to 16, -17 to -294 — and read best as plain decimals.
 */
const greek = (x: number | null) => {
  if (x === null) return "—";
  if (x === 0) return "0";
  return Math.abs(x) >= 1 ? x.toFixed(2) : x.toFixed(4);
};

/**
 * Gamma, vanna and volga do not: one BTC chain runs 3.7e-06 to 2.5e-04 across
 * its strikes. Switching notation partway down that column — `0.0003` at the
 * money against `3.7e-6` in the wings — makes the two unreadable against each
 * other, and the shape of the gamma profile *is* the information. Everything
 * below a hundredth goes exponential so one column keeps one notation.
 */
const smallGreek = (x: number | null) => {
  if (x === null) return "—";
  if (x === 0) return "0";
  return Math.abs(x) < 0.01 ? x.toExponential(1) : x.toFixed(4);
};

const expiryLabel = (ms: number) =>
  new Date(ms).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "2-digit" });

/** A leg with no bid and no open interest is listed, not traded. */
const isQuoted = (l: OptionLeg | null) =>
  !!l && (l.bid !== null || (l.openInterest ?? 0) > 0);

/**
 * Listed option chain with the venue's own greeks.
 *
 * **The greeks are the venue's, not ours.** These are the numbers the venue
 * margins positions against, so a locally recomputed delta that disagreed
 * would not be a correction — it would be a second opinion wearing the
 * venue's label. The desk's own pricing engines live under PRICING, where
 * they are clearly the desk's.
 *
 * Calls and puts are paired on one row because the comparison across a strike
 * is the point: call IV against put IV is where skew lives, and call mark
 * against put mark is where put-call parity breaks show up. Two tables sorted
 * by strike make the reader do that join by eye.
 */
export function OptionsBoard() {
  const [base, setBase] = useState("BTC");
  const [expiryMs, setExpiryMs] = useState<number | undefined>(undefined);
  const [showSecondOrder, setShowSecondOrder] = useState(false);

  // Changing the underlying must clear the expiry: a BTC expiry epoch is
  // usually not a listed ETH expiry, so carrying it over 404s the chain.
  const pickBase = (b: string) => { setBase(b); setExpiryMs(undefined); };

  const expiries = useAsync(() => dex.getExpiries(base), [base], 120000);
  const chain = useAsync(() => dex.getChain(base, expiryMs, 120),
                         [base, expiryMs], 15000);

  const rows = chain.data?.chain ?? [];
  const spot = chain.data?.underlying ?? null;
  const atm = chain.data?.atmStrike ?? null;

  // ATM implied vol, taken from whichever leg the venue is actually marking.
  const atmRow = rows.find(r => r.strike === atm);
  const atmIv = atmRow?.call?.markIv ?? atmRow?.put?.markIv ?? null;

  const quoted = rows.filter(r => isQuoted(r.call) || isQuoted(r.put)).length;
  const chainOi = rows.reduce(
    (a, r) => a + (r.call?.openInterest ?? 0) + (r.put?.openInterest ?? 0), 0);

  // Net chain vega: what the listed surface is worth per vol point. Signed
  // per leg would net to nothing meaningful without a position, so this is
  // the gross listed exposure — a size-of-surface read, not a risk number.
  const chainVega = rows.reduce(
    (a, r) => a + Math.abs(r.call?.vega ?? 0) + Math.abs(r.put?.vega ?? 0), 0);

  return <div className="page">
    <div className="hero-row">
      <div><h1>Options Board</h1>
        <p>Listed option chain with venue greeks — DEX venue · mainnet</p></div>
      <StatusPill status={chain.error ? "FEED ERROR"
        : chain.data ? (chain.data.staleS > 0 ? `STALE ${chain.data.staleS.toFixed(0)}s` : "LIVE · MAINNET")
        : "CONNECTING"} />
    </div>

    <Panel title="Underlying">
      <Tabs items={BASES} active={base} onChange={pickBase} />
    </Panel>

    {expiries.data && expiries.data.length > 0 && <Panel
      title="Expiry"
      right={<span className="muted">{expiries.data.length} listed</span>}>
      <div className="chip-row">
        {expiries.data.map(e => {
          const active = chain.data?.expiryMs === e.expiryMs;
          return <button
            key={e.expiryMs}
            className={`chip ${active ? "active" : ""}`}
            onClick={() => setExpiryMs(e.expiryMs)}
            title={`${e.contracts} contracts · ${e.strikeCount} strikes · ${e.calls}C / ${e.puts}P`}>
            {expiryLabel(e.expiryMs)}
            <small>{e.daysToExpiry < 1
              ? `${Math.max(0, e.daysToExpiry * 24).toFixed(0)}h`
              : `${e.daysToExpiry.toFixed(0)}d`}</small>
          </button>;
        })}
      </div>
    </Panel>}

    {chain.error && <Panel><Empty text={`Chain unavailable — ${chain.error}`} /></Panel>}
    {!chain.data && chain.loading && <Panel><Empty text="Loading chain…" /></Panel>}

    {chain.data && <>
      <div className="metric-grid four">
        <Metric label={`${base} Underlying`} value={spot === null ? "—" : money(spot, 2)} />
        <Metric label="ATM Implied Vol" value={iv(atmIv)}
          sub={atm === null ? undefined : `K = ${num(atm, 0)}`} />
        <Metric label="Days to Expiry" value={chain.data.daysToExpiry.toFixed(1)}
          sub={expiryLabel(chain.data.expiryMs)} />
        <Metric label="Strikes Quoted" value={`${quoted} / ${chain.data.strikeCount}`}
          tone={quoted === 0 ? "warning" : ""}
          sub={`${num(chainOi, 1)} contracts OI`} />
      </div>

      <Panel
        title={`${base} chain — ${expiryLabel(chain.data.expiryMs)}`}
        right={<button className="mini" onClick={() => setShowSecondOrder(v => !v)}>
          {showSecondOrder ? "First-order greeks" : "Vanna / volga"}
        </button>}>
                  <table className="chain">
            <thead>
              <tr>
                <th colSpan={showSecondOrder ? 5 : 6} className="side-call">CALLS</th>
                <th className="strike-col">STRIKE</th>
                <th colSpan={showSecondOrder ? 5 : 6} className="side-put">PUTS</th>
              </tr>
              <tr>
                {showSecondOrder
                  ? <><th>MARK</th><th>IV</th><th>VANNA</th><th>VOLGA</th><th>OI</th></>
                  : <><th>MARK</th><th>IV</th><th>DELTA</th><th>GAMMA</th><th>VEGA</th><th>THETA</th></>}
                <th className="strike-col">K</th>
                {showSecondOrder
                  ? <><th>OI</th><th>VOLGA</th><th>VANNA</th><th>IV</th><th>MARK</th></>
                  : <><th>THETA</th><th>VEGA</th><th>GAMMA</th><th>DELTA</th><th>IV</th><th>MARK</th></>}
              </tr>
            </thead>
            <tbody>{rows.map(r => {
              const atRow = atm !== null && r.strike === atm;
              // In-the-money shading follows the option, not the row: a call
              // is ITM below spot and a put is ITM above it.
              const callItm = spot !== null && r.strike < spot;
              const c = r.call, p = r.put;
              return <tr key={r.strike} className={atRow ? "atm" : ""}>
                {showSecondOrder ? <>
                  <td className={`mono ${callItm ? "itm" : ""}`}>{c?.mark === undefined || c?.mark === null ? "—" : px(c.mark)}</td>
                  <td className={callItm ? "itm" : ""}>{iv(c?.markIv ?? null)}</td>
                  <td className="mono">{smallGreek(c?.vanna ?? null)}</td>
                  <td className="mono">{smallGreek(c?.volga ?? null)}</td>
                  <td>{c?.openInterest ? num(c.openInterest, 1) : "—"}</td>
                </> : <>
                  <td className={`mono ${callItm ? "itm" : ""}`}>{c?.mark === undefined || c?.mark === null ? "—" : px(c.mark)}</td>
                  <td className={callItm ? "itm" : ""}>{iv(c?.markIv ?? null)}</td>
                  <td className="mono">{greek(c?.delta ?? null)}</td>
                  <td className="mono">{smallGreek(c?.gamma ?? null)}</td>
                  <td className="mono">{greek(c?.vega ?? null)}</td>
                  <td className="mono">{greek(c?.theta ?? null)}</td>
                </>}

                <td className="strike-col strong">{num(r.strike, 0)}</td>

                {showSecondOrder ? <>
                  <td>{p?.openInterest ? num(p.openInterest, 1) : "—"}</td>
                  <td className="mono">{smallGreek(p?.volga ?? null)}</td>
                  <td className="mono">{smallGreek(p?.vanna ?? null)}</td>
                  <td className={!callItm ? "itm" : ""}>{iv(p?.markIv ?? null)}</td>
                  <td className={`mono ${!callItm ? "itm" : ""}`}>{p?.mark === undefined || p?.mark === null ? "—" : px(p.mark)}</td>
                </> : <>
                  <td className="mono">{greek(p?.theta ?? null)}</td>
                  <td className="mono">{greek(p?.vega ?? null)}</td>
                  <td className="mono">{smallGreek(p?.gamma ?? null)}</td>
                  <td className="mono">{greek(p?.delta ?? null)}</td>
                  <td className={!callItm ? "itm" : ""}>{iv(p?.markIv ?? null)}</td>
                  <td className={`mono ${!callItm ? "itm" : ""}`}>{p?.mark === undefined || p?.mark === null ? "—" : px(p.mark)}</td>
                </>}
              </tr>;
            })}</tbody>
          </table>
      </Panel>

      <div className="grid two">
        <Panel title="Implied vol by strike — the skew">
          <div className="bars">{rows.filter(r => r.call?.markIv || r.put?.markIv).slice(0, 12).map(r => {
            const v = r.call?.markIv ?? r.put?.markIv ?? 0;
            const all = rows.map(x => x.call?.markIv ?? x.put?.markIv ?? 0);
            const hi = Math.max(...all, 0.0001);
            return <div key={r.strike}>
              <span>{num(r.strike, 0)}</span>
              <div className="bar"><i style={{ width: `${((v / hi) * 100).toFixed(1)}%` }} /></div>
              <b>{iv(v)}</b>
            </div>;
          })}</div>
        </Panel>
        <Panel title="Surface size">
          <div className="cost-grid">
            <div><span>Listed vega</span><b>{num(chainVega, 0)}</b></div>
            <div><span>Chain OI</span><b>{num(chainOi, 1)}</b></div>
            <div><span>Strikes</span><b>{chain.data.strikeCount}</b></div>
            <div><span>Quoted</span>
              <b className={quoted === 0 ? "negative" : "positive"}>{quoted}</b></div>
          </div>
        </Panel>
      </div>

      <p className="disclaimer">
        Greeks and implied vols are <strong>the venue's own</strong>, read from
        the venue's public market summary — these are the numbers it margins
        positions against, which is why they are not recomputed here. Vega is
        quoted per vol point and theta per day. Most listed strikes have never
        traded: a blank mark is a contract nobody is quoting, and “strikes
        quoted” counts those with a live bid or open interest.
      </p>
    </>}
  </div>;
}
