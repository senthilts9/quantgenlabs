import { useState } from "react";
import { Panel, Metric, StatusPill, Tabs, Empty } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { dex } from "../../services/dexProviders";
import { money, num, px } from "../../utils/format";

/** Board tab → the venue's `asset_kind`. */
const KINDS: Record<string, string> = {
  "Perpetuals": "PERP",
  "Spot": "SPOT",
  "Options": "OPTION",
  "All": "ALL",
};

/**
 * Funding is published per period, and the period is per contract.
 *
 * Annualising with a hardcoded 8h would misreport every market on a different
 * schedule — and the venue does not run one schedule for everything. The period
 * comes back on each row for exactly this reason.
 */
function fundingApr(rate: number | null, periodHours: number | null): number | null {
  if (rate === null || !periodHours) return null;
  return rate * ((365 * 24) / periodHours) * 100;
}

/**
 * Live market browser across the venue's whole listed universe.
 *
 * The listed counts sit in the header rather than only on the active tab,
 * because the shape of the universe is itself the story: 2,980 listed options
 * against 63 perps says the option surface is quoted by market makers rather
 * than traded, and a reader who only sees the perp tab never learns that.
 */
export function Markets() {
  const [tab, setTab] = useState("Perpetuals");
  const kind = KINDS[tab];
  const board = useAsync(() => dex.getMarkets(kind, 60), [kind], 20000);

  const rows = board.data?.rows ?? [];
  const listed = board.data?.listed ?? {};
  const isOption = kind === "OPTION";

  return <div className="page">
    <div className="hero-row">
      <div><h1>Markets</h1>
        <p>DEX venue · mainnet — every listed contract, marked live</p></div>
      <StatusPill status={board.error ? "FEED ERROR"
        : board.data ? (board.data.staleS > 0 ? `STALE ${board.data.staleS.toFixed(0)}s` : "LIVE · MAINNET")
        : "CONNECTING"} />
    </div>

    {board.error && <Panel><Empty text={`Feed unavailable — ${board.error}`} /></Panel>}
    {!board.data && board.loading && <Panel><Empty text="Loading market universe…" /></Panel>}

    {board.data && <>
      <div className="metric-grid four">
        <Metric label="Perpetuals Listed" value={`${listed.PERP ?? 0}`} />
        <Metric label="Options Listed" value={`${listed.OPTION ?? 0}`}
          sub="quoted, largely untraded" />
        <Metric label="Open Interest" value={money(board.data.aggregateOiUsd, 0)}
          sub={`${tab.toLowerCase()} only`} />
        <Metric label="24h Volume" value={num(board.data.aggregateVolume24h, 0)}
          sub="base units" />
      </div>

      <Panel title="Asset Class"
        right={<span className="muted">{board.data.matched} matched · showing {rows.length}</span>}>
        <Tabs items={Object.keys(KINDS)} active={tab} onChange={setTab} />
      </Panel>

      <Panel title={`${tab} — by open interest`}>
        {rows.length === 0 ? <Empty text={`Nothing listed under ${tab}.`} /> : <table>
          <thead><tr>
            <th>SYMBOL</th><th>MARK</th><th>BID</th><th>ASK</th><th>SPREAD</th>
            <th>24H</th>{!isOption && <th>FUNDING APR</th>}
            <th>OPEN INTEREST</th><th>24H VOL</th><th>TICK</th>
          </tr></thead>
          <tbody>{rows.map(r => {
            const apr = fundingApr(r.fundingRate, r.fundingPeriodHours);
            return <tr key={r.symbol}>
              <td className="strong">{r.symbol}</td>
              <td className="mono">{r.mark === null ? "—" : px(r.mark)}</td>
              {/* An unquoted contract has no bid. Rendering null as 0.00 would
                  claim someone is bidding zero for it. */}
              <td className="mono">{r.bid === null ? "—" : px(r.bid)}</td>
              <td className="mono">{r.ask === null ? "—" : px(r.ask)}</td>
              <td className={(r.spreadBps ?? 0) > 100 ? "warning" : ""}>
                {r.spreadBps === null ? "—" : `${r.spreadBps.toFixed(1)} bps`}</td>
              <td className={(r.change24hPct ?? 0) >= 0 ? "positive" : "negative"}>
                {r.change24hPct === null ? "—" : `${r.change24hPct >= 0 ? "+" : ""}${r.change24hPct.toFixed(2)}%`}</td>
              {!isOption && <td className={(apr ?? 0) >= 0 ? "positive" : "negative"}>
                {apr === null ? "—" : `${apr >= 0 ? "+" : ""}${apr.toFixed(2)}%`}</td>}
              <td>{r.openInterestUsd === null ? "—" : money(r.openInterestUsd, 0)}</td>
              <td>{r.volume24h === null ? "—" : num(r.volume24h, 2)}</td>
              <td className="muted mono">{r.tickSize === null ? "—" : r.tickSize}</td>
            </tr>;
          })}</tbody>
        </table>}
      </Panel>

      <p className="disclaimer">
        Public read-only venue data, DEX venue · mainnet. Open interest is quoted
        in base units by the venue and marked to USD here so contracts on
        different underlyings can be ranked against each other. Funding is
        annualised over each contract's own period — the venue does not run a
        single schedule for every market. A blank is a contract nobody is
        quoting, not a zero.
      </p>
    </>}
  </div>;
}
