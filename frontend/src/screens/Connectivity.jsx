/* Connectivity.jsx — drive the C++ order path from the browser.
 *
 * Four panels, each hitting /api/connectivity and showing what the C++ core
 * actually returned: the L2 book, the market-data quality gate, the pre-trade
 * risk gate and the FIX parser.
 *
 * The screen states plainly which implementation served each response (cpp or
 * python) and that the venue is simulated. Both are read from the API rather
 * than hardcoded here, so the label cannot drift from the truth.
 */
import React, { useEffect, useState } from "react";
import { T, MONO } from "../theme.js";

const post = (u, body) =>
  fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (r) => ({ ok: r.ok, status: r.status, data: await r.json() }));

function Box({ title, sub, children, right }) {
  return (
    <section style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 9, marginBottom: 14, overflow: "hidden" }}>
      <header style={{ padding: "11px 14px", borderBottom: `1px solid ${T.lineSoft}`, display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>{title}</div>
          {sub && <div style={{ fontSize: 11, color: T.dim, marginTop: 3, lineHeight: 1.5 }}>{sub}</div>}
        </div>
        {right}
      </header>
      <div style={{ padding: 14 }}>{children}</div>
    </section>
  );
}

function Pill({ children, tone = T.dim }) {
  return <span style={{ padding: "2px 8px", borderRadius: 20, fontSize: 9.5, letterSpacing: 0.7, color: tone, border: `1px solid ${tone}`, fontFamily: MONO, textTransform: "uppercase", whiteSpace: "nowrap" }}>{children}</span>;
}

const inputStyle = { background: T.panel2, border: `1px solid ${T.line}`, color: T.text, borderRadius: 6, padding: "6px 9px", fontFamily: MONO, fontSize: 12, width: "100%" };
const btnStyle = { background: T.teal, color: T.bg, border: "none", borderRadius: 6, padding: "7px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer" };
const labelStyle = { color: T.faint, fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase", display: "block", marginBottom: 4 };

function Field({ label, ...rest }) {
  return <label style={{ display: "block" }}><span style={labelStyle}>{label}</span><input style={inputStyle} {...rest} /></label>;
}

/* ------------------------------------------------------------------ status */
function StatusStrip({ status }) {
  if (!status) return null;
  const cpp = status.cpp_extension_built;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 14 }}>
      <Pill tone={cpp ? T.teal : T.amber}>impl: {status.impl}</Pill>
      {Object.entries(status.components).map(([k, v]) => (
        <Pill key={k} tone={v === "cpp" ? T.teal : v === "python" ? T.blue : T.red}>
          {k.replace(/_/g, " ")}: {v}
        </Pill>
      ))}
      <Pill tone={T.amber}>simulated</Pill>
    </div>
  );
}

