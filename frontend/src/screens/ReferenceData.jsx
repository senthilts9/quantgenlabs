/* ReferenceData.jsx — security master, calendars, corporate actions, recon, batch.
 *
 * Five panels over /api/refdata. The screen is built around the three questions
 * this layer exists to answer correctly, and each panel is arranged so a wrong
 * answer would be VISIBLE rather than plausible:
 *
 *   Symbology     the as-of date is a first-class input, not a hidden default.
 *                 Changing it and watching the instrument change is the whole
 *                 demonstration, so the date sits next to the symbol.
 *   Calendar      shows T+2 and the next trading day together, because the gap
 *                 between them over a holiday is the bug being prevented.
 *   Actions       shows book value before and after side by side. A split that
 *                 changed the portfolio's worth is then obvious on the screen
 *                 instead of buried in a number nobody checks.
 *   Recon         every break carries its classification, severity and the
 *                 suggested next step, because "they differ" is not actionable.
 *   Batch         runs SOD/EOD and then runs it AGAIN, to show the guard refuse.
 *
 * Nothing here is a price. The instruments and corporate actions come from the
 * fixture security master and every response says so.
 */
import React, { useEffect, useState } from "react";
import { T, MONO } from "../theme.js";

const get = (u) => fetch(u).then(async (r) => ({ ok: r.ok, status: r.status, data: await r.json() }));
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
const btnGhost = { ...btnStyle, background: "transparent", color: T.teal, border: `1px solid ${T.teal}` };
const labelStyle = { color: T.faint, fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase", display: "block", marginBottom: 4 };
const row = { display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 };
const mono = { fontFamily: MONO, fontSize: 12 };
const note = { fontSize: 10.5, color: T.faint, lineHeight: 1.6, marginTop: 10, fontStyle: "italic" };

function Field({ label, width = 130, ...rest }) {
  return (
    <label style={{ display: "block", width }}>
      <span style={labelStyle}>{label}</span>
      <input style={inputStyle} {...rest} />
    </label>
  );
}

function KV({ k, v, tone }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 14, padding: "4px 0", borderBottom: `1px solid ${T.lineSoft}` }}>
      <span style={{ fontSize: 11, color: T.dim }}>{k}</span>
      <span style={{ ...mono, color: tone || T.text, textAlign: "right" }}>{v}</span>
    </div>
  );
}

const SEV_TONE = { critical: T.red, high: T.amber, medium: T.blue, info: T.dim };

/* ------------------------------------------------------------- symbology */
function SymbologyPanel() {
  const [symbol, setSymbol] = useState("ZZZ");
  const [on, setOn] = useState("2000-01-03");
  const [res, setRes] = useState(null);
  const [lineage, setLineage] = useState(null);

  const run = async (sym = symbol, date = on) => {
    const r = await get(`/api/refdata/resolve?symbol=${encodeURIComponent(sym)}&on=${date}`);
    setRes(r);
    if (r.ok) {
      const l = await get(`/api/refdata/lineage/${r.data.instrument_id}?id_type=ticker`);
      setLineage(l.ok ? l.data : null);
    } else setLineage(null);
  };
  useEffect(() => { run(); }, []);

  // Each preset is a case where resolving without a date gives a wrong answer.
  const presets = [
    ["ZZZ", "2000-01-03", "reused ticker, first owner"],
    ["ZZZ", "2020-01-03", "same string, different company"],
    ["ZZZ", "2011-06-01", "between owners — resolves to nobody"],
    ["FB", "2015-01-02", "ticker before the change"],
    ["FB", "2024-01-02", "same ticker, after — not this company"],
    ["META", "2024-01-02", "ticker after the change"],
  ];

  return (
    <Box title="Symbology — resolution is as-of a date"
         sub="A ticker is a display label that exchanges reuse, not an identifier. The date below is required by the API, not defaulted to today: resolving without one is the wrong answer delivered confidently."
         right={<Pill tone={T.amber}>fixture master</Pill>}>
      <div style={row}>
        <Field label="Symbol" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
        <Field label="As of" type="date" value={on} onChange={(e) => setOn(e.target.value)} width={150} />
        <button style={btnStyle} onClick={() => run()}>Resolve</button>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {presets.map(([s, d, why]) => (
          <button key={s + d} title={why} style={{ ...btnGhost, padding: "4px 9px", fontSize: 10.5, fontWeight: 400 }}
                  onClick={() => { setSymbol(s); setOn(d); run(s, d); }}>
            {s} @ {d.slice(0, 4)}
          </button>
        ))}
      </div>

      {res && !res.ok && (
        <div style={{ border: `1px solid ${T.amber}`, borderRadius: 7, padding: 11 }}>
          <Pill tone={T.amber}>{res.status} — no resolution</Pill>
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8, lineHeight: 1.6 }}>{res.data.detail}</div>
          <div style={note}>Refusing is the correct answer here. Returning either
            the previous or the next owner of this ticker would key data onto the
            wrong company with nothing to flag it.</div>
        </div>
      )}

      {res && res.ok && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(250px,1fr))", gap: 14 }}>
          <div>
            <KV k="instrument_id" v={res.data.instrument_id} tone={T.teal} />
            <KV k="name" v={res.data.name} />
            <KV k="asset class" v={res.data.asset_class} />
            <KV k="exchange / ccy" v={`${res.data.exchange} / ${res.data.currency}`} />
            <KV k="tradable on this date" v={String(res.data.tradable_on)}
                tone={res.data.tradable_on ? T.teal : T.amber} />
          </div>
          <div>
            <div style={{ ...labelStyle, marginBottom: 6 }}>Identifiers on {res.data.as_of}</div>
            {Object.entries(res.data.identifiers).map(([k, v]) => <KV key={k} k={k} v={v} />)}
          </div>
          {lineage && lineage.history.length > 1 && (
            <div>
              <div style={{ ...labelStyle, marginBottom: 6 }}>Ticker lineage</div>
              {lineage.history.map((h, i) => (
                <KV key={i} k={`${h.valid_from} → ${h.valid_to || "current"}`} v={h.value}
                    tone={h.value === res.data.identifiers.ticker ? T.teal : T.dim} />
              ))}
              <div style={note}>Windows are half-open: the instrument does not
                resolve to both the old and new ticker on the change date.</div>
            </div>
          )}
        </div>
      )}
    </Box>
  );
}

