import { Panel, Metric, StatusPill } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { liveProviders } from "../../services/liveProviders";
import { money, num, px } from "../../utils/format";

/**
 * Live perpetual board.
 *
 * One upstream request covers the whole universe, so polling the board costs
 * the same as polling one contract.
 */
export function Perpetuals() {
  const board = useAsync(() => liveProviders.getPerps(10, "mainnet"), [], 20000);
  const rows = board.data?.rows ?? [];

  // Open-interest-weighted funding: a simple mean would let a dormant
  // contract with an extreme rate dominate the read of what the market is
  // actually paying to stay long.
  const oiTotal = rows.reduce((a, r) => a + r.openInterestUsd, 0);
  const wFunding = oiTotal > 0
    ? rows.reduce((a, r) => a + (r.fundingAprPct ?? 0) * r.openInterestUsd, 0) / oiTotal
    : null;

  return <div className="page">
    <div className="hero-row">
      <div><h1>Perpetuals</h1>
        <p>Mark/index, basis, funding and open interest — live venue data</p></div>
      <StatusPill status={board.error ? "FEED ERROR"
        : board.data ? `LIVE · ${board.data.network.toUpperCase()}` : "CONNECTING"} />
    </div>

    {board.error && <Panel><div className="empty">Feed unavailable — {board.error}</div></Panel>}
    {!board.data && board.loading && <Panel><div className="empty">Loading perp board…</div></Panel>}

    {board.data && <>
      <div className="metric-grid four">
        <Metric label="Aggregate Open Interest" value={money(board.data.aggregateOiUsd, 0)} />
        <Metric label="24h Notional Volume" value={money(board.data.aggregateVolUsd, 0)} />
        <Metric label="Contracts Listed" value={`${board.data.contractsTotal}`} />
        <Metric label="OI-Weighted Funding APR"
          value={wFunding === null ? "—" : `${wFunding >= 0 ? "+" : ""}${wFunding.toFixed(2)}%`}
          tone={wFunding !== null && wFunding >= 0 ? "positive" : "warning"} />
      </div>

      <Panel title={`Perpetual Contracts — ${board.data.source}`}
        right={<span className="muted">by open interest</span>}>
        <table>
          <thead><tr>
            <th>CONTRACT</th><th>MARK</th><th>INDEX</th><th>BASIS</th>
            <th>FUNDING APR</th><th>OPEN INTEREST</th><th>24H VOLUME</th>
            <th>24H</th><th>MAX LEV</th>
          </tr></thead>
          <tbody>{rows.map(r => <tr key={r.market}>
            <td className="strong">{r.market}</td>
            <td className="mono">{px(r.mark)}</td>
            <td className="mono">{px(r.index)}</td>
            {/* Positive basis = perp above index = longs pay shorts. */}
            <td className={r.basisBps >= 0 ? "positive" : "negative"}>
              {r.basisBps >= 0 ? "+" : ""}{r.basisBps.toFixed(2)} bps</td>
            <td className={(r.fundingAprPct ?? 0) >= 0 ? "positive" : "negative"}>
              {r.fundingAprPct === null ? "—"
                : `${r.fundingAprPct >= 0 ? "+" : ""}${r.fundingAprPct.toFixed(2)}%`}</td>
            <td>{money(r.openInterestUsd, 0)}</td>
            <td>{money(r.dayVolumeUsd, 0)}</td>
            <td className={(r.change24hPct ?? 0) >= 0 ? "positive" : "negative"}>
              {r.change24hPct === null ? "—" : `${r.change24hPct.toFixed(2)}%`}</td>
            <td>{r.maxLeverage ? `${r.maxLeverage}x` : "—"}</td>
          </tr>)}</tbody>
        </table>
      </Panel>

      <div className="grid two">
        <Panel title="Open Interest Distribution">
          <div className="bars">{rows.slice(0, 6).map(r => {
            const share = oiTotal > 0 ? (r.openInterestUsd / oiTotal) * 100 : 0;
            return <div key={r.market}>
              <span>{r.coin}</span>
              <div className="bar"><i style={{ width: `${share.toFixed(1)}%` }} /></div>
              <b>{share.toFixed(1)}%</b>
            </div>;
          })}</div>
        </Panel>
        <Panel title="Carry — cost of staying long, per $10k per day">
          <div className="cost-grid">{rows.slice(0, 4).map(r => {
            // Funding is quoted per period; APR/365 is the daily drag.
            const perDay = r.fundingAprPct === null ? null
              : (r.fundingAprPct / 365) * 100;   // $10k * pct/100
            return <div key={r.market}>
              <span>{r.coin}</span>
              <b className={(perDay ?? 0) <= 0 ? "positive" : "negative"}>
                {perDay === null ? "—" : `${perDay >= 0 ? "-" : "+"}$${Math.abs(perDay).toFixed(2)}`}
              </b>
            </div>;
          })}</div>
        </Panel>
      </div>

      <p className="disclaimer">
        Public read-only venue data ({board.data.network}). Funding annualises
        over 365 days — this market does not close at the weekend. Basis is
        signed against the index: positive means the perp trades above spot,
        which is when longs pay shorts.
      </p>
    </>}
  </div>;
}
