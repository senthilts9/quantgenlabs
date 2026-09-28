import { useState } from "react";
import { Panel, Metric, StatusPill, Tabs, Empty } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { dex } from "../../services/dexProviders";
import { money, num, px, clock } from "../../utils/format";

const MARKETS = ["BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP", "HYPE-USD-PERP"];

/**
 * Funding history and the public tape for one perp.
 *
 * These belong on one screen because they answer the two halves of the same
 * question. Funding says what it costs to hold the position; the tape says
 * whether you could get into it at all. A carry that looks attractive in a
 * market printing $9k an hour is not a trade — it is a screenshot.
 */
export function FundingTape() {
  const [market, setMarket] = useState(MARKETS[0]);
  const funding = useAsync(() => dex.getFunding(market, 60), [market], 60000);
  const tape = useAsync(() => dex.getTrades(market, 40), [market], 10000);
  const insurance = useAsync(() => dex.getInsurance(), [], 120000);

  const points = funding.data?.points ?? [];
  const prints = tape.data?.rows ?? [];

  // Scale the sparkline to the window's own extremes rather than to zero:
  // funding rates live in the 1e-5 range, so a zero-based axis renders every
  // period as the same flat line against the top of the panel.
  const rates = points.map(p => p.fundingRate ?? 0);
  const hi = rates.length ? Math.max(...rates) : 0;
  const lo = rates.length ? Math.min(...rates) : 0;
  const span = hi - lo || 1;

  const periodsPerYear = funding.data?.periodHours
    ? (365 * 24) / funding.data.periodHours : null;
  const latest = points[0]?.fundingRate ?? null;
  const latestApr = latest !== null && periodsPerYear
    ? latest * periodsPerYear * 100 : null;

  return <div className="page">
    <div className="hero-row">
      <div><h1>Funding &amp; Tape</h1>
        <p>Realised funding history and public prints — DEX venue · mainnet</p></div>
      <StatusPill status={funding.error ? "FEED ERROR"
        : funding.data ? "LIVE · MAINNET" : "CONNECTING"} />
    </div>

    <Panel title="Contract">
      <Tabs items={MARKETS} active={market} onChange={setMarket} />
    </Panel>

    {funding.error && <Panel><Empty text={`Funding unavailable — ${funding.error}`} /></Panel>}

    {funding.data && <>
      <div className="metric-grid four">
        <Metric label="Latest Funding APR"
          value={latestApr === null ? "—" : `${latestApr >= 0 ? "+" : ""}${latestApr.toFixed(2)}%`}
          tone={(latestApr ?? 0) >= 0 ? "positive" : "negative"}
          sub={`per ${funding.data.periodHours ?? 8}h period`} />
        <Metric label="Mean APR — window"
          value={funding.data.meanAprPct === null ? "—"
            : `${funding.data.meanAprPct >= 0 ? "+" : ""}${funding.data.meanAprPct.toFixed(2)}%`}
          sub={`${funding.data.observations} periods`} />
        <Metric label="Periods Long Paid"
          value={funding.data.positiveSharePct === null ? "—"
            : `${funding.data.positiveSharePct.toFixed(0)}%`}
          sub="longs paying shorts" />
        <Metric label="Insurance Fund"
          value={insurance.data?.accountValue == null ? "—"
            : money(insurance.data.accountValue, 0)}
          sub={insurance.data?.settlementAsset ?? "venue backstop"} />
      </div>

      <Panel title={`Funding — last ${points.length} periods`}
        right={<span className="muted">
          range {(lo * 100).toFixed(4)}% … {(hi * 100).toFixed(4)}% per period</span>}>
        {points.length === 0 ? <Empty text="No funding history returned." /> : <>
          <div className="spark">
            {/* Oldest on the left: a series read left-to-right should run
                forward in time, and the API returns newest-first. */}
            {[...points].reverse().map(p => {
              const r = p.fundingRate ?? 0;
              const h = ((r - lo) / span) * 100;
              return <i key={p.createdMs}
                className={r >= 0 ? "pos" : "neg"}
                style={{ height: `${Math.max(2, h).toFixed(1)}%` }}
                title={`${new Date(p.createdMs).toLocaleString("en-GB", { hour12: false })} · ${(r * 100).toFixed(5)}%`} />;
            })}
          </div>
          <div className="cost-grid">
            <div><span>Max period</span><b className="negative">{(hi * 100).toFixed(5)}%</b></div>
            <div><span>Min period</span><b className="positive">{(lo * 100).toFixed(5)}%</b></div>
            <div><span>Mean period</span><b>{funding.data.meanRate === null ? "—"
              : `${(funding.data.meanRate * 100).toFixed(5)}%`}</b></div>
            <div><span>Cost /$10k/day</span><b>{funding.data.meanAprPct === null ? "—"
              : `$${((funding.data.meanAprPct / 365) * 100).toFixed(2)}`}</b></div>
          </div>
        </>}
      </Panel>

      <Panel title={`Tape — ${market}`}
        right={<span className="muted">
          {tape.data ? `${tape.data.count} prints · ${money(tape.data.windowNotional, 0)} · ${tape.data.buySharePct?.toFixed(0) ?? "—"}% buy` : "…"}</span>}>
        {tape.error ? <Empty text={`Tape unavailable — ${tape.error}`} />
          : prints.length === 0 ? <Empty text="No prints in the window." />
          :             <table>
              <thead><tr><th>TIME</th><th>SIDE</th><th>PRICE</th><th>SIZE</th><th>NOTIONAL</th><th>TYPE</th></tr></thead>
              <tbody>{prints.map(t => <tr key={t.id}>
                <td className="mono muted">{clock(t.createdMs)}</td>
                <td className={(t.side || "").toUpperCase().startsWith("B") ? "positive" : "negative"}>
                  {t.side || "—"}</td>
                <td className="mono">{t.price === null ? "—" : px(t.price)}</td>
                <td className="mono">{t.size === null ? "—" : num(t.size, 4)}</td>
                <td>{t.price === null || t.size === null ? "—" : money(t.price * t.size, 0)}</td>
                <td className="muted">{t.tradeType || "—"}</td>
              </tr>)}</tbody>
            </table>}
      </Panel>

      <p className="disclaimer">
        Public read-only venue data. Funding is annualised from the mean of the
        window rather than the latest print — one elevated period extrapolated
        over a year is a headline, not a carry estimate. A positive rate means
        longs pay shorts. The insurance fund is the venue's backstop for
        liquidations that close through bankruptcy, shown here because it is
        the balance standing between a bad fill and socialised losses.
      </p>
    </>}
  </div>;
}