/* -------------------------------------------------------------- calendar */
function CalendarPanel() {
  const [mic, setMic] = useState("XNYS");
  const [on, setOn] = useState("2025-11-25");
  const [res, setRes] = useState(null);

  const run = async (m = mic, d = on) => setRes((await get(`/api/refdata/calendar/${m}?on=${d}`)).data);
  useEffect(() => { run(); }, []);

  const presets = [
    ["XNYS", "2025-11-25", "T+2 steps over Thanksgiving"],
    ["XNYS", "2025-11-28", "half-day: open, early close"],
    ["XNYS", "2025-05-26", "Memorial Day: closed"],
    ["XNYS", "2025-01-02", "T+2 over a weekend"],
    ["CRYPTO", "2025-12-25", "24/7: open on Christmas"],
  ];

  const settleGap = res && res.settlement_t_plus_2
    ? Math.round((new Date(res.settlement_t_plus_2) - new Date(res.date)) / 86400000) : null;

  return (
    <Box title="Trading calendar — a trading day is not a calendar day"
         sub="T+2 is two trading days forward on this calendar, not two calendar days. Watch the gap below: over a weekend or a holiday, settlement lands more than two days out."
         right={res && <Pill tone={res.is_trading_day ? T.teal : T.red}>{res.is_trading_day ? (res.is_half_day ? "half day" : "open") : "closed"}</Pill>}>
      <div style={row}>
        <label style={{ display: "block", width: 130 }}>
          <span style={labelStyle}>Venue</span>
          <select style={inputStyle} value={mic} onChange={(e) => { setMic(e.target.value); run(e.target.value, on); }}>
            <option value="XNYS">XNYS</option><option value="XNAS">XNAS</option><option value="CRYPTO">CRYPTO</option>
          </select>
        </label>
        <Field label="Date" type="date" value={on} onChange={(e) => setOn(e.target.value)} width={150} />
        <button style={btnStyle} onClick={() => run()}>Check</button>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {presets.map(([m, d, why]) => (
          <button key={m + d} title={why} style={{ ...btnGhost, padding: "4px 9px", fontSize: 10.5, fontWeight: 400 }}
                  onClick={() => { setMic(m); setOn(d); run(m, d); }}>{why}</button>
        ))}
      </div>

      {res && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(230px,1fr))", gap: 14 }}>
          <div>
            <KV k="trading day" v={String(res.is_trading_day)} tone={res.is_trading_day ? T.teal : T.red} />
            <KV k="half day" v={String(res.is_half_day)} tone={res.is_half_day ? T.amber : T.dim} />
            <KV k="session" v={res.is_trading_day ? `${res.open_time} – ${res.close_time}` : "—"}
                tone={res.is_half_day ? T.amber : T.text} />
          </div>
          <div>
            <KV k="previous trading day" v={res.previous_trading_day} />
            <KV k="next trading day" v={res.next_trading_day} />
            <KV k="settlement T+2" v={res.settlement_t_plus_2 || "—"} tone={T.teal} />
          </div>
          <div>
            <div style={{ ...labelStyle, marginBottom: 6 }}>Calendar days to settlement</div>
            <div style={{ fontFamily: MONO, fontSize: 30, color: settleGap > 2 ? T.amber : T.teal, lineHeight: 1 }}>
              {settleGap == null ? "—" : `+${settleGap}`}
            </div>
            <div style={note}>
              {settleGap == null ? "Closed: nothing settles from a day with no session."
                : settleGap > 2
                  ? `T+2 is ${settleGap} calendar days here. Adding timedelta(2) would land on a closed day and the failure would surface days later in an operations queue.`
                  : "No weekend or holiday in the way, so T+2 and +2 days agree — which is exactly why the bug survives in code that assumes they always do."}
            </div>
          </div>
        </div>
      )}
    </Box>
  );
}

