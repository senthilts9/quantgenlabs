import { useEffect, useState } from "react";
import { Empty, Panel, StatusPill, Tabs } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { liveProviders } from "../../services/liveProviders";
import type { Execution, Order } from "../../types/domain";
import { ago, bps, clock, money, num, px } from "../../utils/format";

/**
 * OMS/EMS blotter, read back from the run tables.
 *
 * The orders and fills on this screen are **rows in the database**, not client
 * state: every one was written by `/lifecycle` inside a single transaction
 * covering the parent, its slices, its fills and its TCA, and is read here
 * through `/runs`. That round trip is the point. A blotter rendered from the
 * submit handler's own return value proves the browser can remember what it
 * just did; a blotter that reloads from the store proves the desk persisted
 * it, which is the property reconciliation actually depends on.
 *
 * The Phase 1 fixtures are kept, in their own tab and labelled. They are what
 * the venue/parent columns are meant to look like once a gateway fills them;
 * deleting them would lose that, and mixing them into the live tables would
 * cost the real rows their credibility.
 */

const POLL_MS = 10000;

export function OrdersExecutions({ orders, executions, onCancel, refreshKey = 0 }: {
  orders: Order[];
  executions: Execution[];
  onCancel: (id: string) => void;
  refreshKey?: number;
}) {
  const [tab, setTab] = useState("Orders");
  const [selected, setSelected] = useState<string | null>(null);

  const runs = useAsync(() => liveProviders.getRuns(50), [refreshKey], POLL_MS);
  const rows = runs.data ?? [];
  const newest = rows[0] ?? null;

  // Follow the newest run until the operator picks one, then stay put — a
  // selection that jumps every poll cannot be read. Submitting a new order
  // releases the selection, so the screen you land on after sending is
  // showing the order you just sent rather than the one before it.
  useEffect(() => { setSelected(null); }, [refreshKey]);
  useEffect(() => {
    if (!selected && newest) setSelected(newest.parentId);
  }, [newest, selected]);

  const detail = useAsync(
    async () => (selected ? liveProviders.getRun(selected) : null), [selected]);
  const fills = detail.data?.fills ?? [];
  const children = detail.data?.children ?? [];

  const lastFill = detail.data && newest && detail.data.parent.parentId === newest.parentId
    ? fills[fills.length - 1] ?? null : null;

  return <div className="page">
    <div className="hero-row">
      <div><h1>Orders &amp; Executions</h1>
        <p>OMS/EMS blotter with parent/child traceability, read back from the run store</p></div>
      <StatusPill status={runs.error ? "STORE UNAVAILABLE"
        : runs.data ? `PERSISTED · ${rows.length} RUNS` : "LOADING"} />
    </div>

    {/* ---------------------------------------------------- most recent */}
    {runs.error && <div className="notice reject">Run store unreachable — {runs.error}</div>}
    {!runs.error && !rows.length && !runs.loading && <Panel title="Most Recent Transaction">
      <Empty text="No runs persisted yet. Submit an order from the Trading Workstation — it executes against the local simulator and is written to the run tables." />
    </Panel>}

    {newest && <Panel title="Most Recent Transaction"
      right={<span className="muted">{ago(newest.createdMs)} · {newest.venue}</span>}>
      <div className="recent">
        <div className="recent-head">
          <b className={newest.side === "BUY" ? "positive" : "negative"}>{newest.side}</b>
          <b className="strong">{newest.market}</b>
          <span className="mono">{newest.algo}</span>
          <StatusPill status={newest.status} />
          <span className="mono muted">{newest.parentId}</span>
        </div>
        <div className="recent-grid">
          <div><span>Filled</span><b>{num(newest.filledSize, 6)} / {num(newest.totalSize, 6)}</b></div>
          <div><span>Avg Price</span><b>{newest.avgPrice === null ? "—" : px(newest.avgPrice)}</b></div>
          <div><span>Arrival</span><b>{px(newest.arrivalPrice)}</b></div>
          <div><span>Notional</span>
            <b>{newest.avgPrice === null ? "—" : money(newest.avgPrice * newest.filledSize)}</b></div>
          {/* Signed, and null when nothing was measured: a run whose benchmark
              window held no prints has no slippage, and "0.00 bps" would claim
              it matched the benchmark exactly. */}
          <div><span>Shortfall vs Arrival</span>
            <b className={newest.shortfallBps === null ? "" : newest.shortfallBps <= 0 ? "positive" : "negative"}>
              {bps(newest.shortfallBps)}</b></div>
          <div><span>Total Cost</span><b>{bps(newest.totalCostBps)}</b></div>
          <div><span>Fills</span><b>{newest.fillCount} across {newest.sliceCount} slices</b></div>
          <div><span>Executed</span>
            <b>{clock(newest.completedMs ?? newest.createdMs)}</b></div>
        </div>
        {lastFill && <div className="recent-fill">
          Last fill · <b>{num(lastFill.size, 6)}</b> @ <b>{px(lastFill.price)}</b>
          {" · "}fee {money(lastFill.fee)} · {lastFill.liquidity} · slice {lastFill.sliceIndex}
          {" · "}{clock(lastFill.tsMs)}
        </div>}
      </div>
    </Panel>}

    <Tabs items={["Orders", "Executions", "Slices", "Session", "Fixtures"]}
      active={tab} onChange={setTab} />

    {/* ------------------------------------------------------- parents */}
    {tab === "Orders" && <Panel title="Parent Orders"
      right={<span className="muted">from /api/cryptodesk/runs</span>}>
      {!rows.length ? <Empty text={runs.loading ? "Loading runs…" : "No persisted runs."} />
        : <table className="dense"><thead><tr>
          <th>TIME</th><th>NETWORK</th><th>PARENT ID</th><th>INSTRUMENT</th><th>SIDE</th><th>ALGO</th>
          <th>SIZE</th><th>FILLED</th><th>AVG PX</th><th>ARRIVAL</th>
          <th>SHORTFALL</th><th>FILLS</th><th>VENUE</th><th>ACTOR</th><th>STATUS</th>
        </tr></thead><tbody>
          {rows.map(r => <tr key={r.parentId}
            className={r.parentId === selected ? "selected" : ""}
            onClick={() => setSelected(r.parentId)}>
            <td>{clock(r.createdMs)}</td>
            {/* The most consequential column here: real money and a rehearsal
                are otherwise both just a row with a price. */}
            <td><span className={`net ${r.network ?? "unknown"}`}>
              {r.network ?? "unknown"}</span></td>
            {/* Middle-truncated: the time prefix and the random suffix are
                both identifying, the middle is not. Full value on hover. */}
            <td className="link mono" title={r.parentId}>
              {r.parentId.length > 16
                ? `${r.parentId.slice(0, 6)}…${r.parentId.slice(-6)}`
                : r.parentId}</td>
            <td className="strong">{r.market}</td>
            <td className={r.side === "BUY" ? "positive" : "negative"}>{r.side}</td>
            <td>{r.algo}</td>
            <td>{num(r.totalSize, 6)}</td>
            <td>{num(r.filledSize, 6)}</td>
            <td>{r.avgPrice === null ? "—" : px(r.avgPrice)}</td>
            <td>{px(r.arrivalPrice)}</td>
            <td className={r.shortfallBps === null ? "" : r.shortfallBps <= 0 ? "positive" : "negative"}>
              {bps(r.shortfallBps)}</td>
            <td>{r.fillCount}</td>
            <td className={r.venue === "paper" ? "" : "strong"}>{r.venue}</td>
            <td className="mono" title={r.actor ?? undefined}>
              {r.actor ? `${r.actor.slice(0, 6)}…${r.actor.slice(-4)}`
                : <span className="muted">unattributed</span>}</td>
            <td><StatusPill status={r.status} /></td>
          </tr>)}
        </tbody></table>}
    </Panel>}

    {/* --------------------------------------------------------- fills */}
    {tab === "Executions" && <Panel
      title={`Executions${selected ? ` · ${selected}` : ""}`}
      right={<span className="muted">{fills.length} fills</span>}>
      {detail.error && <div className="notice reject">{detail.error}</div>}
      {!selected ? <Empty text="Select a parent order." />
        : detail.loading && !detail.data ? <Empty text="Loading fills…" />
        : !fills.length ? <Empty text="This run recorded no fills." />
        : <table className="dense"><thead><tr>
          <th>TIME</th><th>FILL ID</th><th>SLICE</th><th>INSTRUMENT</th><th>SIDE</th>
          <th>QTY</th><th>PRICE</th><th>NOTIONAL</th><th>FEE</th><th>LIQUIDITY</th>
        </tr></thead><tbody>
          {fills.map(f => <tr key={f.fillId}>
            <td>{clock(f.tsMs)}</td>
            <td className="link mono">{f.fillId}</td>
            <td>{f.sliceIndex}</td>
            <td className="strong">{detail.data!.parent.market}</td>
            <td className={detail.data!.parent.side === "BUY" ? "positive" : "negative"}>
              {detail.data!.parent.side}</td>
            <td>{num(f.size, 6)}</td>
            <td>{px(f.price)}</td>
            <td>{money(f.size * f.price)}</td>
            <td>{money(f.fee)}</td>
            <td>{f.liquidity}</td>
          </tr>)}
        </tbody></table>}
    </Panel>}

    {/* -------------------------------------------------------- slices */}
    {tab === "Slices" && <Panel
      title={`Child Orders${selected ? ` · ${selected}` : ""}`}
      right={<span className="muted">{children.length} slices</span>}>
      {!selected ? <Empty text="Select a parent order." />
        : !children.length ? <Empty text={detail.loading ? "Loading slices…" : "No slices recorded."} />
        : <table className="dense"><thead><tr>
          <th>SLICE</th><th>SCHEDULED</th><th>CLIENT ID</th><th>TARGET</th>
          <th>WEIGHT</th><th>FILLED</th><th>AVG PX</th><th>STATUS</th>
        </tr></thead><tbody>
          {children.map(c => <tr key={c.sliceIndex}>
            <td>{c.sliceIndex}</td>
            <td>{clock(c.scheduledMs)}</td>
            <td className="mono">{c.clientId ?? "—"}</td>
            <td>{num(c.scheduledSize, 6)}</td>
            <td>{num(c.weight * 100, 2)}%</td>
            <td>{num(c.filledSize, 6)}</td>
            <td>{c.avgPrice === null ? "—" : px(c.avgPrice)}</td>
            <td><StatusPill status={c.status} /></td>
          </tr>)}
        </tbody></table>}
    </Panel>}

    {/* ------------------------------------------------------- session */}
    {tab === "Session" && <Panel title="This Session"
      right={<span className="muted">client-side, not persisted</span>}>
      <div className="notice warn">
        Orders staged in this browser tab. A row appears here the moment the
        ticket is accepted; it is replaced by the persisted parent above once
        the run completes. Nothing in this tab survives a reload.
      </div>
      {!orders.length ? <Empty text="No orders staged this session." />
        : <table className="dense"><thead><tr>
          <th>TIME</th><th>ORDER ID</th><th>ACTOR</th><th>INSTRUMENT</th><th>SIDE</th>
          <th>TYPE / ALGO</th><th>QTY</th><th>FILLED</th><th>AVG PX</th>
          <th>VENUE</th><th>STATUS</th><th></th>
        </tr></thead><tbody>
          {orders.map(o => <tr key={o.id}>
            <td>{o.time}</td>
            <td className="link">{o.id}</td>
            <td>{o.actor ?? "anonymous"}</td>
            <td className="strong">{o.symbol}</td>
            <td className={o.side === "BUY" ? "positive" : "negative"}>{o.side}</td>
            <td>{o.type} / {o.algo}</td>
            <td>{o.qty}</td>
            <td>{o.filled}</td>
            <td>{o.avgPrice?.toLocaleString() || "—"}</td>
            <td>{o.venue}</td>
            <td><StatusPill status={o.status} /></td>
            <td>{["WORKING", "PARTIAL", "ACCEPTED"].includes(o.status) &&
              <button className="mini" onClick={() => onCancel(o.id)}>Cancel</button>}</td>
          </tr>)}
        </tbody></table>}
    </Panel>}

    {/* ------------------------------------------------------ fixtures */}
    {tab === "Fixtures" && <>
      <div className="notice warn">
        Phase 1 fixture data. Kept as the shape a venue gateway is expected to
        fill — parent/child linkage, venue routing, per-execution fees — none
        of which came from a market. Labelled rather than deleted, and never
        mixed into the tables above.
      </div>
      <Panel title="Fixture Executions"><table className="dense"><thead><tr>
        <th>TIME</th><th>EXECUTION</th><th>ORDER</th><th>INSTRUMENT</th><th>SIDE</th>
        <th>QTY</th><th>PRICE</th><th>FEE</th><th>VENUE</th>
      </tr></thead><tbody>
        {executions.map(x => <tr key={x.id}>
          <td>{x.time}</td><td className="link">{x.id}</td><td>{x.orderId}</td>
          <td className="strong">{x.symbol}</td>
          <td className={x.side === "BUY" ? "positive" : "negative"}>{x.side}</td>
          <td>{x.qty}</td><td>{x.price.toLocaleString()}</td>
          <td>{money(x.fee)}</td><td>{x.venue}</td>
        </tr>)}
      </tbody></table></Panel>
    </>}
  </div>;
}
