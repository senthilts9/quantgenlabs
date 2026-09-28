import { Panel, Metric, StatusPill, Empty } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { dex } from "../../services/dexProviders";
import { money, num } from "../../utils/format";

const shortAddr = (a: string | null) =>
  !a ? "—" : `${a.slice(0, 6)}…${a.slice(-4)}`;

/**
 * On-chain strategy vaults (VTFs), ranked by capital actually in them.
 *
 * **Funded vaults are counted separately from listed ones, prominently.**
 * Anyone can deploy a vault on this venue, and most never take a deposit —
 * ~1,600 listed against a few hundred holding anything. A "1,614 vaults"
 * headline with no second number describes a directory and implies a market.
 *
 * Ranking by TVL rather than by return is the same discipline. Sorting by ROI
 * puts a vault that turned $40 into $80 above one compounding $5m, and the
 * first number is noise: on a small enough base, a single fill is a return.
 */
export function StrategyVaults() {
  const v = useAsync(() => dex.getVaults(25, "tvl"), [], 60000);
  const rows = v.data?.rows ?? [];

  // Drawdown is the honest counterweight to a return column, so the worst one
  // on screen gets a tile of its own rather than being buried mid-table.
  const worstDd = rows.reduce<number | null>((w, r) =>
    r.maxDrawdownPct === null ? w
      : w === null ? r.maxDrawdownPct : Math.min(w, r.maxDrawdownPct), null);

  return <div className="page">
    <div className="hero-row">
      <div><h1>Strategy Vaults</h1>
        <p>On-chain strategy vaults — capital, returns and drawdown, live</p></div>
      <StatusPill status={v.error ? "FEED ERROR"
        : v.data ? (v.data.staleS > 0 ? `STALE ${v.data.staleS.toFixed(0)}s` : "LIVE · MAINNET")
        : "CONNECTING"} />
    </div>

    {v.error && <Panel><Empty text={`Feed unavailable — ${v.error}`} /></Panel>}
    {!v.data && v.loading && <Panel><Empty text="Loading vaults…" /></Panel>}

    {v.data && <>
      <div className="metric-grid four">
        <Metric label="Aggregate TVL" value={money(v.data.aggregateTvl, 0)} />
        <Metric label="Vaults Funded" value={`${v.data.funded}`}
          sub={`of ${num(v.data.total, 0)} deployed`} />
        <Metric label="Unfunded" value={`${v.data.total - v.data.funded}`}
          tone="warning" sub="listed, never took a deposit" />
        <Metric label="Deepest Drawdown" value={worstDd === null ? "—" : `${worstDd.toFixed(1)}%`}
          tone={(worstDd ?? 0) < -20 ? "negative" : ""} sub="worst on this page" />
      </div>

      <Panel title="Vaults by capital deployed"
        right={<span className="muted">top {rows.length} by TVL</span>}>
        {rows.length === 0 ? <Empty text="No vaults returned." /> :           <table>
            <thead><tr>
              <th>VAULT</th><th>CURATOR</th><th>TVL</th><th>NET DEPOSITS</th>
              <th>TOTAL ROI</th><th>24H</th><th>MAX DD</th>
              <th>PROFIT SHARE</th><th>LOCKUP</th><th>OWNER</th>
            </tr></thead>
            <tbody>{rows.map(r => <tr key={r.address}>
              <td className="strong" title={r.strategy || r.description || undefined}>
                {r.name || shortAddr(r.address)}</td>
              <td className="muted">{r.curator || "—"}</td>
              <td>{r.tvl === null ? "—" : money(r.tvl, 0)}</td>
              <td className="muted">{r.netDeposits === null ? "—" : money(r.netDeposits, 0)}</td>
              <td className={(r.roiTotalPct ?? 0) >= 0 ? "positive" : "negative"}>
                {r.roiTotalPct === null ? "—" : `${r.roiTotalPct >= 0 ? "+" : ""}${r.roiTotalPct.toFixed(2)}%`}</td>
              <td className={(r.roi24hPct ?? 0) >= 0 ? "positive" : "negative"}>
                {r.roi24hPct === null ? "—" : `${r.roi24hPct >= 0 ? "+" : ""}${r.roi24hPct.toFixed(3)}%`}</td>
              <td className={(r.maxDrawdownPct ?? 0) < -20 ? "negative" : ""}>
                {r.maxDrawdownPct === null ? "—" : `${r.maxDrawdownPct.toFixed(1)}%`}</td>
              <td>{r.profitSharePct === null ? "—" : `${r.profitSharePct}%`}</td>
              <td className="muted">{r.lockupDays === null ? "—" : `${r.lockupDays}d`}</td>
              <td className="mono muted">{shortAddr(r.owner)}</td>
            </tr>)}</tbody>
          </table>}
      </Panel>

      <p className="disclaimer">
        Public read-only venue data. Vault deployment is permissionless, so the
        listed count is a directory rather than a market — the funded count is
        the one that describes it. Returns are the venue's own reported figures
        and are shown beside maximum drawdown because a return without one is
        half a track record. Nothing here is investable from this console: it
        holds no account on that venue and no deposit path.
      </p>
    </>}
  </div>;
}