/* ------------------------------------------------------ corporate actions */
function CorporateActionsPanel() {
  const [actions, setActions] = useState([]);
  const [sel, setSel] = useState(null);
  const [qty, setQty] = useState("100");
  const [cost, setCost] = useState("400");
  const [adj, setAdj] = useState(null);

  useEffect(() => {
    get("/api/refdata/corporate-actions").then((r) => {
      if (!r.ok) return;
      setActions(r.data.actions);
      const split = r.data.actions.find((a) => a.type === "split");
      if (split) setSel(split);
    });
  }, []);

  const apply = async (a = sel) => {
    if (!a) return;
    const r = await post("/api/refdata/adjust-position", {
      instrument_id: a.instrument_id, qty: Number(qty), avg_cost: Number(cost), ex_date: a.ex_date });
    setAdj(r.ok ? r.data : null);
  };
  useEffect(() => { if (sel) apply(sel); }, [sel]);

  return (
    <Box title="Corporate actions — adjust positions and history together, or neither"
         sub="A split that adjusts the position but not the price history shows a return on the ex-date that never happened, and every volatility, VaR and beta over that window is wrong. Book value before and after are shown side by side so an inverted ratio is visible rather than plausible."
         right={<Pill tone={T.dim}>{actions.length} records</Pill>}>
      <div style={row}>
        <label style={{ display: "block", width: 290 }}>
          <span style={labelStyle}>Action</span>
          <select style={inputStyle} value={sel ? `${sel.instrument_id}|${sel.ex_date}` : ""}
                  onChange={(e) => setSel(actions.find((a) => `${a.instrument_id}|${a.ex_date}` === e.target.value))}>
            {actions.map((a) => (
              <option key={`${a.instrument_id}|${a.ex_date}`} value={`${a.instrument_id}|${a.ex_date}`}>
                {a.instrument_id.replace("QGL-", "")} · {a.ex_date} · {a.type === "split" ? `${a.ratio}-for-1` : `${a.cash_amount}/sh`}
              </option>
            ))}
          </select>
        </label>
        <Field label="Quantity" value={qty} onChange={(e) => setQty(e.target.value)} width={110} />
        <Field label="Avg cost" value={cost} onChange={(e) => setCost(e.target.value)} width={110} />
        <button style={btnStyle} onClick={() => apply()}>Apply</button>
      </div>

      {adj && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))", gap: 14 }}>
            <div>
              <div style={{ ...labelStyle, marginBottom: 6 }}>Before ex-date</div>
              <KV k="quantity" v={adj.qty_before} />
              <KV k="avg cost" v={adj.cost_before.toFixed(4)} />
              <KV k="book value" v={adj.book_value_before.toLocaleString(undefined, { minimumFractionDigits: 2 })} tone={T.teal} />
            </div>
            <div>
              <div style={{ ...labelStyle, marginBottom: 6 }}>On ex-date</div>
              <KV k="quantity" v={adj.qty_after} tone={adj.qty_after !== adj.qty_before ? T.amber : T.text} />
              <KV k="avg cost" v={adj.cost_after.toFixed(4)} tone={adj.cost_after !== adj.cost_before ? T.amber : T.text} />
              <KV k="book value" v={adj.book_value_after.toLocaleString(undefined, { minimumFractionDigits: 2 })} tone={T.teal} />
            </div>
            <div>
              <div style={{ ...labelStyle, marginBottom: 6 }}>Invariant</div>
              <div style={{ fontFamily: MONO, fontSize: 13, color: adj.market_value_preserved ? T.teal : T.red, marginBottom: 6 }}>
                {adj.market_value_preserved ? "✓ market value preserved" : "✗ MARKET VALUE CHANGED"}
              </div>
              {adj.cash_entitlement > 0 && <KV k="cash entitlement" v={adj.cash_entitlement.toFixed(2)} tone={T.amber} />}
              <div style={note}>{adj.market_value_preserved
                ? "A split creates nothing, so the position is worth the same either side of it. This check is what catches an inverted ratio immediately."
                : "The ratio has been applied in the same direction to both quantity and cost. This is the bug the check exists for."}</div>
            </div>
          </div>
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 12, lineHeight: 1.6, borderTop: `1px solid ${T.lineSoft}`, paddingTop: 10 }}>
            {adj.explanation}
          </div>
        </>
      )}
    </Box>
  );
}

