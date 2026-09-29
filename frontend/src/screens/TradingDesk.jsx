/* TradingDesk.jsx — order ticket wired to the C++ simulated venue.
 *
 * Four panes: the ladder, the ticket, the blotter, the FIX monitor. Every one is
 * fed by /api/desk, which drives mkt::venue::SimulatedDesk — a real client FIX
 * session talking to a real matching engine.
 *
 * What makes this worth clicking rather than reading: the FIX monitor shows the
 * ACTUAL bytes that moved for the order you just sent, next to the blotter row
 * they updated. The blotter is rebuilt from the ExecutionReports that came back,
 * not from what the ticket hoped would happen, which is the distinction between
 * an OMS and a wish list.
 *
 * Simulated throughout. No exchange, no money — the API says so on every
 * response and the screen repeats it.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { T, ALPHA, MONO } from "../theme.js";
import DataTable from "../components/DataTable.jsx";

const j = async (r) => ({ ok: r.ok, status: r.status, data: await r.json().catch(() => null) });
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b ?? {}) }).then(j);
const get = (u) => fetch(u).then(j);

const n2 = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
const qty = (x) => (x == null ? "—" : Number(x).toLocaleString());

const STATUS_TONE = {
  filled: T.teal, partially_filled: T.blue, new: T.text,
  cancelled: T.dim, rejected: T.red, pending: T.faint,
};

function Panel({ title, sub, right, children, pad = 14 }) {
  return (
    <section style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 9, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      <header style={{ padding: "10px 13px", borderBottom: `1px solid ${T.lineSoft}`, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600 }}>{title}</div>
          {sub && <div style={{ fontSize: 10.5, color: T.dim, marginTop: 2 }}>{sub}</div>}
        </div>
        {right}
      </header>
      <div style={{ padding: pad, flex: 1, minHeight: 0 }}>{children}</div>
    </section>
  );
}

function Pill({ children, tone = T.dim }) {
  return <span style={{ padding: "2px 7px", borderRadius: 20, fontSize: 9.5, letterSpacing: 0.6, color: tone, border: `1px solid ${tone}`, fontFamily: MONO, textTransform: "uppercase", whiteSpace: "nowrap" }}>{children}</span>;
}

const inp = { background: T.panel2, border: `1px solid ${T.line}`, color: T.text, borderRadius: 6, padding: "6px 9px", fontFamily: MONO, fontSize: 12.5, width: "100%" };
const lbl = { color: T.faint, fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase", display: "block", marginBottom: 4 };

/* ------------------------------------------------------------------ ladder */
function Ladder({ book, onPick }) {
  if (!book) return null;
  const rows = Math.max(book.bids?.length || 0, book.asks?.length || 0);
  const maxQ = Math.max(1, ...(book.bids || []).map((l) => l.qty), ...(book.asks || []).map((l) => l.qty));
  return (
    <>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
        <Pill tone={book.is_crossed ? T.red : T.teal}>{book.is_crossed ? "crossed" : "uncrossed"}</Pill>
        {book.spread != null && <Pill>spread {n2(book.spread)}</Pill>}
        {book.micro_price != null && <Pill tone={T.violet}>micro {n2(book.micro_price, 4)}</Pill>}
        <Pill tone={book.imbalance > 0 ? T.green : book.imbalance < 0 ? T.red : T.dim}>
          imb {n2(book.imbalance, 3)}
        </Pill>
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
        <thead><tr>
          {["Bid qty", "Bid", "Ask", "Ask qty"].map((h, i) => (
            <th key={h} style={{ padding: "5px 8px", color: T.faint, fontSize: 9, letterSpacing: 0.8, textTransform: "uppercase", textAlign: i < 2 ? "right" : "left", borderBottom: `1px solid ${T.line}` }}>{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {Array.from({ length: rows }).map((_, i) => {
            const b = book.bids[i], a = book.asks[i];
            return (
              <tr key={i}>
                <td style={{ padding: "3px 8px", textAlign: "right", color: T.dim, position: "relative" }}>
                  {b && <span style={{ position: "absolute", right: 0, top: 2, bottom: 2, width: `${(b.qty / maxQ) * 70}%`, background: ALPHA.green }} />}
                  <span style={{ position: "relative" }}>{b ? qty(b.qty) : ""}</span>
                </td>
                <td onClick={() => b && onPick(b.price)} title={b ? "Use this price" : undefined}
                    style={{ padding: "3px 8px", textAlign: "right", color: T.green, cursor: b ? "pointer" : "default" }}>
                  {b ? n2(b.price) : ""}
                </td>
                <td onClick={() => a && onPick(a.price)} title={a ? "Use this price" : undefined}
                    style={{ padding: "3px 8px", color: T.red, cursor: a ? "pointer" : "default" }}>
                  {a ? n2(a.price) : ""}
                </td>
                <td style={{ padding: "3px 8px", color: T.dim, position: "relative" }}>
                  {a && <span style={{ position: "absolute", left: 0, top: 2, bottom: 2, width: `${(a.qty / maxQ) * 70}%`, background: ALPHA.red }} />}
                  <span style={{ position: "relative" }}>{a ? qty(a.qty) : ""}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p style={{ color: T.faint, fontSize: 10, marginTop: 8, marginBottom: 0 }}>Click a price to load it into the ticket.</p>
    </>
  );
}

/* ------------------------------------------------------------------ ticket */
function Ticket({ onSend, busy, price, setPrice }) {
  const [side, setSide] = useState("1");
  const [qtyV, setQty] = useState(250);
  const [ordType, setOrdType] = useState("2");
  const [tif, setTif] = useState("0");

  const submit = (e) => {
    e.preventDefault();
    onSend({ side, qty: Number(qtyV), price: Number(price), ord_type: ordType, tif, symbol: "AAPL" });
  };

  const sideBtn = (v, label, tone) => (
    <button type="button" onClick={() => setSide(v)} style={{
      flex: 1, padding: "8px 0", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 600,
      background: side === v ? tone : T.panel2, color: side === v ? T.bg : tone,
      border: `1px solid ${tone}`,
    }}>{label}</button>
  );

  return (
    <form onSubmit={submit}>
      <div style={{ display: "flex", gap: 8, marginBottom: 11 }}>
        {sideBtn("1", "BUY", T.green)}
        {sideBtn("2", "SELL", T.red)}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9, marginBottom: 9 }}>
        <label><span style={lbl}>Quantity</span>
          <input style={inp} type="number" min="1" value={qtyV} onChange={(e) => setQty(e.target.value)} />
        </label>
        <label><span style={lbl}>Limit price</span>
          <input style={{ ...inp, opacity: ordType === "1" ? 0.4 : 1 }} type="number" step="0.01" min="0"
                 disabled={ordType === "1"} value={price} onChange={(e) => setPrice(e.target.value)} />
        </label>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9, marginBottom: 12 }}>
        <label><span style={lbl}>Type (FIX 40)</span>
          <select style={inp} value={ordType} onChange={(e) => setOrdType(e.target.value)}>
            <option value="2">2 · Limit</option>
            <option value="1">1 · Market</option>
          </select>
        </label>
        <label><span style={lbl}>TIF (FIX 59)</span>
          <select style={inp} value={tif} onChange={(e) => setTif(e.target.value)}>
            <option value="0">0 · Day</option>
            <option value="3">3 · IOC</option>
            <option value="4">4 · FOK</option>
          </select>
        </label>
      </div>
      <button type="submit" disabled={busy} style={{
        width: "100%", padding: "9px 0", borderRadius: 6, border: "none", cursor: busy ? "wait" : "pointer",
        background: side === "1" ? T.green : T.red, color: T.bg, fontWeight: 700, fontSize: 12.5, letterSpacing: 0.4,
      }}>
        {busy ? "SENDING…" : `SEND ${side === "1" ? "BUY" : "SELL"} · NewOrderSingle (35=D)`}
      </button>
      <p style={{ color: T.faint, fontSize: 10, lineHeight: 1.5, marginTop: 9, marginBottom: 0 }}>
        Encoded by the C++ session layer, which owns MsgSeqNum, BodyLength and CheckSum.
        The browser never assembles FIX.
      </p>
    </form>
  );
}

/* ----------------------------------------------------------------- blotter */
/* Uses the shared DataTable: virtualised, so a blotter that grows through a
 * session stays responsive, plus sort, filter and CSV export for free. */
function Blotter({ rows, onCancel, selected, onSelect }) {
  const columns = [
    { key: "cl_ord_id", header: "ClOrdID", width: "15%", mono: true,
      tone: (r) => (r.cl_ord_id === selected ? T.teal : T.text) },
    { key: "side", header: "Side", width: "7%",
      raw: (r) => (r.side === "1" ? "BUY" : "SELL"),
      render: (r) => (r.side === "1" ? "BUY" : "SELL"),
      tone: (r) => (r.side === "1" ? T.green : T.red) },
    { key: "order_qty", header: "Qty", width: "9%", align: "right",
      raw: (r) => r.order_qty, render: (r) => qty(r.order_qty) },
    { key: "limit_price", header: "Limit", width: "9%", align: "right",
      raw: (r) => (r.ord_type === "1" ? null : r.limit_price),
      render: (r) => (r.ord_type === "1" ? "MKT" : n2(r.limit_price)),
      tone: () => T.dim },
    { key: "tif", header: "TIF", width: "6%",
      raw: (r) => r.tif,
      render: (r) => ({ "0": "DAY", "3": "IOC", "4": "FOK" }[r.tif] ?? r.tif),
      tone: () => T.dim },
    { key: "status", header: "Status (39)", width: "13%",
      tone: (r) => STATUS_TONE[r.status] || T.text },
    { key: "cum_qty", header: "Cum", width: "8%", align: "right",
      raw: (r) => r.cum_qty, render: (r) => qty(r.cum_qty) },
    { key: "leaves_qty", header: "Leaves", width: "8%", align: "right",
      raw: (r) => r.leaves_qty, render: (r) => qty(r.leaves_qty), tone: () => T.dim },
    { key: "avg_px", header: "Avg px", width: "9%", align: "right",
      raw: (r) => (r.cum_qty > 0 ? r.avg_px : null),
      render: (r) => (r.cum_qty > 0 ? n2(r.avg_px) : "—") },
    { key: "actions", header: "", width: "16%", align: "right", mono: false,
      raw: (r) => r.reject_text || "",
      render: (r) => {
        const live = r.status === "new" || r.status === "partially_filled";
        if (live) {
          return (
            <button onClick={(e) => { e.stopPropagation(); onCancel(r.cl_ord_id); }}
              style={{ background: "none", border: `1px solid ${T.amber}`, color: T.amber,
                       borderRadius: 5, padding: "1px 7px", fontSize: 10, cursor: "pointer" }}>
              cancel
            </button>
          );
        }
        return r.reject_text
          ? <span title={r.reject_text} style={{ color: T.red, fontSize: 10 }}>{r.reject_text.slice(0, 20)}</span>
          : null;
      } },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      height={230}
      rowKey={(r) => r.cl_ord_id}
      selectedKey={selected}
      onRowClick={(r) => onSelect(r.cl_ord_id)}
      exportName="blotter"
      empty="No orders yet."
      dense
    />
  );
}

/* ------------------------------------------------------------ FIX monitor */
function FixMonitor({ wire, filterId }) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest" }); }, [wire?.length]);
  if (!wire?.length) return <div style={{ color: T.faint, fontSize: 11.5 }}>No traffic yet.</div>;

  const shown = filterId ? wire.filter((w) => w.raw.includes(`11=${filterId}|`)) : wire;
  const list = shown.length ? shown : wire;

  return (
    <div style={{ maxHeight: 300, overflowY: "auto", fontFamily: MONO, fontSize: 10.5, lineHeight: 1.65 }}>
      {list.map((w, i) => (
        <div key={i} style={{ display: "flex", gap: 7, padding: "2px 0", borderBottom: `1px solid ${T.lineSoft}` }}>
          <span style={{ color: w.outbound ? T.blue : T.teal, flexShrink: 0 }}>{w.outbound ? "▲ OUT" : "▼ IN "}</span>
          <span style={{ color: T.faint, flexShrink: 0, width: 26 }}>{w.seq}</span>
          <span style={{ color: T.violet, flexShrink: 0, width: 22 }}>{w.msg_type}</span>
          <span style={{ color: T.dim, wordBreak: "break-all" }}>{w.raw}</span>
        </div>
      ))}
      <div ref={endRef} />
      {filterId && shown.length === 0 && (
        <div style={{ color: T.faint, paddingTop: 6 }}>No messages carry ClOrdID {filterId}; showing all.</div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ screen */
export default function TradingDesk() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [price, setPrice] = useState(100.01);
  const [selected, setSelected] = useState(null);

  const boot = useCallback(async () => {
    setErr(null);
    const r = await post("/api/desk/session", { mid: 100, levels: 6, level_qty: 200 });
    if (!r.ok) { setErr(r.data?.detail || `HTTP ${r.status}`); return; }
    setSt(r.data);
  }, []);

  useEffect(() => { boot(); }, [boot]);

  const send = async (body) => {
    if (!st) return;
    setBusy(true);
    const r = await post(`/api/desk/${st.desk_id}/order`, body);
    setBusy(false);
    if (!r.ok) { setErr(r.data?.detail || `HTTP ${r.status}`); return; }
    setSt(r.data);
    setSelected(r.data.this_order?.cl_ord_id ?? null);
  };

  const cancel = async (id) => {
    if (!st) return;
    const r = await post(`/api/desk/${st.desk_id}/cancel`, { orig_cl_ord_id: id });
    if (r.ok) { setSt(r.data); setSelected(id); }
  };

  if (err) {
    return (
      <div style={{ background: T.panel, border: `1px solid ${T.amber}`, borderRadius: 9, padding: 16, color: T.amber, fontSize: 12.5, lineHeight: 1.6 }}>
        {err}
        <div style={{ marginTop: 9 }}>
          <button onClick={boot} style={{ background: T.teal, color: T.bg, border: "none", borderRadius: 6, padding: "6px 14px", cursor: "pointer", fontSize: 12 }}>Retry</button>
        </div>
      </div>
    );
  }
  if (!st) return <div style={{ color: T.dim, padding: 16, fontSize: 12.5 }}>Opening FIX session…</div>;

  const pos = st.position || {};
  const vs = st.venue_stats || {};

  return (
    <div>
      <div style={{ display: "flex", gap: 7, flexWrap: "wrap", alignItems: "center", marginBottom: 13 }}>
        <Pill tone={st.connected ? T.teal : T.red}>{st.connected ? "FIX session active" : "disconnected"}</Pill>
        <Pill tone={T.teal}>impl: {st.impl}</Pill>
        <Pill tone={T.amber}>simulated</Pill>
        <Pill>desk {st.desk_id}</Pill>
        <span style={{ flex: 1 }} />
        <Pill tone={pos.net_qty > 0 ? T.green : pos.net_qty < 0 ? T.red : T.dim}>
          position {qty(pos.net_qty)} @ {n2(pos.avg_cost)}
        </Pill>
        <Pill tone={T.dim}>fills {vs.fills ?? 0}</Pill>
        <Pill tone={T.dim}>volume {qty(vs.volume)}</Pill>
        <button onClick={boot} style={{ background: T.panel2, color: T.dim, border: `1px solid ${T.line}`, borderRadius: 6, padding: "3px 10px", fontSize: 11, cursor: "pointer" }}>
          New session
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(280px,1fr) minmax(280px,1fr)", gap: 13, marginBottom: 13 }}>
        <Panel title="Order book" sub="Venue ladder, price-time priority">
          <Ladder book={st.book} onPick={(p) => setPrice(p)} />
        </Panel>
        <Panel title="Order ticket" sub="Sends a real NewOrderSingle over the session">
          <Ticket onSend={send} busy={busy} price={price} setPrice={setPrice} />
        </Panel>
      </div>

      <div style={{ marginBottom: 13 }}>
        <Panel title="Blotter"
               sub="Rebuilt from the ExecutionReports that came back — not from what the ticket hoped would happen"
               right={<Pill tone={T.dim}>{st.blotter.length} orders</Pill>}>
          <Blotter rows={st.blotter} onCancel={cancel} selected={selected} onSelect={setSelected} />
        </Panel>
      </div>

      <Panel
        title="FIX monitor"
        sub={selected ? `Filtered to ClOrdID ${selected} — click another blotter row to change` : "Every message, both directions. SOH rendered as |"}
        right={selected && <button onClick={() => setSelected(null)} style={{ background: "none", border: `1px solid ${T.line}`, color: T.dim, borderRadius: 5, padding: "2px 9px", fontSize: 10.5, cursor: "pointer" }}>show all</button>}
      >
        <FixMonitor wire={st.wire_log} filterId={selected} />
      </Panel>
    </div>
  );
}