/* -------------------------------------------------------------- order book */
function BookPanel() {
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    // A deliberately awkward stream: an out-of-range price that must be
    // rejected and counted rather than clamped or crashed.
    const updates = [];
    for (let i = 0; i < 12; i++) {
      updates.push({ side: 0, price_ticks: 9990 - i * 3, qty: 100 + i * 40 });
      updates.push({ side: 1, price_ticks: 10010 + i * 3, qty: 120 + i * 35 });
    }
    updates.push({ side: 0, price_ticks: 999999, qty: 50 });
    const r = await post("/api/connectivity/book", { floor_ticks: 1, n_ticks: 50000, depth_levels: 8, updates });
    setRes(r.data); setBusy(false);
  };
  useEffect(() => { run(); }, []);

  const rows = Math.max(res?.bids?.length || 0, res?.asks?.length || 0);
  return (
    <Box
      title="L2 order book"
      sub="Integer ticks, flat-array ladder, cached best with lazy repair. Out-of-range prices are rejected and counted, never clamped."
      right={<button style={btnStyle} onClick={run} disabled={busy}>{busy ? "…" : "Rebuild"}</button>}
    >
      {res && (
        <>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
            <Pill tone={T.teal}>applied {res.applied}</Pill>
            <Pill tone={res.rejected ? T.amber : T.dim}>rejected {res.rejected}</Pill>
            <Pill tone={res.is_crossed ? T.red : T.teal}>{res.is_crossed ? "CROSSED" : "uncrossed"}</Pill>
            <Pill>spread {res.spread_ticks}t</Pill>
            <Pill>micro {res.micro_price_ticks?.toFixed(3)}</Pill>
            <Pill tone={res.imbalance > 0 ? T.green : res.imbalance < 0 ? T.red : T.dim}>
              imbalance {res.imbalance?.toFixed(3)}
            </Pill>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
            <thead><tr>
              {["Bid qty", "Bid", "Ask", "Ask qty"].map((h, i) => (
                <th key={h} style={{ padding: "6px 10px", color: T.faint, fontSize: 9.5, letterSpacing: 0.9,
                  textTransform: "uppercase", textAlign: i < 2 ? "right" : "left", borderBottom: `1px solid ${T.line}` }}>{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {Array.from({ length: rows }).map((_, i) => {
                const b = res.bids[i], a = res.asks[i];
                return (
                  <tr key={i}>
                    <td style={{ padding: "5px 10px", textAlign: "right", color: T.dim, borderBottom: `1px solid ${T.lineSoft}` }}>{b?.qty ?? ""}</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", color: T.green, borderBottom: `1px solid ${T.lineSoft}` }}>{b?.price_ticks ?? ""}</td>
                    <td style={{ padding: "5px 10px", color: T.red, borderBottom: `1px solid ${T.lineSoft}` }}>{a?.price_ticks ?? ""}</td>
                    <td style={{ padding: "5px 10px", color: T.dim, borderBottom: `1px solid ${T.lineSoft}` }}>{a?.qty ?? ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {res.throughput_note && (
            <p style={{ color: T.faint, fontSize: 10.5, lineHeight: 1.6, marginTop: 10, marginBottom: 0 }}>
              {res.updates_per_sec?.toLocaleString()} updates/s across the HTTP + FFI boundary. {res.throughput_note}
            </p>
          )}
        </>
      )}
    </Box>
  );
}

/* ------------------------------------------------------------- feed quality */
function FeedPanel() {
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    // One stream carrying all four fault classes at once, which is the point:
    // they look alike and are diagnosed differently.
    const ticks = [];
    let seq = 0;
    for (let i = 0; i < 120; i++) ticks.push({ seq: ++seq, ts_ns: seq * 1e9, price: 100 + 0.01 * (i % 7) });
    ticks.push({ seq, ts_ns: (seq + 1) * 1e9, price: 100.02 });          // duplicate
    seq += 4;
    ticks.push({ seq, ts_ns: (seq + 1) * 1e9, price: 100.03 });          // gap of 3
    ticks.push({ seq: seq - 2, ts_ns: (seq + 2) * 1e9, price: 100.01 }); // reorder
    ticks.push({ seq: ++seq, ts_ns: (seq + 3) * 1e9, price: 9999 });     // out of band
    const r = await post("/api/connectivity/feed/validate", { ticks, price_min: 50, price_max: 200, min_samples: 20 });
    setRes(r.data); setBusy(false);
  };
  useEffect(() => { run(); }, []);

  const tone = { accepted: T.teal, sequence_gap: T.red, duplicate: T.amber, reordered: T.amber,
                 price_out_of_band: T.red, price_outlier: T.violet, bad_value: T.red,
                 stale_timestamp: T.amber, too_old: T.amber };
  return (
    <Box
      title="Market-data quality gate"
      sub="A gap, a duplicate and a reorder all look like a bad sequence number and mean three different things. Each is diagnosed separately and counted."
      right={<button style={btnStyle} onClick={run} disabled={busy}>{busy ? "…" : "Replay stream"}</button>}
    >
      {res && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 8, marginBottom: 12 }}>
            {Object.entries(res.verdicts).filter(([, v]) => v > 0).map(([k, v]) => (
              <div key={k} style={{ background: T.panel2, border: `1px solid ${tone[k] || T.line}`, borderRadius: 7, padding: "8px 11px" }}>
                <div style={{ color: T.faint, fontSize: 9.5, letterSpacing: 0.8, textTransform: "uppercase" }}>{k.replace(/_/g, " ")}</div>
                <div style={{ fontFamily: MONO, fontSize: 17, color: tone[k] || T.text, marginTop: 2 }}>{v}</div>
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Pill tone={res.reject_rate > 0 ? T.amber : T.teal}>reject rate {(res.reject_rate * 100).toFixed(2)}%</Pill>
            <Pill tone={res.needs_recovery ? T.red : T.teal}>
              {res.needs_recovery ? "book needs recovery" : "book trusted"}
            </Pill>
            <Pill>{res.ticks?.toLocaleString()} ticks</Pill>
            <Pill>{res.ticks_per_sec?.toLocaleString()} ticks/s</Pill>
            <Pill tone={T.dim}>messages lost {res.counters?.gap_messages_lost}</Pill>
          </div>
          {res.needs_recovery && (
            <p style={{ color: T.red, fontSize: 11, lineHeight: 1.6, marginTop: 10, marginBottom: 0 }}>
              A gap was detected, so the book is flagged untrustworthy until a snapshot
              or resend rebuilds it. Application processing is suspended rather than
              continuing out of order.
            </p>
          )}
        </>
      )}
    </Box>
  );
}

/* --------------------------------------------------------- pre-trade risk */
const SCENARIOS = [
  { label: "Normal order",        body: { side: 0, qty: 500,   price: 100.10, reference_price: 100 } },
  { label: "Fat finger: qty ×100", body: { side: 0, qty: 50000, price: 100.10, reference_price: 100 } },
  { label: "Fat finger: price +30%", body: { side: 0, qty: 100, price: 130.0, reference_price: 100 } },
  { label: "Stale market data",   body: { side: 0, qty: 100, price: 100.1, reference_price: 0, reference_ok: false } },
  { label: "Short limit breach",  body: { side: 1, qty: 900, price: 100.0, reference_price: 100, current_position: 0,
                                          limits: { max_order_qty: 10000, max_order_notional: 1e7, collar_bps: 500,
                                                    max_long_position: 50000, max_short_position: 500, contract_size: 1 } } },
  { label: "Kill switch engaged", body: { side: 0, qty: 1, price: 100.1, reference_price: 100, kill_switch: true } },
];

function PreTradePanel() {
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    const out = [];
    for (const s of SCENARIOS) {
      const r = await post("/api/connectivity/pretrade", s.body);
      out.push({ label: s.label, ...(r.ok ? r.data : { decision: `unavailable (${r.status})`, accepted: false }) });
    }
    setRows(out); setBusy(false);
  };
  useEffect(() => { run(); }, []);

  return (
    <Box
      title="Pre-trade risk gate"
      sub="Kill switch first and unconditional, then wellformedness, static limits, price collar, position limits. A stale feed refuses rather than waving the order through unchecked."
      right={<button style={btnStyle} onClick={run} disabled={busy}>{busy ? "…" : "Re-run"}</button>}
    >
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>
          {["Scenario", "Decision", "Notional", "Deviation", "Resulting pos"].map((h, i) => (
            <th key={h} style={{ padding: "6px 10px", textAlign: i === 0 ? "left" : "right", color: T.faint,
              fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase", borderBottom: `1px solid ${T.line}` }}>{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td style={{ padding: "7px 10px", borderBottom: `1px solid ${T.lineSoft}` }}>{r.label}</td>
              <td style={{ padding: "7px 10px", textAlign: "right", fontFamily: MONO,
                color: r.accepted ? T.teal : T.red, borderBottom: `1px solid ${T.lineSoft}` }}>{r.decision}</td>
              <td style={{ padding: "7px 10px", textAlign: "right", fontFamily: MONO, color: T.dim, borderBottom: `1px solid ${T.lineSoft}` }}>
                {r.notional != null ? r.notional.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "—"}
              </td>
              <td style={{ padding: "7px 10px", textAlign: "right", fontFamily: MONO, color: T.dim, borderBottom: `1px solid ${T.lineSoft}` }}>
                {r.deviation_bps != null ? `${r.deviation_bps.toFixed(0)} bps` : "—"}
              </td>
              <td style={{ padding: "7px 10px", textAlign: "right", fontFamily: MONO, color: T.dim, borderBottom: `1px solid ${T.lineSoft}` }}>
                {r.resulting_position ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows[0]?.elapsed_note && (
        <p style={{ color: T.faint, fontSize: 10.5, lineHeight: 1.6, marginTop: 10, marginBottom: 0 }}>
          <b style={{ color: T.dim }}>On timing:</b> {rows[0].elapsed_note}
        </p>
      )}
    </Box>
  );
}

/* ----------------------------------------------------------------- FIX */
const SAMPLE = "8=FIX.4.2|9=65|35=A|49=SERVER|56=CLIENT|34=177|52=20090107-18:15:16|98=0|108=30|10=062|";

function FixPanel() {
  const [raw, setRaw] = useState(SAMPLE);
  const [validate, setValidate] = useState(true);
  const [res, setRes] = useState(null);

  const run = async (r = raw, v = validate) => {
    const q = new URLSearchParams({ raw: r, validate: String(v) });
    const resp = await fetch(`/api/connectivity/fix/parse?${q}`);
    setRes(resp.ok ? await resp.json() : { ok: false, error: `HTTP ${resp.status}` });
  };
  useEffect(() => { run(); }, []);

  const corrupt = () => {
    const bad = raw.replace("SERVER", "XERVER");
    setRaw(bad); run(bad, validate);
  };

  return (
    <Box
      title="FIX parser"
      sub="This sample is the canonical Logon from the FIX 4.2 documentation — its BodyLength (65) and CheckSum (062) were computed by someone else, so parsing it is a check against the spec rather than against our own encoder."
    >
      <textarea
        value={raw} onChange={(e) => setRaw(e.target.value)} rows={3}
        style={{ ...inputStyle, resize: "vertical", lineHeight: 1.6, marginBottom: 9 }}
      />
      <div style={{ display: "flex", gap: 9, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <button style={btnStyle} onClick={() => run()}>Parse</button>
        <button style={{ ...btnStyle, background: T.panel2, color: T.amber, border: `1px solid ${T.amber}` }} onClick={corrupt}>
          Corrupt one byte
        </button>
        <label style={{ display: "flex", gap: 6, alignItems: "center", color: T.dim, fontSize: 11.5 }}>
          <input type="checkbox" checked={validate} onChange={(e) => { setValidate(e.target.checked); run(raw, e.target.checked); }} />
          validate BodyLength + CheckSum
        </label>
      </div>
      {res && (
        <>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
            <Pill tone={res.ok ? T.teal : T.red}>{res.ok ? "valid" : res.error}</Pill>
            {res.ok && <><Pill>type {res.msg_type}</Pill><Pill>seq {res.seq_num ?? "—"}</Pill><Pill>{res.field_count} fields</Pill></>}
          </div>
          {res.ok && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(160px,1fr))", gap: 1, background: T.lineSoft, border: `1px solid ${T.line}`, borderRadius: 7, overflow: "hidden" }}>
              {Object.entries(res.fields).map(([tag, val]) => (
                <div key={tag} style={{ background: T.panel2, padding: "6px 10px" }}>
                  <span style={{ color: T.blue, fontFamily: MONO, fontSize: 11 }}>{tag}</span>
                  <span style={{ color: T.faint, margin: "0 5px" }}>=</span>
                  <span style={{ color: T.text, fontFamily: MONO, fontSize: 11.5 }}>{val}</span>
                </div>
              ))}
            </div>
          )}
          {!res.ok && res.error === "checksum_mismatch" && (
            <p style={{ color: T.dim, fontSize: 11, lineHeight: 1.6, marginTop: 4, marginBottom: 0 }}>
              A single changed byte is caught by the checksum. Untick validation above
              and the same bytes parse — that path is only legitimate when replaying a
              log whose framing is already trusted.
            </p>
          )}
        </>
      )}
    </Box>
  );
}

/* ------------------------------------------------------------------ screen */
export default function Connectivity() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    fetch("/api/connectivity/status")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setStatus).catch((e) => setErr(e.message));
  }, []);

  if (err) return <div style={{ color: T.red, padding: 16, fontSize: 12.5 }}>Connectivity API unavailable: {err}</div>;

  const degraded = status && !status.cpp_extension_built;
  return (
    <div>
      <StatusStrip status={status} />
      {degraded && (
        <div style={{ background: T.panel, border: `1px solid ${T.amber}`, borderRadius: 9, padding: "11px 14px", marginBottom: 14, color: T.amber, fontSize: 11.5, lineHeight: 1.6 }}>
          The C++ extension is not built, so the book and quality gate are running the
          Python fallback and the matching engine, pre-trade gate and FIX parser are
          unavailable. That is deliberate: a risk control silently degrading to an
          unverified second implementation is worse than a clear error.
          <code style={{ display: "block", marginTop: 7, fontFamily: MONO, color: T.teal }}>./scripts/build_bindings.sh</code>
        </div>
      )}
      <BookPanel />
      <FeedPanel />
      <PreTradePanel />
      <FixPanel />
    </div>
  );
}