/* -------------------------------------------------------- reconciliation */
function ReconPanel() {
  // Seeded with one of each break class, so the classification is the thing on
  // screen rather than a single number that differs.
  const [ours, setOurs] = useState('{"AAPL": 1000, "MSFT": 500, "NVDA": 100, "TSLA": 0}');
  const [theirs, setTheirs] = useState('{"AAPL": 1000, "MSFT": -500, "NVDA": 150, "SPY": 300}');
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);

  const run = async () => {
    let o, t;
    try { o = JSON.parse(ours); t = JSON.parse(theirs); setErr(null); }
    catch { setErr("Both sides must be valid JSON objects of symbol → quantity."); return; }
    const r = await post("/api/refdata/reconcile/positions", { ours: o, theirs: t });
    setRes(r.ok ? r.data : null);
  };
  useEffect(() => { run(); }, []);

  return (
    <Box title="Reconciliation — the classified cause, not a boolean"
         sub="A report that says two numbers disagree leaves someone to work out why. Each break below carries its class, its severity and the next step, because 'they differ' is not actionable and 'AAPL is 1000 here and 1150 there, probably a partial applied twice' is."
         right={res && <Pill tone={res.clean ? T.teal : T.red}>{res.clean ? "clean" : `${res.break_count} breaks`}</Pill>}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 12, marginBottom: 12 }}>
        <label><span style={labelStyle}>Our positions</span>
          <textarea style={{ ...inputStyle, minHeight: 58, resize: "vertical" }} value={ours} onChange={(e) => setOurs(e.target.value)} /></label>
        <label><span style={labelStyle}>Their record</span>
          <textarea style={{ ...inputStyle, minHeight: 58, resize: "vertical" }} value={theirs} onChange={(e) => setTheirs(e.target.value)} /></label>
      </div>
      <button style={btnStyle} onClick={run}>Reconcile</button>
      {err && <div style={{ color: T.amber, fontSize: 11.5, marginTop: 10 }}>{err}</div>}

      {res && (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
            <Pill tone={T.teal}>{res.matched} matched</Pill>
            {Object.entries(res.by_severity).filter(([, n]) => n > 0).map(([s, n]) => (
              <Pill key={s} tone={SEV_TONE[s]}>{n} {s}</Pill>
            ))}
          </div>
          {res.breaks.length === 0 && <div style={{ fontSize: 12, color: T.teal }}>{res.note}</div>}
          {res.breaks.map((b, i) => (
            <div key={i} style={{ border: `1px solid ${T.lineSoft}`, borderLeft: `3px solid ${SEV_TONE[b.break_type === "timing" ? "info" : b.severity]}`, borderRadius: 6, padding: "9px 12px", marginBottom: 8 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
                <span style={{ ...mono, fontWeight: 600 }}>{b.key}</span>
                <Pill tone={SEV_TONE[b.severity]}>{b.break_type.replace(/_/g, " ")}</Pill>
                <span style={{ ...mono, fontSize: 11, color: T.dim }}>
                  ours {b.ours === null ? "—" : b.ours} · theirs {b.theirs === null ? "—" : b.theirs} · Δ {b.difference}
                </span>
              </div>
              <div style={{ fontSize: 11.5, color: T.dim, lineHeight: 1.6 }}>{b.explanation}</div>
              <div style={{ fontSize: 11, color: T.teal, marginTop: 5 }}>→ {b.suggested_action}</div>
            </div>
          ))}
          <div style={note}>{res.note}</div>
        </div>
      )}
    </Box>
  );
}

/* ------------------------------------------------------------ batch cycle */
function BatchPanel() {
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);

  // The sequence deliberately ends by running EOD a second time, so the
  // idempotency guard refusing is part of what the panel shows.
  const run = async () => {
    setBusy(true); setLog([]);
    const push = (entry) => setLog((l) => [...l, entry]);

    const sod = await post("/api/refdata/batch/sod", {
      business_date: "2024-06-10",
      positions: [{ instrument_id: "QGL-EQ-NVDA", qty: 100, avg_cost: 400, mark: 1200 }],
    });
    push({ label: "SOD 2024-06-10 — NVDA 10-for-1 ex-date", r: sod });

    const sid = sod.ok ? sod.data.session_id : null;
    const eodBody = {
      business_date: "2025-05-23", session_id: sid,
      positions: [{ instrument_id: "QGL-EQ-AAPL", qty: 1000, avg_cost: 150 }],
      marks: { "QGL-EQ-AAPL": 200 },
      external_positions: { "QGL-EQ-AAPL": 1000 },
    };
    push({ label: "EOD 2025-05-23 — mark, value, reconcile, roll", r: await post("/api/refdata/batch/eod", eodBody) });
    push({ label: "EOD 2025-05-23 again — must be refused", r: await post("/api/refdata/batch/eod", eodBody), expectFail: true });
    setBusy(false);
  };

  return (
    <Box title="Batch cycle — SOD and EOD, where the order of steps is the correctness"
         sub="Corporate actions before marking, marking before reconciling, roll last. The run below ends by triggering EOD a second time: a repeat is refused rather than redone, because a double-applied 10-for-1 gives a 100x position that looks like a trading error."
         right={<Pill tone={T.amber}>simulated</Pill>}>
      <button style={btnStyle} onClick={run} disabled={busy}>{busy ? "Running…" : "Run SOD → EOD → EOD again"}</button>

      <div style={{ marginTop: 14 }}>
        {log.map((e, i) => {
          const refused = e.r.status === 409;
          const good = e.expectFail ? refused : e.r.ok && e.r.data.ok;
          return (
            <div key={i} style={{ border: `1px solid ${T.lineSoft}`, borderLeft: `3px solid ${good ? T.teal : T.red}`, borderRadius: 6, padding: "9px 12px", marginBottom: 8 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>{e.label}</span>
                <Pill tone={good ? T.teal : T.red}>{refused ? `${e.r.status} refused` : e.r.data.ok ? "ok" : "not ok"}</Pill>
              </div>
              {refused && <div style={{ fontSize: 11.5, color: T.dim, marginTop: 7, lineHeight: 1.6 }}>{e.r.data.detail}</div>}
              {!refused && e.r.ok && (
                <>
                  <div style={{ marginTop: 8 }}>
                    {e.r.data.steps.map((s, j) => (
                      <div key={j} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "3px 0" }}>
                        <span style={{ color: s.ok ? T.teal : T.red, fontFamily: MONO, fontSize: 11 }}>{s.ok ? "✓" : "✗"}</span>
                        <span style={{ ...mono, fontSize: 11, width: 130, color: T.faint }}>{s.step}</span>
                        <span style={{ fontSize: 11.5, color: T.dim, flex: 1, lineHeight: 1.5 }}>{s.detail}</span>
                      </div>
                    ))}
                  </div>
                  {e.r.data.positions.length > 0 && (
                    <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 9, paddingTop: 8, borderTop: `1px solid ${T.lineSoft}` }}>
                      {e.r.data.positions.map((p, j) => (
                        <span key={j} style={{ ...mono, fontSize: 11, color: T.dim }}>
                          {p.instrument_id.replace("QGL-", "")}: {p.qty} @ {p.avg_cost.toFixed(2)}
                          {p.mark ? ` · mark ${p.mark.toFixed(2)} · mv ${p.market_value.toLocaleString()}` : ""}
                        </span>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>
    </Box>
  );
}

export default function ReferenceData() {
  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 5 }}>Reference Data</div>
        <div style={{ fontSize: 11.5, color: T.dim, lineHeight: 1.65, maxWidth: 780 }}>
          The layer everything else already assumed. Identifiers, calendars and
          adjustments are load-bearing and invisible until they are wrong, and
          when they are wrong the numbers stay plausible — so each panel here is
          arranged around the case where a wrong answer would otherwise look fine.
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
          <Pill tone={T.amber}>fixture security master</Pill>
          <Pill tone={T.dim}>no prices — reference data only</Pill>
          <Pill tone={T.amber}>simulated batch</Pill>
        </div>
      </div>
      <SymbologyPanel />
      <CalendarPanel />
      <CorporateActionsPanel />
      <ReconPanel />
      <BatchPanel />
    </div>
  );
}
