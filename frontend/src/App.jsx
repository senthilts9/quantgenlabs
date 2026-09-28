import React, { useEffect, useMemo, useState } from "react";
import {
  LayoutGrid, Activity, Sigma, TriangleAlert, ShieldCheck, LineChart as LineIcon,
  Layers, Briefcase, Gauge, Coins, History, Database, Bot,
  BookOpen, Search, Command, Circle, ArrowUpRight, ArrowDownRight, Cpu, Zap,
  CheckCircle2, ChevronRight, Router, Split,
} from "lucide-react";
// Phase 1 digital-assets desk. Aliased because this file already defines a
// `CryptoDesk` (the previous perps/AMM screen), which the new workspace
// replaces. Styles are scoped to `.qgl-desk` — see cryptodesk/desk.css.
import { CryptoDesk as DigitalAssetsDesk } from "./cryptodesk/app/CryptoDesk";
import "./cryptodesk/desk.css";
// DEX board screens. Kept out of desk.css because that sheet is generated
// from the Phase 1 source and hand edits are lost on the next regeneration.
import "./cryptodesk/dexboard.css";

/* ================================================================== *
 *  QuantGenLabs — Risk & Quant Console (full shell)
 *  Slate institutional terminal. Colour encodes risk bands, not decor.
 *  IBM Plex Mono for every figure. BSM/binomial computed live; the LSM
 *  American numbers and engine latencies are the real values measured
 *  from the C++ benchmarks in this repo.
 * ================================================================== */

const T = {
  bg: "#0E1420", panel: "#141C2A", panel2: "#101725", line: "#233144",
  lineSoft: "#1A2536", text: "#E6ECF3", dim: "#8A99AE", faint: "#5B6B82",
  teal: "#38BFA6", blue: "#5B9BF0", amber: "#E0A44E", red: "#E5535B",
  green: "#46C08A", violet: "#9B87F5",
};
const MONO = "'IBM Plex Mono',monospace";

/* ---- chart series palette ----------------------------------------------- *
 * Separate from T on purpose. T's colours do double duty as UI accents (pill
 * borders, alert rules, status text) and are stepped for TEXT on this surface,
 * which puts them above the dark-mode chart band (OKLCH L 0.48-0.67) -- as
 * marks they glare. These steps are chosen for the chart surface and validated
 * as a set against it: lightness band, chroma floor, CVD separation
 * (protan/deutan), normal-vision floor, and >= 3:1 contrast vs T.panel.
 *
 * Slots are assigned in fixed order and never cycled. Status colours (T.red,
 * T.green) stay reserved for state and are never used as a series.
 */
const V = {
  s1: "#3987E5",          // blue    -- empirical / observed
  s2: "#D95926",          // orange  -- model / fitted
  s3: "#199E70",          // aqua    -- third series
  s4: "#C98500",          // yellow  -- fourth series
  grid: "#1F2B3D",        // hairline, one step off the surface
  surface: "#141C2A",     // must equal T.panel -- the gap/ring colour
};
// Renders an em dash rather than throwing on a missing value. This is not
// defensive clutter: `fmt` is called from ~100 places, React unmounts the whole
// tree on a render-time throw, and a single absent API field therefore took the
// ENTIRE console to a blank page -- which is exactly what a field-name drift on
// /api/crypto/margin did to the Liquidation Risk screen. One missing number
// should cost one cell, not the application.
const fmt = (n, d = 2) => (n == null || (typeof n === "number" && !Number.isFinite(n)))
  ? "—"
  : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const band = (s) => s >= 75 ? { k: "CRITICAL", c: T.red } : s >= 50 ? { k: "HIGH", c: T.amber }
  : s >= 25 ? { k: "MEDIUM", c: T.blue } : { k: "LOW", c: T.teal };

/* ---- live math ---- */
const pdf = (x) => Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
const cdf = (x) => 0.5 * (1 + erf(x / Math.SQRT2));
function erf(x) { const t = 1 / (1 + 0.3275911 * Math.abs(x));
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return x >= 0 ? y : -y; }
// "1 in N days" for a move of |z| sigma under a normal, two-tailed. The tail
// probability underflows to 0 in double precision past about 8 sigma, so the
// formatted answer saturates rather than printing 1 in Infinity.
function normOdds(z) {
  const p = 2 * (1 - cdf(Math.abs(z)));
  if (!(p > 0)) return "more days than the universe has had";
  const n = 1 / p;
  return n >= 1e9 ? `${(n / 1e9).toPrecision(3)} billion days`
    : n >= 1e6 ? `${(n / 1e6).toPrecision(3)} million days`
    : `${Math.round(n).toLocaleString("en-US")} days`;
}

function bsm(S, K, T_, r, q, s, call) {
  const st = s * Math.sqrt(T_), d1 = (Math.log(S / K) + (r - q + 0.5 * s * s) * T_) / st, d2 = d1 - st;
  const df = Math.exp(-r * T_), dq = Math.exp(-q * T_), sgn = call ? 1 : -1;
  return { price: sgn * (S * dq * cdf(sgn * d1) - K * df * cdf(sgn * d2)),
    delta: sgn * dq * cdf(sgn * d1), gamma: dq * pdf(d1) / (S * st),
    vega: S * dq * pdf(d1) * Math.sqrt(T_) / 100,
    theta: ((-S * dq * pdf(d1) * s / (2 * Math.sqrt(T_))) - sgn * r * K * df * cdf(sgn * d2) + sgn * q * S * dq * cdf(sgn * d1)) / 365,
    rho: sgn * K * T_ * df * cdf(sgn * d2) / 100 };
}
function binomialAmerican(S, K, T_, r, q, s, N, call, american) {
  const dt = T_ / N, u = Math.exp(s * Math.sqrt(dt)), d = 1 / u;
  const p = (Math.exp((r - q) * dt) - d) / (u - d), df = Math.exp(-r * dt);
  const v = Array.from({ length: N + 1 }, (_, i) => Math.max(call ? S * u ** (N - i) * d ** i - K : K - S * u ** (N - i) * d ** i, 0));
  for (let st = N - 1; st >= 0; st--) for (let i = 0; i <= st; i++) {
    v[i] = df * (p * v[i] + (1 - p) * v[i + 1]);
    if (american) { const ST = S * u ** (st - i) * d ** i; v[i] = Math.max(v[i], call ? ST - K : K - ST); }
  } return v[0];
}

/* ---- nav ---- */

// Every digital-asset screen now lives behind one nav entry. The boundary is
// enforced on the API too: /api/oms/blotter and /api/marketdata/board default
// to desk=traditional and exclude crypto, so BTCUSDT no longer appears beside
// EURUSD in a blotter whose marks come from entirely different infrastructure.

// The crypto desk. False removes the nav group, the HANDLED id and every
// dispatch below, so Vite tree-shakes the console out entirely rather than
// shipping a screen nobody can reach.
const CRYPTO_DESK_ENABLED = true;

const NAV = [
  { id: "overview", label: "Overview", icon: LayoutGrid },
  { id: "sor", label: "Smart Order Routing", icon: Router },
  { id: "blotter", label: "Holdings & Blotter", icon: Briefcase },
  { group: "RISK", items: [
    { id: "market-risk", label: "Market Risk", icon: Activity },
    { id: "sensitivity", label: "Factor & Sensitivity", icon: Sigma },
    { id: "stress", label: "Stress & Scenarios", icon: TriangleAlert },
    { id: "limits", label: "Limits & Alerts", icon: ShieldCheck }] },
  { group: "PRICING", items: [
    { id: "pricing", label: "Equity Derivatives", icon: LineIcon },
    { id: "rates", label: "Rates & Fixed Income", icon: Layers }] },
  { group: "PORTFOLIO", items: [
    { id: "optim", label: "Optimization", icon: Gauge }] },
  // One entry, not four. Perps, AMM and liquidation are stages of the same
  // desk, not peers of the FX and rates screens - and keeping them separate
  // put crypto tickers three clicks from a Treasury curve. The whole group
  // drops when gated off; an empty group would render a bare header.
  ...(CRYPTO_DESK_ENABLED ? [{ group: "DIGITAL ASSETS", items: [
    { id: "cryptodesk", label: "Crypto Desk", icon: Coins }] }] : []),
  { group: "ANALYTICS", items: [
    { id: "quanthub", label: "Risk & Quant Hub", icon: Sigma },
    { id: "backtest", label: "Backtesting / TCA", icon: History },
    { id: "market", label: "Market Data", icon: Database },
    { id: "obs", label: "Observability", icon: Cpu },
    { id: "genai", label: "GenAI Monitor", icon: Bot },
    { id: "models", label: "Model Library", icon: BookOpen }] },
  { id: "copilot", label: "Risk Copilot", icon: Zap },
];

// Views with a real, API-backed screen. Kept beside the router so adding a
// NAV entry without a branch surfaces immediately.
const HANDLED = new Set(["overview", "sor", "blotter", "limits", "market", "rates",
  "backtest", "sensitivity", "stress", "optim", "genai", "models", "market-risk", "quanthub",
  "pricing", "obs", "copilot",
  ...(CRYPTO_DESK_ENABLED ? ["cryptodesk"] : [])]);

export default function Console() {
  const [view, setView] = useState("overview");
  // The rail is a fixed 230px column that pushed the canvas off-screen on a
  // phone. It now collapses below 900px and opens as an overlay drawer, so
  // the nav is reachable without stealing width from the content.
  const [navOpen, setNavOpen] = useState(false);
  // The context bar used to read `Portfolio: Global Multi-Asset · As-of:
  // 2026-09-05` as literals. The portfolio name was invented, and the date was
  // frozen -- it sat on every screen quietly asserting a stale as-of while the
  // data underneath moved. Both now come from the book and the marks.
  const [ctx, setCtx] = useState(null);
  // These two are independent -- the blotter is read only for its marks
  // as-of. Awaiting them in sequence made the context bar cost two serial
  // round-trips on EVERY page load, on the critical path of the whole shell,
  // and both endpoints are among the slowest on the platform.
  useEffect(() => {
    (async () => {
      try {
        const get = (u) => fetch(u).then((x) => x.ok ? x.json() : null).catch(() => null);
        const [d, bl] = await Promise.all([
          get("/api/oms/portfolios"), get("/api/oms/blotter"),
        ]);
        if (!d) return;
        setCtx({ books: d.portfolios?.length || 0,
                 positions: d.firm_totals?.position_count ?? null,
                 asOf: bl?.marks_as_of || null });
      } catch { /* context bar degrades to '—', never blocks the console */ }
    })();
  }, []);
  const flat = NAV.flatMap((n) => n.items || [n]);
  const active = flat.find((i) => i.id === view);
  const go = (id) => { setView(id); setNavOpen(false); };  // close on select
  return (
    <div style={S.root} className="shell">
      <style>{CSS}</style>
      {/* Scrim: tapping outside the drawer closes it. Only rendered when
          open so it can never swallow clicks on desktop. */}
      {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}
      <aside style={S.rail} className={`rail${navOpen ? " open" : ""}`}>
        <div style={S.brand}>
          <div style={S.mark}>◈</div>
          <div><div style={{ fontWeight: 600, letterSpacing: 1, fontSize: 14 }}>QUANTGENLABS</div>
            <div style={{ color: T.dim, fontSize: 10, letterSpacing: 1 }}>RISK & QUANT CONSOLE</div></div>
          <button className="navclose" onClick={() => setNavOpen(false)}
            aria-label="Close navigation" style={S.iconBtn}>✕</button>
        </div>
        <nav style={{ padding: "6px 8px", overflowY: "auto", flex: 1 }}>
          {NAV.map((n, i) => n.group ? (
            <div key={i} style={{ marginTop: 12 }}>
              <div style={S.groupLbl}>{n.group}</div>
              {n.items.map((it) => <NavItem key={it.id} it={it} on={view === it.id} go={go} />)}
            </div>) : <NavItem key={n.id} it={n} on={view === n.id} go={go} />)}
        </nav>
        <div style={S.railFoot}><EngineStatus /></div>
      </aside>
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
        <header style={S.topbar}>
          <button className="navtoggle" onClick={() => setNavOpen((v) => !v)}
            aria-label="Toggle navigation" aria-expanded={navOpen} style={S.iconBtn}>☰</button>
          <span style={{ color: T.dim, fontSize: 12, whiteSpace: "nowrap" }}>{active?.label}</span>
          <div style={S.searchWrap} className="searchwrap"><Search size={14} color={T.dim} />
            <input placeholder="Search instruments, models, commands…" style={S.search} />
            <span style={S.kbd} className="kbd"><Command size={11} /> K</span></div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginLeft: "auto" }}>
            <span className="ctxwrap" style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Ctx k="Books" v={ctx ? `${ctx.books} · ${ctx.positions} pos` : "—"} />
              <Ctx k="Marks as-of" v={ctx?.asOf || "live + EOD"} />
              <Ctx k="Base" v="USD" /></span>
            <span style={S.sim}>SIMULATED · NOT PRODUCTION</span></div>
        </header>
        <main style={S.canvas}>
          {view === "overview" && <Overview go={setView} />}
          {view === "sor" && <SmartOrderRouting />}
          {view === "blotter" && <Blotter />}
          {view === "limits" && <LimitsAlerts />}
          {view === "market" && <MarketData />}
          {view === "rates" && <RatesFI />}
          {view === "backtest" && <TCA />}
          {view === "sensitivity" && <FactorSensitivity />}
          {view === "stress" && <StressScenarios />}
          {view === "optim" && <Optimization />}
          {view === "genai" && <GenAIMonitor />}
          {view === "models" && <ModelLibrary />}
          {view === "market-risk" && <MarketRisk />}
          {view === "quanthub" && <QuantHub />}
          {view === "pricing" && <Pricing />}
          {/* The Phase 1 desk replaces the previous perps/AMM console. Its
              styles are scoped to this wrapper, so nothing leaks into the
              rest of the shell. */}
          {CRYPTO_DESK_ENABLED && view === "cryptodesk" && (
            <div className="qgl-desk"><DigitalAssetsDesk /></div>
          )}
          {view === "obs" && <Observability />}
          {view === "copilot" && <Copilot />}
          {/* Every NAV id above maps to a real screen. If one is ever added
              without a branch, this makes the gap loud instead of rendering a
              silent blank canvas. */}
          {!HANDLED.has(view) && (
            <Empty text={`No screen registered for "${view}" — this is a routing gap, not an empty dataset.`} />)}
        </main>
        <ConsoleFooter />
      </div>
    </div>
  );
}

// The rail footer hardcoded "C++ + numpy engines · healthy" regardless of
// what was actually loaded. It now reads /api/health.
function EngineStatus() {
  const [h, setH] = useState(null);
  useEffect(() => {
    fetch("/api/health").then((r) => r.json()).then(setH).catch(() => setH(null));
  }, []);
  const ok = h?.status === "ok";
  const cpp = h?.engines?.cpp_available;
  return (
    <>
      <Circle size={7} fill={ok ? T.green : T.red} color={ok ? T.green : T.red} />
      <span style={{ fontSize: 11, color: T.dim }}>
        {h ? `${cpp ? "C++ + numpy" : "numpy"} engines · ${h.status}` : "checking…"}
      </span>
    </>
  );
}

// Attribution footer. Links to the GitHub PROFILE rather than a repo URL:
// senthilts9/quantgenlabs currently 404s, and shipping a dead link in the
// footer of a portfolio piece is worse than shipping no link.
function ConsoleFooter() {
  return (
    <footer style={S.footer}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", justifyContent: "center" }}>
        <span style={{ color: T.text }}>Cryptolabs</span>
        <span style={{ color: T.faint }}>·</span>
        <span>built by <b style={{ color: T.text }}>Senthil Saravanamuthu</b>,{" "}
          <span style={{ color: T.teal, fontFamily: MONO }}>PRM</span></span>
        <span style={{ color: T.faint }}>·</span>
        <span>Risk, Quantitative &amp; Front Office Developer</span>
        <span style={{ color: T.faint }}>·</span>
        <a href="https://github.com/senthilts9" target="_blank" rel="noopener noreferrer"
           style={{ color: T.teal, textDecoration: "none", display: "inline-flex",
                    alignItems: "center", gap: 4 }}>
          GitHub <ArrowUpRight size={11} />
        </a>
        <span style={{ color: T.faint }}>·</span>
        <a href="https://grafana.quantgenlabs.com" target="_blank" rel="noopener noreferrer"
           style={{ color: T.teal, textDecoration: "none", display: "inline-flex",
                    alignItems: "center", gap: 4 }}>
          Grafana <ArrowUpRight size={11} />
        </a>
      </div>
      <div style={{ marginTop: 4, color: T.faint }}>
        © {new Date().getFullYear()} Senthil Saravanamuthu. All rights reserved. Not public
        domain — shared for demonstration and portfolio purposes only.
      </div>
    </footer>
  );
}

function NavItem({ it, on, go }) {
  const A = it.icon;
  return <button className="nav" onClick={() => go(it.id)} style={{ ...S.nav, ...(on ? S.navOn : null) }}>
    <A size={16} color={on ? T.teal : T.dim} /><span style={{ color: on ? T.text : T.dim, fontSize: 12.5 }}>{it.label}</span>
    {on && <span style={{ marginLeft: "auto", width: 3, height: 15, background: T.teal, borderRadius: 2 }} />}
  </button>;
}
const Ctx = ({ k, v }) => <div style={S.ctx}><span style={{ color: T.dim, fontSize: 10 }}>{k}</span>
  <span style={{ fontSize: 12, fontFamily: MONO }}>{v}</span></div>;

/* ---------- OVERVIEW ---------- */
function Overview({ go }) {
  // Every figure below comes from a live endpoint. This screen previously
  // displayed a hardcoded array ($14.2M P&L, 342 positions, invented alerts)
  // while the blotter showed the real book -- the two disagreed by four
  // orders of magnitude, which discredits the screens that ARE real.
  const [d, setD] = useState({});
  const [err, setErr] = useState(null);
  const load = async () => {
    const get = async (u) => { try { const r = await fetch(u); return r.ok ? await r.json() : null; } catch { return null; } };
    const [blotter, factors, limits, prof, books] = await Promise.all([
      get("/api/oms/blotter"), get("/api/portfolio/factors"),
      get("/api/oms/limits"), get("/api/obs/profiling"), get("/api/oms/portfolios"),
    ]);
    if (!blotter) setErr("backend unreachable");
    setD({ blotter, factors, limits, prof, books });
  };
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, []);

  const tot = d.blotter?.totals || {};
  const f = d.factors && !d.factors.empty && !d.factors.error ? d.factors : null;
  const util = d.limits?.utilisation || [];
  const breaches = d.limits?.breaches || [];
  const p99 = Math.max(0, ...(d.prof || []).map((r) => r.p99_ms));
  const books = d.books?.portfolios || [];

  // Composite score from REAL limit utilisation, not an invented dial value.
  const worstUtil = Math.max(0, ...util.map((u) => u.utilisation_pct));
  const score = Math.round(Math.min(100, worstUtil));
  const contrib = (f?.positions || []).map((p) => ({
    name: p.symbol, pct: Math.abs(p.pct_of_risk), signed: p.pct_of_risk }));
  const maxContrib = Math.max(1, ...contrib.map((c) => c.pct));
  const COLORS = [T.blue, T.teal, T.violet, T.amber, T.green];

  const alerts = [
    ...breaches.slice(0, 2).map((b) => ["HIGH", `${b.gate}: ${b.symbol} ${b.detail}`.slice(0, 90), T.red]),
    ...util.filter((u) => u.status !== "ok").map((u) =>
      [u.status.toUpperCase(), `${u.limit} at ${fmt(u.utilisation_pct, 1)}% of cap`,
       u.status === "breach" ? T.red : T.amber]),
    ...(d.limits?.limits?.kill_switch ? [["HIGH", "Kill switch ACTIVE — all orders blocked", T.red]] : []),
  ];
  if (!alerts.length) alerts.push(["INFO", `All limits within tolerance · ${util.length} monitored`, T.teal]);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.1fr 1fr 1fr", gap: 14 }}>
      <Panel title="Limit utilisation" sub="highest consumed limit across the book">
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Arc score={score} />
          <div style={{ display: "grid", gap: 9, flex: 1 }}>
            <KV k={`1D VaR (${f ? f.confidence * 100 : 99}%)`} v={f ? fmt(f.var_historical, 2) : "—"} />
            <KV k="Expected Shortfall" v={f ? fmt(f.es_historical, 2) : "—"} />
            <KV k="Ann. Volatility" v={f ? `${fmt(f.portfolio_vol_annual_pct, 2)}%` : "—"} />
          </div></div>
        {err && <div style={{ color: T.red, fontSize: 11.5, marginTop: 8 }}>{err}</div>}
      </Panel>

      <Panel title="VaR contribution" sub="component VaR — sums exactly to portfolio VaR (Euler)">
        <div style={{ display: "grid", gap: 9 }}>
          {contrib.length ? contrib.map((c, i) => (
            <div key={c.name}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, color: T.dim, marginBottom: 3 }}>
                <span>{c.name}</span>
                <span style={{ fontFamily: MONO, color: c.signed < 0 ? T.green : T.text }}>
                  {fmt(c.signed, 1)}%</span></div>
              <Bar pct={c.pct / maxContrib * 100} c={c.signed < 0 ? T.green : COLORS[i % COLORS.length]} />
            </div>)) : <Empty text="no positions with mapped history yet" />}
          {contrib.some((c) => c.signed < 0) && (
            <div style={{ fontSize: 11, color: T.green }}>negative = diversifying (reduces total risk)</div>)}
        </div>
      </Panel>

      <Panel title="Alerts" sub="live from the risk-limit engine">
        <div style={{ display: "grid", gap: 8 }}>
          {alerts.slice(0, 4).map(([s, t, c], i) => (
            <div key={i} style={S.alert}><span style={{ ...S.pill, color: c, borderColor: c }}>{s}</span>
              <span style={{ fontSize: 12.5 }}>{t}</span></div>))}
        </div>
      </Panel>

      {[["Total P&L", tot.total_pnl, T.green, (v) => fmt(v ?? 0, 2)],
        ["Unrealised", tot.unrealized_pnl, T.teal, (v) => fmt(v ?? 0, 2)],
        ["Realised", tot.realized_pnl, T.blue, (v) => fmt(v ?? 0, 2)],
        ["Gross exposure", tot.gross_notional, T.violet, (v) => fmt(v ?? 0, 0)],
        ["Positions", tot.position_count, T.dim, (v) => `${v ?? 0}`],
        ["Engine p99", p99, T.blue, (v) => v ? `${fmt(v, 3)}ms` : "—"]].map(([k, v, c, f2]) => (
        <Panel key={k} pad="tight"><div style={{ display: "flex", justifyContent: "space-between" }}>
          <div><div style={{ color: T.dim, fontSize: 11.5 }}>{k}</div>
            <div style={{ fontFamily: MONO, fontSize: 21, fontWeight: 600, marginTop: 3,
              color: typeof v === "number" && v < 0 ? T.red : T.text }}>{f2(v)}</div></div>
          <Spark c={c} /></div></Panel>))}

      <Panel title="Books" sub={`${books.length} portfolio(s) · independent cost basis per book`}>
        {books.length ? (
          <table style={S.table}><thead><tr>
            {["Book", "Pos", "Gross", "Unreal", "P&L"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: h === "Book" ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{books.map((b) => (
            <tr key={b.id} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO }}>{b.id}</span>
                <span style={{ color: T.faint, fontSize: 10.5 }}> {b.name}</span></td>
              <td style={S.tdN}>{b.positions}</td>
              <td style={S.tdN}>{fmt(b.gross_notional, 0)}</td>
              <td style={{ ...S.tdN, color: b.unrealized_pnl < 0 ? T.red : T.green }}>{fmt(b.unrealized_pnl, 2)}</td>
              <td style={{ ...S.tdN, color: b.total_pnl < 0 ? T.red : T.green }}>{fmt(b.total_pnl, 2)}</td>
            </tr>))}</tbody></table>) : <Empty text="—" />}
      </Panel>

      <Panel title="Jump to a screen" sub="every figure on this page is read from a live endpoint">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {[["Blotter", "blotter"], ["Limits", "limits"], ["Market risk", "market-risk"],
            ["Rates & FI", "rates"], ["TCA", "backtest"], ["Observability", "obs"]].map(([l, v]) => (
            <button key={v} onClick={() => go(v)} style={S.tab}>{l} ›</button>))}
        </div>
      </Panel>
    </div>
  );
}

/* ---------- RISK & QUANT HUB ---------- */
// The quantitative foundations, computed on the desk's OWN return series. The
// fat-tail exhibit is the point: a normal distribution sits under every
// parametric VaR on this platform, and it is wrong in exactly the region that
// matters. Plotting the empirical histogram against the fitted normal makes
// that visible instead of asserted.
function QuantHub() {
  const [tab, setTab] = useState("tails");
  const [d, setD] = useState({});
  const [state, setState] = useState("loading");

  useEffect(() => {
    (async () => {
      const get = (u) => fetch(u).then((r) => r.ok ? r.json() : null).catch(() => null);
      const [dist, prob, la, ts, st] = await Promise.all([
        get("/api/quant/distributions"), get("/api/quant/probability"),
        get("/api/quant/linear-algebra"), get("/api/quant/timeseries"),
        get("/api/quant/statistics"),
      ]);
      setD({ dist, prob, la, ts, st });
      setState(dist ? "ready" : "error");
    })();
  }, []);

  const TABS = [["tails", "Fat tails"], ["ts", "Time series"], ["la", "Linear algebra"],
                ["prob", "Probability"], ["stats", "Estimation error"]];

  if (state === "loading") return <Panel title="Risk & Quant Hub"><Empty text="Computing on the live return series…" /></Panel>;
  if (state === "error") return <Panel title="Risk & Quant Hub"><Empty text="Analytics unavailable — backend unreachable." /></Panel>;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Risk & Quant Hub"
        sub={d.dist?.meta ? `${d.dist.meta.source} · ${d.dist.meta.n} observations · ${d.dist.meta.from} → ${d.dist.meta.to}` : ""}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {TABS.map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)}
              style={{ ...S.seg, ...(tab === k ? S.segOn : {}), flex: "0 0 auto", padding: "7px 14px" }}>{l}</button>))}
        </div>
      </Panel>

      {tab === "tails" && <FatTails d={d.dist} />}
      {tab === "ts" && <TimeSeriesPanel d={d.ts} />}
      {tab === "la" && <LinearAlgebraPanel d={d.la} />}
      {tab === "prob" && <ProbabilityPanel d={d.prob} />}
      {tab === "stats" && <EstimationPanel d={d.st} />}
    </div>
  );
}

/* ---- chart interaction primitives --------------------------------------- *
 * An SVG chart here is drawn in viewBox units but rendered at whatever width
 * the grid gives it, so a pointer event's clientX has to be mapped back into
 * viewBox space before it can be matched against a datum. preserveAspectRatio
 * defaults to a uniform fit, so one scale factor does it.
 */
function useVbPointer(W) {
  const ref = React.useRef(null);
  const [vbx, setVbx] = useState(null);
  const onMove = (e) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r?.width) return;
    setVbx((e.clientX - r.left) * W / r.width);
  };
  return { ref, vbx, hoverProps: {
    onPointerMove: onMove, onPointerLeave: () => setVbx(null),
    onPointerDown: onMove, style: { touchAction: "none" } } };
}

// Wrapper that positions an HTML tooltip over an SVG chart. HTML rather than
// <text>: tooltips need wrapping, padding and a background, and laying that
// out by hand in SVG is a lot of arithmetic for a worse result.
function ChartFrame({ children, tip, tipAt, tipY, W, H }) {
  // The card follows the mark it describes. Anchored to a fixed corner it ends
  // up commenting on a bar at the far side of the plot, and covers the y-axis
  // and the curve on the way. Flip to the other side of the cursor past the
  // midpoint so it is never clipped by the panel edge.
  const pct = tipAt == null ? 0 : (tipAt / W) * 100;
  // Flip at the midpoint, not past it: a card anchored at 55% still reaches
  // well over the right half of the plot before it runs out of content.
  const flip = pct > 50;
  // Ceiling at 36%: the card is fixed-size HTML over a chart that scales with
  // its column, so a percentage anchored to a short bar puts its BOTTOM
  // through the x-axis band and the legend. 36% keeps the tallest card clear
  // of both at every width, and a bar low enough to need more room has empty
  // plot above it anyway.
  const top = tipY == null || !H ? 6 : Math.max(2, Math.min(36, (tipY / H) * 100));
  return (
    <div style={{ position: "relative" }}>
      {children}
      {tip && tipAt != null && (
        <div style={{
          position: "absolute", top: `${top}%`, left: `${pct}%`,
          transform: `translateX(${flip ? "calc(-100% - 14px)" : "14px"})`,
          background: "#0B111C", border: `1px solid ${T.line}`, borderRadius: 8,
          padding: "9px 11px", pointerEvents: "none", zIndex: 5,
          // Bounded width. The explanatory footer is a sentence, and without a
          // ceiling the card stretches to fit it on one line -- which put it
          // across the right half of the histogram and over the x-axis labels.
          minWidth: 186, maxWidth: 250,
          boxShadow: "0 8px 24px rgba(0,0,0,.45)",
        }}>{tip}</div>)}
    </div>
  );
}

// One tooltip row: the value leads in high-contrast ink, the label follows in
// secondary. `k` is keyed by a short stroke of the series colour rather than a
// filled box -- at this density a box is data-weight ink doing a label's job.
const TipRow = ({ c, l, v, tone }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 5 }}>
    {c ? <span style={{ width: 11, height: 2, background: c, borderRadius: 1, flexShrink: 0 }} />
       : <span style={{ width: 11, flexShrink: 0 }} />}
    <span style={{ color: T.dim, fontSize: 10.5, flex: 1, whiteSpace: "nowrap" }}>{l}</span>
    <span style={{ fontFamily: MONO, fontSize: 11.5, color: tone || T.text, fontWeight: 500 }}>{v}</span>
  </div>
);
const TipHead = ({ children }) => (
  <div style={{ fontFamily: MONO, fontSize: 11.5, color: T.text, fontWeight: 600,
                borderBottom: `1px solid ${T.lineSoft}`, paddingBottom: 6 }}>{children}</div>
);

// Legend: always present for two or more series, mirroring each mark's shape
// (rect for bars/areas, line for lines). Text wears text tokens; the colour
// lives in the key beside it, never in the words.
const LegendKey = ({ c, shape, children }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    {shape === "line"
      ? <span style={{ width: 14, height: 2, background: c, borderRadius: 1 }} />
      : <span style={{ width: 10, height: 10, background: c, borderRadius: 2 }} />}
    <span style={{ color: T.dim, fontSize: 11 }}>{children}</span>
  </span>
);

// Column with a 4px rounded data-end and square corners at the baseline.
function barPath(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y}`
       + ` L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
}

// Overlaid density: empirical histogram vs the normal fitted to the SAME mean
// and variance. Same moments, so any divergence is pure shape.
//
// `n` turns the density axis into something a reader can actually hold: a
// density of 0.31 means nothing on its own, but "17 days landed here, a normal
// predicts 6" is the entire argument of this screen in one hover.
function DensityChart({ centres, empirical, normal, n }) {
  const W = 680, H = 300, PL = 46, PR = 14, PB = 34, PT = 16;
  const maxY = Math.max(...empirical, ...normal) * 1.08 || 1;
  const x0 = centres[0], x1 = centres[centres.length - 1];
  const px = (v) => PL + (v - x0) / (x1 - x0) * (W - PL - PR);
  const py = (v) => (H - PB) - v / maxY * (H - PB - PT);
  const slot = (W - PL - PR) / centres.length;
  const bw = Math.max(1, Math.min(24, slot - 2));   // 2px surface gap between bars
  const binW = centres.length > 1 ? centres[1] - centres[0] : 1;
  const days = (dens) => dens * binW * (n || 0);    // density -> observed days

  const { ref, vbx, hoverProps } = useVbPointer(W);
  let hi = null;
  if (vbx != null && vbx >= PL - slot && vbx <= W - PR + slot) {
    const s = x0 + (vbx - PL) / (W - PL - PR) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < centres.length; i++)
      if (Math.abs(centres[i] - s) < Math.abs(centres[best] - s)) best = i;
    hi = best;
  }

  const area = normal.map((v, i) => `${i ? "L" : "M"}${px(centres[i])},${py(v)}`).join(" ")
    + ` L${px(centres[centres.length - 1])},${py(0)} L${px(centres[0])},${py(0)} Z`;
  const line = normal.map((v, i) => `${i ? "L" : "M"}${px(centres[i])},${py(v)}`).join(" ");
  const ratio = hi != null && normal[hi] > 1e-9 ? empirical[hi] / normal[hi] : null;

  // The interesting statistic is NOT the same everywhere on this curve, so the
  // readout changes with the region the pointer is in. A fixed set of rows
  // would spend the peak explaining tail risk and the tail quoting a peak
  // density -- the reader has to work out which numbers matter where, which is
  // the job the chart is supposed to be doing.
  //
  //   CORE   (|z| <= 1)  peak fit: how much of the sample sits inside, and how
  //                      the observed height compares with the normal's. This
  //                      is where leptokurtosis shows up as a TALLER middle.
  //   SHOULDER(1..3)     the crossover: cumulative mass beyond this point vs
  //                      what a normal implies -- where the two curves swap
  //                      which one is higher.
  //   TAIL   (|z| > 3)   the money region: exceedance counts and the odds a
  //                      normal assigns to a day this size.
  const cum = (from) => {                        // share of sample beyond |z|
    let s = 0;
    for (let i = 0; i < centres.length; i++)
      if (Math.abs(centres[i]) >= from) s += empirical[i] * binW;
    return s;
  };
  const nCum = (from) => {                       // same, under the fitted normal
    let s = 0;
    for (let i = 0; i < centres.length; i++)
      if (Math.abs(centres[i]) >= from) s += normal[i] * binW;
    return s;
  };
  const z = hi == null ? 0 : centres[hi];
  const az = Math.abs(z);
  const region = az <= 1 ? "core" : az <= 3 ? "shoulder" : "tail";
  const empty = hi != null && empirical[hi] === 0;

  const tip = hi == null ? null : (<>
    <TipHead>{fmt(centres[hi] - binW / 2, 2)}σ … {fmt(centres[hi] + binW / 2, 2)}σ</TipHead>
    {empty ? (<>
      <TipRow c={V.s1} l="Observed days" v="none" tone={T.dim} />
      <TipRow c={V.s2} l="Normal predicts" v={fmt(days(normal[hi]), 2)} />
    </>) : (<>
      <TipRow c={V.s1} l="Observed days" v={fmt(days(empirical[hi]), 0)} />
      <TipRow c={V.s2} l="Normal predicts" v={fmt(days(normal[hi]), 1)} />
      {ratio != null && (
        <TipRow l={ratio >= 1 ? "More often than normal" : "Less often than normal"}
          v={`${fmt(ratio, 2)}×`}
          tone={ratio > 2 ? T.red : ratio > 1.2 ? T.amber : T.dim} />)}
    </>)}

    {/* --- region-specific rows --- */}
    {region === "core" && (<>
      <TipRow l="Peak height vs normal"
        v={`${fmt(Math.max(...empirical) / Math.max(...normal), 2)}×`}
        tone={Math.max(...empirical) / Math.max(...normal) > 1.15 ? T.amber : T.dim} />
      <TipRow l="Sample within ±1σ" v={`${fmt((1 - cum(1)) * 100, 1)}%`} />
      <TipRow l="Normal implies" v={`${fmt((1 - nCum(1)) * 100, 1)}%`} />
    </>)}
    {region === "shoulder" && (<>
      <TipRow l={`Sample beyond ±${fmt(az, 1)}σ`} v={`${fmt(cum(az) * 100, 2)}%`} />
      <TipRow l="Normal implies" v={`${fmt(nCum(az) * 100, 2)}%`} />
      <TipRow l="Excess mass out here"
        v={`${cum(az) - nCum(az) >= 0 ? "+" : ""}${fmt((cum(az) - nCum(az)) * 100, 2)}pp`}
        tone={cum(az) - nCum(az) > 0.005 ? T.amber : T.dim} />
    </>)}
    {region === "tail" && (<>
      <TipRow l="Days beyond ±3σ" v={fmt(cum(3) * (n || 0), 0)} />
      <TipRow l="Normal predicts" v={fmt(nCum(3) * (n || 0), 2)} />
      <TipRow l={`A ${fmt(az, 1)}σ day, under a normal`}
        v={`1 in ${normOdds(az)}`} tone={T.red} />
    </>)}

    <div style={{ marginTop: 7, paddingTop: 6, borderTop: `1px solid ${T.lineSoft}`,
                  color: T.dim, fontSize: 10, lineHeight: 1.45 }}>
      {region === "core"
        ? "The middle. A heavier-tailed sample is also a TALLER, narrower middle — same variance has to come from somewhere."
        : region === "shoulder"
        ? "The crossover. Somewhere through here the two curves swap which one is higher."
        : "Beyond ±3σ — the region a parametric VaR treats as effectively impossible."}
    </div>
  </>);

  return (
    <ChartFrame tip={tip} W={W} H={H}
      tipAt={hi == null ? null : px(centres[hi])}
      tipY={hi == null ? null : Math.min(py(empirical[hi]), py(normal[hi]))}>
      <svg ref={ref} {...hoverProps} width="100%" viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: "100%", display: "block", cursor: "crosshair" }}
        role="img" aria-label="Empirical return histogram overlaid with the fitted normal density">
        {/* tail zones: shaded because this is where the two curves part company */}
        {[[x0, -3], [3, x1]].map(([a, b], i) => (
          <rect key={i} x={px(a)} y={PT} width={Math.max(0, px(b) - px(a))}
            height={H - PB - PT} fill={T.red} opacity="0.07" />))}
        {/* recessive hairline grid, solid */}
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={PL} y1={py(maxY * f)} x2={W - PR} y2={py(maxY * f)}
            stroke={V.grid} strokeWidth="1" />))}
        {/* fitted normal: 10% wash + 2px line */}
        <path d={area} fill={V.s2} opacity="0.10" />
        {/* empirical bars */}
        {empirical.map((v, i) => (
          <path key={i} d={barPath(px(centres[i]) - bw / 2, py(v), bw, (H - PB) - py(v))}
            fill={V.s1} opacity={hi == null || hi === i ? 1 : 0.55} />))}
        <path d={line} fill="none" stroke={V.s2} strokeWidth="2"
          strokeLinejoin="round" strokeLinecap="round" />
        {/* crosshair */}
        {hi != null && (
          <line x1={px(centres[hi])} y1={PT} x2={px(centres[hi])} y2={H - PB}
            stroke={T.text} strokeWidth="1" opacity="0.45" />)}
        {/* axes */}
        <line x1={PL} y1={H - PB} x2={W - PR} y2={H - PB} stroke={T.line} strokeWidth="1" />
        {[0.5, 1].map((f) => (
          <text key={f} x={PL - 8} y={py(maxY * f) + 3.5} textAnchor="end" fontSize="9.5"
            fill={T.faint} fontFamily={MONO}>{fmt(maxY * f, 2)}</text>))}
        <text x={PL - 8} y={py(0) + 3.5} textAnchor="end" fontSize="9.5"
          fill={T.faint} fontFamily={MONO}>0</text>
        {[-4, -3, -2, -1, 0, 1, 2, 3, 4].map((s) => (
          <text key={s} x={px(s)} y={H - 12} textAnchor="middle" fontSize="9.5"
            fill={T.faint} fontFamily={MONO}>{s}σ</text>))}
        <text x={W / 2} y={H - 1} textAnchor="middle" fontSize="9.5" fill={T.faint}>
          standard deviations from the mean
        </text>
        {/* name both shaded zones, so the shading reads as meaning rather than decor */}
        <text x={px(-3.1)} y={PT + 11} textAnchor="end" fontSize="9.5" fill={T.dim}>tail</text>
        <text x={px(3.1)} y={PT + 11} textAnchor="start" fontSize="9.5" fill={T.dim}>tail</text>
      </svg>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center",
                    marginTop: 4, paddingLeft: PL / 2 }}>
        <LegendKey c={V.s1} shape="rect">Empirical (observed returns)</LegendKey>
        <LegendKey c={V.s2} shape="line">Fitted normal (same μ, σ)</LegendKey>
        <span style={{ color: T.faint, fontSize: 10.5 }}>hover for the numbers behind any bar</span>
      </div>
    </ChartFrame>);
}

// Q-Q: empirical quantiles against normal ones. A straight line means normal;
// the S-bend at the ends is the fat tail, and it is easier to SEE here than in
// the density plot, where the tail is squashed against zero.
function QQChart({ points }) {
  const W = 320, H = 300, PL = 42, PR = 12, PB = 40, PT = 14;
  const all = points.flatMap((p) => [p.theoretical, p.empirical]);
  const lo = Math.min(...all), hi2 = Math.max(...all);
  const sc = (v) => PL + (v - lo) / (hi2 - lo) * (W - PL - PR);
  const sy = (v) => (H - PB) - (v - lo) / (hi2 - lo) * (H - PB - PT);

  const { ref, vbx, hoverProps } = useVbPointer(W);
  let hi = null;
  if (vbx != null) {
    let best = 0;
    for (let i = 1; i < points.length; i++)
      if (Math.abs(sc(points[i].theoretical) - vbx) < Math.abs(sc(points[best].theoretical) - vbx)) best = i;
    // Only latch on if the pointer is genuinely near a point's column.
    if (Math.abs(sc(points[best].theoretical) - vbx) < 22) hi = best;
  }
  const p = hi == null ? null : points[hi];
  const dev = p ? p.empirical - p.theoretical : 0;
  const pct = hi == null ? 0 : (0.005 + (hi / (points.length - 1)) * 0.99) * 100;

  const tip = !p ? null : (<>
    <TipHead>{fmt(pct, 1)}th percentile</TipHead>
    <TipRow c={V.s2} l="Normal says" v={`${fmt(p.theoretical, 2)}σ`} />
    <TipRow c={V.s1} l="Actually was" v={`${fmt(p.empirical, 2)}σ`} />
    <TipRow l="Deviation" v={`${dev >= 0 ? "+" : ""}${fmt(dev, 2)}σ`}
      tone={Math.abs(dev) > 0.5 ? T.red : Math.abs(dev) > 0.2 ? T.amber : T.dim} />
    <div style={{ marginTop: 7, paddingTop: 6, borderTop: `1px solid ${T.lineSoft}`,
                  color: T.dim, fontSize: 10, lineHeight: 1.45 }}>
      {Math.abs(dev) < 0.2 ? "On the line — normal fits here."
        : dev * p.theoretical > 0 ? "Outside the line — the real move was bigger than normal allows."
        : "Inside the line — the real move was smaller than normal allows."}
    </div>
  </>);

  return (
    <ChartFrame tip={tip} W={W} H={H}
      tipAt={p == null ? null : sc(p.theoretical)}
      tipY={p == null ? null : sy(p.empirical)}>
      <svg ref={ref} {...hoverProps} width="100%" viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: "100%", display: "block", cursor: "crosshair" }}
        role="img" aria-label="Q-Q plot of empirical against normal quantiles">
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={PL} y1={sy(lo + (hi2 - lo) * f)} x2={W - PR} y2={sy(lo + (hi2 - lo) * f)}
            stroke={V.grid} strokeWidth="1" />))}
        {/* y = x: what perfect normality looks like */}
        <line x1={sc(lo)} y1={sy(lo)} x2={sc(hi2)} y2={sy(hi2)}
          stroke={V.s2} strokeWidth="2" strokeLinecap="round" opacity="0.9" />
        {points.map((q, i) => (
          <circle key={i} cx={sc(q.theoretical)} cy={sy(q.empirical)} r={hi === i ? 5 : 3.4}
            fill={V.s1} stroke={V.surface} strokeWidth="2"
            opacity={hi == null || hi === i ? 1 : 0.6} />))}
        <line x1={PL} y1={H - PB} x2={W - PR} y2={H - PB} stroke={T.line} strokeWidth="1" />
        <line x1={PL} y1={PT} x2={PL} y2={H - PB} stroke={T.line} strokeWidth="1" />
        <text x={W / 2} y={H - 22} textAnchor="middle" fontSize="9.5" fill={T.faint} fontFamily={MONO}>
          normal quantile (σ)
        </text>
        <text x={W / 2} y={H - 8} textAnchor="middle" fontSize="9.5" fill={T.faint}>
          straight line = normal · S-bend = fat tails
        </text>
        <text transform={`translate(13,${(H - PB + PT) / 2}) rotate(-90)`} textAnchor="middle"
          fontSize="9.5" fill={T.faint} fontFamily={MONO}>empirical quantile (σ)</text>
      </svg>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 4, paddingLeft: PL / 2 }}>
        <LegendKey c={V.s1} shape="rect">Observed quantile</LegendKey>
        <LegendKey c={V.s2} shape="line">Perfect normal</LegendKey>
      </div>
    </ChartFrame>);
}

/* A legend says WHAT each mark is. This says what the reader is looking FOR,
 * which is a different question and the one people actually arrive with. Each
 * glyph is the real mark at small scale, so the mapping to the chart beside it
 * needs no explaining.
 *
 * These describe how to READ the plot; they never assert which way this
 * particular book came out. The sample decides that, and it does not always
 * come out the textbook way -- a book whose vol is set by a few large moves
 * can show FEWER +/-1 sigma days than a normal predicts even while its
 * kurtosis is enormous. The adaptive verdict is the backend's `interpretation`
 * block and the exceedance table below; static copy that front-ran either
 * would eventually contradict the numbers next to it. */
const READING = [
  { t: "Compare the shapes", d: "bars are what happened; the curve is what a normal predicts from the same μ and σ — only the shape differs",
    g: (<>
      <path d="M2,26 Q18,4 34,26" fill="none" stroke={V.s2} strokeWidth="2" strokeLinecap="round" />
      {[[8, 20], [14, 11], [20, 8], [26, 12], [32, 21]].map(([x, y], i) => (
        <path key={i} d={barPath(x - 2.4, y, 4.8, 26 - y, 2)} fill={V.s1} />))}
    </>) },
  { t: "Watch the shaded tails", d: "past ±3σ, a bar above the curve means that move came more often than normal allows — below it, less often",
    g: (<>
      <rect x="0" y="2" width="9" height="24" fill={T.red} opacity="0.12" />
      <rect x="27" y="2" width="9" height="24" fill={T.red} opacity="0.12" />
      <path d="M2,25 Q18,6 34,25" fill="none" stroke={V.s2} strokeWidth="2" strokeLinecap="round" />
      {[[4, 19], [31, 20]].map(([x, y], i) => (
        <path key={i} d={barPath(x - 2.2, y, 4.4, 26 - y, 2)} fill={V.s1} />))}
    </>) },
  { t: "Hover any bar", d: "the tooltip gives observed days, what a normal predicts for that bin, and the ratio between them",
    g: (<>
      <path d={barPath(6, 14, 5, 12, 2)} fill={V.s1} opacity="0.55" />
      <path d={barPath(14, 8, 5, 18, 2)} fill={V.s1} />
      <path d={barPath(22, 16, 5, 10, 2)} fill={V.s1} opacity="0.55" />
      <line x1="16.5" y1="3" x2="16.5" y2="26" stroke={T.text} strokeWidth="1" opacity="0.5" />
      <rect x="20" y="2" width="16" height="11" rx="2.5" fill="#0B111C" stroke={T.line} />
    </>) },
];
const HowToRead = () => (
  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
                gap: 10, marginBottom: 14 }}>
    {READING.map((r) => (
      <div key={r.t} style={{ display: "flex", gap: 10, alignItems: "flex-start",
        background: T.panel2, border: `1px solid ${T.lineSoft}`, borderRadius: 8, padding: "10px 11px" }}>
        <svg width="36" height="28" viewBox="0 0 36 28" style={{ flexShrink: 0, marginTop: 1 }}
          aria-hidden="true">{r.g}</svg>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, color: T.text }}>{r.t}</div>
          <div style={{ fontSize: 10.5, color: T.dim, lineHeight: 1.45, marginTop: 2 }}>{r.d}</div>
        </div>
      </div>))}
  </div>
);

function FatTails({ d }) {
  if (!d || d.empty) return <Panel title="Fat tails"><Empty text={d?.message || "unavailable"} /></Panel>;
  const m = d.moments, I = d.interpretation;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Empirical returns vs the normal distribution"
        sub={`same mean and variance — every difference below is pure shape, not scale`
          + (d.meta?.n ? ` · ${d.meta.n} trading days from ${d.meta.source}` : "")}>
        <HowToRead />
        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 14 }}>
          <DensityChart {...d.histogram} n={d.meta?.n} />
          <QQChart points={d.qq_plot} />
        </div>
        <div style={{ ...S.alert, borderColor: m.normal_rejected ? T.red : T.green, marginTop: 12 }}>
          <span style={{ ...S.pill, color: m.normal_rejected ? T.red : T.green,
            borderColor: m.normal_rejected ? T.red : T.green }}>
            {m.normal_rejected ? "NORMALITY REJECTED" : "NORMAL NOT REJECTED"}</span>
          <span style={{ fontSize: 12.5 }}>{I.headline}</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 12, marginTop: 12 }}>
          <Big l="Skew" v={m.skew} accent={m.skew < 0 ? T.red : T.green} />
          <Big l="Excess kurtosis" v={m.excess_kurtosis} accent={m.excess_kurtosis > 1 ? T.red : T.green} />
          <Big l="Jarque-Bera" v={m.jarque_bera} />
          <Big l="JB p-value" v={m.jarque_bera_p} accent={m.normal_rejected ? T.red : T.green} />
          <Big l="Ann. volatility" v={m.vol_annual_pct} suffix="%" />
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 14 }}>
        <Panel title="How often did the 'impossible' happen?"
          sub="observed exceedances vs what a normal distribution predicts">
          <div style={{ overflowX: "auto" }}>
            <table style={S.table}><thead><tr>
              {["Move", "Observed", "Normal predicts", "Ratio", "Normal says"].map((h) => (
                <th key={h} style={{ ...S.th, textAlign: h === "Move" || h === "Normal says" ? "left" : "right" }}>{h}</th>))}
            </tr></thead><tbody>
              {d.tail_counts.map((t) => (
                <tr key={t.sigma} className="tr">
                  <td style={S.td}>beyond ±{t.sigma}σ</td>
                  <td style={S.tdN}>{t.observed}</td>
                  <td style={S.tdN}>{fmt(t.normal_expected, 2)}</td>
                  <td style={{ ...S.tdN, color: t.ratio > 2 ? T.red : t.ratio > 1.2 ? T.amber : T.dim,
                    fontWeight: t.ratio > 2 ? 600 : 400 }}>
                    {t.ratio != null ? `${fmt(t.ratio, 1)}×` : "—"}</td>
                  <td style={{ ...S.td, color: T.faint, fontSize: 11 }}>{t.once_every_normal}</td>
                </tr>))}
            </tbody></table>
          </div>
          <div style={{ ...S.alert, borderColor: T.red, marginTop: 12 }}>
            <span style={{ ...S.pill, color: T.red, borderColor: T.red }}>WORST DAY</span>
            <span style={{ fontSize: 12.5 }}>{d.worst_day_normal_odds}</span>
          </div>
        </Panel>

        <Panel title="Why this matters" sub="the consequence for every VaR on this platform">
          <div style={{ display: "grid", gap: 10, fontSize: 12.5, lineHeight: 1.6 }}>
            <div><b style={{ color: T.amber }}>Shape.</b> {I.skew}.</div>
            <div><b style={{ color: T.amber }}>Tails.</b> {I.kurtosis}.</div>
            <div><b style={{ color: T.red }}>Consequence.</b> {I.why_it_matters}</div>
            <div style={{ borderTop: `1px solid ${T.lineSoft}`, paddingTop: 10, color: T.dim }}>
              This is the reason the Market Risk screen quotes historical and
              Cornish-Fisher VaR beside the parametric number. The spread between
              those methods <i>is</i> the model risk — and on a fat-tailed sample the
              parametric figure is the optimistic one.
            </div>
          </div>
        </Panel>
      </div>
    </div>);
}

// Autocorrelation by lag, against the +/-2/sqrt(n) significance band. The
// hover names what a bar MEANS -- inside the band is indistinguishable from
// zero, which is the whole point of plotting the band.
function AcfChart({ values, band, colour, label = "autocorrelation" }) {
  const W = 320, H = 160, PL = 32, PR = 10, PB = 26, slotW = (W - PL - PR) / values.length;
  const maxY = Math.max(...values.map(Math.abs), band) * 1.2 || 1;
  const bw = Math.max(1, slotW - 2);             // 2px surface gap
  const py = (v) => (H - PB) / 2 - v / maxY * ((H - PB) / 2 - 8);
  const { ref, vbx, hoverProps } = useVbPointer(W);
  let hi = null;
  if (vbx != null && vbx >= PL && vbx <= W - PR)
    hi = Math.min(values.length - 1, Math.max(0, Math.floor((vbx - PL) / slotW)));
  const v = hi == null ? 0 : values[hi];
  const sig = hi != null && Math.abs(v) > band;

  const tip = hi == null ? null : (<>
    <TipHead>Lag {hi + 1}</TipHead>
    <TipRow c={colour} l={label} v={fmt(v, 3)} tone={sig ? colour : T.dim} />
    <TipRow l="Significance band" v={`±${fmt(band, 3)}`} />
    <TipRow l="Multiple of band" v={`${fmt(Math.abs(v) / (band || 1), 2)}×`}
      tone={sig ? T.amber : T.dim} />
    <div style={{ marginTop: 7, paddingTop: 6, borderTop: `1px solid ${T.lineSoft}`,
                  color: T.dim, fontSize: 10, lineHeight: 1.45 }}>
      {sig ? `Outside the band — dependence at this lag is real, not sampling noise.`
           : `Inside the band — indistinguishable from zero at this sample size.`}
    </div>
  </>);

  return (
    <ChartFrame tip={tip} W={W} H={H}
      tipAt={hi == null ? null : PL + hi * slotW + bw / 2}
      tipY={hi == null ? null : Math.min(py(v), py(0))}>
      <svg ref={ref} {...hoverProps} width="100%" viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: "100%", display: "block", cursor: "crosshair" }}
        role="img" aria-label={`${label} by lag with significance band`}>
        <rect x={PL} y={py(band)} width={W - PL - PR} height={Math.abs(py(-band) - py(band))}
          fill={T.dim} opacity="0.12" />
        <line x1={PL} y1={py(0)} x2={W - PR} y2={py(0)} stroke={T.line} />
        {values.map((val, i) => (
          <path key={i} d={barPath(PL + i * slotW + 1, Math.min(py(val), py(0)), bw,
            Math.abs(py(val) - py(0)), 3)}
            fill={Math.abs(val) > band ? colour : T.dim}
            opacity={hi != null && hi !== i ? 0.4 : (Math.abs(val) > band ? 0.95 : 0.5)} />))}
        <text x={PL} y={12} fontSize="9.5" fill={T.faint} fontFamily={MONO}>
          lag 1 → {values.length}
        </text>
        <text x={W / 2} y={H - 8} textAnchor="middle" fontSize="9.5" fill={T.faint}>
          shaded band = indistinguishable from zero
        </text>
      </svg>
    </ChartFrame>);
}

function TimeSeriesPanel({ d }) {
  if (!d || d.empty) return <Panel title="Time series"><Empty text={d?.message || "unavailable"} /></Panel>;
  const I = d.interpretation;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Volatility clustering" sub="returns are unpredictable; their MAGNITUDE is not">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
          <div>
            <div style={{ color: T.dim, fontSize: 12, marginBottom: 4 }}>
              Autocorrelation of <b>returns</b></div>
            <AcfChart values={d.returns_acf} band={d.significance_band} colour={T.blue} />
            <div style={{ fontSize: 11.5, color: T.dim, marginTop: 4 }}>{I.returns}</div>
          </div>
          <div>
            <div style={{ color: T.dim, fontSize: 12, marginBottom: 4 }}>
              Autocorrelation of <b>|returns|</b></div>
            <AcfChart values={d.abs_returns_acf} band={d.significance_band} colour={T.red} />
            <div style={{ fontSize: 11.5, color: T.dim, marginTop: 4 }}>{I.volatility}</div>
          </div>
        </div>
        <div style={{ ...S.alert, borderColor: T.amber, marginTop: 12 }}>
          <span style={{ ...S.pill, color: T.amber, borderColor: T.amber }}>CONSEQUENCE</span>
          <span style={{ fontSize: 12.5 }}>{I.consequence}</span>
        </div>
      </Panel>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="GARCH(1,1)" sub="conditional variance with mean reversion">
          <KVRow k="alpha (shock weight)" v={d.garch?.alpha} />
          <KVRow k="beta (persistence weight)" v={d.garch?.beta} />
          <KVRow k="alpha + beta" v={d.garch?.persistence}
            tone={(d.garch?.persistence ?? 0) > 0.98 ? T.red : T.green} />
          <KVRow k="Long-run vol" v={d.garch?.long_run_vol_annual_pct != null ? `${fmt(d.garch.long_run_vol_annual_pct, 2)}%` : null} />
          <KVRow k="Current vol" v={d.garch?.current_vol_annual_pct != null ? `${fmt(d.garch.current_vol_annual_pct, 2)}%` : null} tone={T.amber} />
          <KVRow k="Shock half-life" v={d.garch?.half_life_days != null ? `${d.garch.half_life_days} days` : null} />
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8 }}>{I.persistence}</div>
        </Panel>
        <Panel title="EWMA (RiskMetrics)" sub="λ = 0.94 — no mean reversion, tracks regime faster">
          <KVRow k="lambda" v={d.ewma?.lambda} />
          <KVRow k="Current sigma (ann.)" v={d.ewma?.sigma_annual_pct != null ? `${fmt(d.ewma.sigma_annual_pct, 2)}%` : null} tone={T.teal} />
          <KVRow k="Half-life" v={d.ewma?.half_life_days != null ? `${d.ewma.half_life_days} days` : null} />
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8 }}>
            EWMA has no long-run level to revert to, so it adapts to a new regime
            faster than GARCH but will drift with it indefinitely. GARCH pulls back
            toward its unconditional variance — which is right when the spike is
            temporary and wrong when the regime has genuinely changed.
          </div>
        </Panel>
      </div>
    </div>);
}

function LinearAlgebraPanel({ d }) {
  if (!d || d.empty) return <Panel title="Linear algebra"><Empty text={d?.message || "needs 2+ positions"} /></Panel>;
  const I = d.interpretation;
  const heat = (v) => {
    const a = Math.min(Math.abs(v), 1);
    return v >= 0 ? `rgba(56,189,248,${a * 0.75})` : `rgba(248,113,113,${a * 0.75})`;
  };
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Covariance eigen-decomposition"
        sub={`${d.symbols.length} instruments · ${d.alignment}`}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 12, marginBottom: 14 }}>
          <Big l="1st factor explains" v={d.first_factor_pct} suffix="%" accent={T.teal} />
          <Big l="Factors for 90%" v={d.factors_for_90pct} />
          <Big l="Effective rank" v={d.effective_rank} />
          <Big l="Condition number" v={d.condition_number}
            accent={d.condition_number > 1000 ? T.red : T.green} />
        </div>
        <div style={{ color: T.dim, fontSize: 12, marginBottom: 6 }}>
          Explained variance by principal component
        </div>
        <div style={{ display: "grid", gap: 5 }}>
          {d.explained_variance_pct.slice(0, 8).map((v, i) => (
            <div key={i}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, color: T.dim }}>
                <span>PC{i + 1}</span><span style={{ fontFamily: MONO }}>{fmt(v, 2)}%</span></div>
              <Bar pct={v} c={[T.teal, T.blue, T.violet, T.amber][i % 4]} />
            </div>))}
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: 14 }}>
        <Panel title="Correlation matrix" sub="blue = positive, red = negative — the diversification map">
          <div style={{ overflowX: "auto" }}>
            <table style={{ ...S.table, fontSize: 10.5 }}><thead><tr>
              <th style={S.th}></th>
              {d.symbols.map((s) => <th key={s} style={{ ...S.th, padding: "4px" }}>{s.slice(0, 6)}</th>)}
            </tr></thead><tbody>
              {d.correlation_matrix.map((row, i) => (
                <tr key={i}>
                  <td style={{ ...S.td, padding: "4px 6px", fontFamily: MONO }}>{d.symbols[i].slice(0, 8)}</td>
                  {row.map((v, j) => (
                    <td key={j} style={{ padding: "4px", textAlign: "center", fontFamily: MONO,
                      background: heat(v), color: Math.abs(v) > 0.55 ? "#fff" : T.dim }}>
                      {v.toFixed(2)}</td>))}
                </tr>))}
            </tbody></table>
          </div>
        </Panel>
        <Panel title="What the eigenvalues mean" sub="how many independent bets are really on">
          <div style={{ display: "grid", gap: 10, fontSize: 12.5, lineHeight: 1.6 }}>
            <div><b style={{ color: T.teal }}>Concentration.</b> {I.concentration}.</div>
            <div><b style={{ color: d.condition_number > 1000 ? T.red : T.green }}>Conditioning.</b> {I.conditioning}</div>
            <div style={{ borderTop: `1px solid ${T.lineSoft}`, paddingTop: 10, color: T.dim }}>
              {I.why_it_matters}
            </div>
          </div>
        </Panel>
      </div>
    </div>);
}

// Discrete pmf with an optional continuous overlay (the normal approximation).
// The hover carries the cumulative probabilities, which is what these charts
// are actually read FOR -- "how likely is k or more" is the question behind
// every operational-loss and exceedance count on the platform.
function PmfChart({ ks, pmf, overlay, overlayLabel, unit = "events" }) {
  const W = 560, H = 230, PL = 40, PR = 12, PB = 40, PT = 14;
  const maxY = Math.max(...pmf, ...(overlay || [0])) * 1.1 || 1;
  const slotW = (W - PL - PR) / ks.length;
  const bw = Math.max(1, Math.min(28, slotW - 2));
  const px = (i) => PL + i * slotW;
  const py = (v) => (H - PB) - v / maxY * (H - PB - PT);
  const { ref, vbx, hoverProps } = useVbPointer(W);
  let hi = null;
  if (vbx != null && vbx >= PL && vbx <= W - PR)
    hi = Math.min(ks.length - 1, Math.max(0, Math.floor((vbx - PL) / slotW)));

  const atMost = hi == null ? 0 : pmf.slice(0, hi + 1).reduce((a, b) => a + b, 0);
  const atLeast = hi == null ? 0 : pmf.slice(hi).reduce((a, b) => a + b, 0);
  const mode = pmf.indexOf(Math.max(...pmf));

  const tip = hi == null ? null : (<>
    <TipHead>k = {ks[hi]}</TipHead>
    <TipRow c={V.s1} l={`P(exactly ${ks[hi]})`} v={`${fmt(pmf[hi] * 100, 2)}%`} />
    <TipRow l={`P(≤ ${ks[hi]})`} v={`${fmt(atMost * 100, 2)}%`} />
    <TipRow l={`P(≥ ${ks[hi]})`} v={`${fmt(atLeast * 100, 2)}%`}
      tone={atLeast < 0.05 ? T.amber : T.dim} />
    {overlay && <TipRow c={V.s2} l={overlayLabel} v={fmt(overlay[hi], 4)} />}
    <div style={{ marginTop: 7, paddingTop: 6, borderTop: `1px solid ${T.lineSoft}`,
                  color: T.dim, fontSize: 10, lineHeight: 1.45 }}>
      {hi === mode ? `The most likely single outcome: ${ks[mode]} ${unit}.`
        : atLeast < 0.05 ? `Fewer than 1 in 20 samples reach this many ${unit}.`
        : `${fmt(atLeast * 100, 1)}% of samples land here or higher.`}
    </div>
  </>);

  const step = Math.max(1, Math.ceil(ks.length / 12));
  return (
    <ChartFrame tip={tip} W={W} H={H}
      tipAt={hi == null ? null : px(hi) + bw / 2}
      tipY={hi == null ? null : py(pmf[hi])}>
      <svg ref={ref} {...hoverProps} width="100%" viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: "100%", display: "block", cursor: "crosshair" }}
        role="img" aria-label="probability mass function">
        {[0.5, 1].map((f) => (
          <line key={f} x1={PL} y1={py(maxY * f)} x2={W - PR} y2={py(maxY * f)}
            stroke={V.grid} strokeWidth="1" />))}
        {pmf.map((v, i) => (
          <path key={i} d={barPath(px(i) + 1, py(v), bw, Math.max(0, (H - PB) - py(v)))}
            fill={V.s1} opacity={hi == null || hi === i ? 1 : 0.55} />))}
        {overlay && <path d={overlay.map((v, i) => `${i ? "L" : "M"}${px(i) + bw / 2},${py(v)}`).join(" ")}
          fill="none" stroke={V.s2} strokeWidth="2" strokeLinejoin="round" />}
        {hi != null && (
          <line x1={px(hi) + bw / 2} y1={PT} x2={px(hi) + bw / 2} y2={H - PB}
            stroke={T.text} strokeWidth="1" opacity="0.45" />)}
        <line x1={PL} y1={H - PB} x2={W - PR} y2={H - PB} stroke={T.line} />
        {ks.filter((_, i) => i % step === 0).map((k, j) => (
          <text key={k} x={px(j * step) + bw / 2} y={H - 22} textAnchor="middle" fontSize="9.5"
            fill={T.faint} fontFamily={MONO}>{k}</text>))}
        <text x={W / 2} y={H - 7} textAnchor="middle" fontSize="9.5" fill={T.faint}>
          number of {unit}
        </text>
      </svg>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 4, paddingLeft: PL / 2 }}>
        <LegendKey c={V.s1} shape="rect">Exact probability</LegendKey>
        {overlay && <LegendKey c={V.s2} shape="line">{overlayLabel}</LegendKey>}
      </div>
    </ChartFrame>);
}

function ProbabilityPanel({ d }) {
  if (!d) return <Panel title="Probability"><Empty text="unavailable" /></Panel>;
  const b = d.binomial, p = d.poisson;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title={`Binomial(n=${b.n}, p=${b.p})`}
        sub="the distribution of VaR exception counts — what Kupiec's test evaluates">
        <PmfChart ks={b.k} pmf={b.pmf} overlay={b.normal_approx} overlayLabel="normal approximation" />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 12, marginTop: 10 }}>
          <Big l="Mean (np)" v={b.mean} /><Big l="Variance np(1-p)" v={b.variance} />
          <Big l="Std dev" v={b.std_dev} />
        </div>
        <div style={{ ...S.alert, borderColor: b.normal_approx_valid ? T.green : T.amber, marginTop: 12 }}>
          <span style={{ ...S.pill, color: b.normal_approx_valid ? T.green : T.amber,
            borderColor: b.normal_approx_valid ? T.green : T.amber }}>
            {b.normal_approx_valid ? "APPROXIMATION VALID" : "APPROXIMATION UNSAFE"}</span>
          <span style={{ fontSize: 12.5 }}>{b.approx_note}</span>
        </div>
        <div style={{ fontSize: 12, color: T.dim, marginTop: 10 }}>{b.desk_meaning}</div>
      </Panel>
      <Panel title={`Poisson(λ=${p.lambda})`} sub="operational-loss frequency — mean equals variance">
        <PmfChart ks={p.k} pmf={p.pmf} />
        <div style={{ fontSize: 12, color: T.dim, marginTop: 10 }}>{p.desk_meaning}</div>
      </Panel>
    </div>);
}

function EstimationPanel({ d }) {
  if (!d || d.empty) return <Panel title="Estimation error"><Empty text={d?.message || "unavailable"} /></Panel>;
  const v = d.var_estimate, I = d.interpretation;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
      <Panel title="VaR is an estimate, not a measurement"
        sub={`bootstrapped from ${v.bootstrap_samples} resamples of the real series`}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginBottom: 12 }}>
          <Big l="VaR point estimate" v={v.point} suffix="%" accent={T.amber} />
          <Big l="95% CI lower" v={v.ci_95[0]} suffix="%" accent={T.red} />
          <Big l="95% CI upper" v={v.ci_95[1]} suffix="%" accent={T.green} />
        </div>
        <KVRow k="CI width as % of the estimate" v={v.relative_width_pct != null ? `${fmt(v.relative_width_pct, 0)}%` : null} tone={T.red} />
        <KVRow k="Observations in the tail" v={v.observations_in_tail}
          tone={v.observations_in_tail < 15 ? T.amber : T.text} />
        <KVRow k="Sample size" v={d.n} />
        <div style={{ ...S.alert, borderColor: T.red, marginTop: 12 }}>
          <span style={{ ...S.pill, color: T.red, borderColor: T.red }}>FALSE PRECISION</span>
          <span style={{ fontSize: 12.5 }}>{I.false_precision}</span>
        </div>
      </Panel>
      <Panel title="Sampling error on the inputs" sub="mean and volatility carry their own uncertainty">
        <KVRow k="Mean (daily)" v={d.mean.estimate} />
        <KVRow k="Standard error" v={d.mean.std_error} />
        <KVRow k="Mean ≠ 0 at 95%?" v={String(d.mean.significantly_nonzero)}
          tone={d.mean.significantly_nonzero ? T.green : T.amber} />
        <KVRow k="Ann. volatility" v={`${fmt(d.volatility.annual_pct, 2)}%`} tone={T.teal} />
        <KVRow k="Volatility 95% CI" v={`${fmt(d.volatility.annual_ci_pct[0], 2)}% – ${fmt(d.volatility.annual_ci_pct[1], 2)}%`} />
        <div style={{ fontSize: 12, color: T.dim, marginTop: 10, lineHeight: 1.6 }}>
          {I.central_limit}
        </div>
        <div style={{ fontSize: 12, color: T.amber, marginTop: 10, lineHeight: 1.6 }}>
          {I.why_it_matters}
        </div>
      </Panel>
    </div>);
}

/* ---------- RISK ANALYTIC PANELS ---------- */
// These were one "FRM Measures" screen — an exam-branded tab no desk has.
// The measures now sit where the decision is made: performance next to the
// book that produced it, regression diagnostics next to the factor model,
// credit capital next to the limits it consumes.
function KVRow({ k, v, tone, hint }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12,
      padding: "6px 0", borderBottom: `1px solid ${T.lineSoft}` }}>
      <span style={{ color: T.dim, fontSize: 12 }}>{k}
        {hint && <span style={{ color: T.faint, fontSize: 10.5 }}> · {hint}</span>}</span>
      <span style={{ fontFamily: MONO, color: tone || T.text, whiteSpace: "nowrap" }}>
        {v ?? "—"}</span></div>);
}

// Risk-adjusted performance of the live book. Belongs beside the blotter:
// it is the scorecard for the positions listed directly above it.
function PerformancePanel() {
  const [d, setD] = useState(null);
  useEffect(() => {
    fetch("/api/frm/performance?days=500&rf_annual=0.04")
      .then((r) => r.ok ? r.json() : null).then(setD).catch(() => setD(null));
  }, []);
  if (!d) return null;
  if (d.empty) return <Panel title="Risk-adjusted performance"><Empty text={d.message} /></Panel>;
  return (
    <Panel title="Risk-adjusted performance"
      sub={d.book ? `${d.book.n} trading days · ${d.book.dates?.[0]} → ${d.book.dates?.[1]}` : ""}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <div>
          <KVRow k="Sharpe" v={d.sharpe} hint="÷ total vol" tone={T.teal} />
          <KVRow k="Sortino" v={d.sortino} hint="÷ downside deviation" tone={T.teal} />
          <KVRow k="Calmar" v={d.calmar} hint="CAGR ÷ |max DD|" />
          <KVRow k="Max drawdown" v={d.max_drawdown_pct != null ? `${fmt(d.max_drawdown_pct, 2)}%` : null} tone={T.red} />
          <KVRow k="Annual vol" v={d.annual_vol_pct != null ? `${fmt(d.annual_vol_pct, 2)}%` : null} />
        </div>
        <div>
          <KVRow k="Beta vs S&P 500" v={d.beta} tone={T.blue} />
          <KVRow k="R² vs benchmark" v={d.r_squared_vs_benchmark}
            tone={(d.r_squared_vs_benchmark ?? 1) < 0.25 ? T.amber : T.green} />
          <KVRow k="Treynor" v={d.treynor} hint="÷ beta"
            tone={d.treynor_warning ? T.amber : T.text} />
          <KVRow k="Jensen's α" v={d.jensens_alpha_pct != null ? `${fmt(d.jensens_alpha_pct, 2)}%` : null}
            tone={d.treynor_warning ? T.amber : T.green} />
          <KVRow k="Information ratio" v={d.information_ratio} hint="÷ tracking error" />
        </div>
      </div>
      {d.treynor_warning && (
        <div style={{ ...S.alert, borderColor: T.amber, marginTop: 10 }}>
          <span style={{ ...S.pill, color: T.amber, borderColor: T.amber }}>QUALIFIED</span>
          <span style={{ fontSize: 12.5 }}>{d.treynor_warning}</span></div>)}
    </Panel>);
}

// Regression and time-series diagnostics — mounted on Factor & Sensitivity,
// because they are the tests that say whether the factor model above them can
// be believed at all.
function RegressionDiagnostics() {
  const [d, setD] = useState({});
  useEffect(() => {
    const get = (u) => fetch(u).then((r) => r.ok ? r.json() : null).catch(() => null);
    Promise.all([get("/api/frm/regression?days=500"), get("/api/frm/timeseries?days=500"),
                 get("/api/frm/hypothesis-tests?days=500")])
      .then(([reg, ts, tests]) => setD({ reg, ts, tests }));
  }, []);
  const { reg, ts, tests } = d;
  if (!reg || reg.empty) return null;
  const slope = reg.coefficients?.[1];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
      <Panel title="Regression diagnostics" sub="book regressed on the benchmark — can this model be believed?">
        <KVRow k="R² / adjusted R²" v={`${fmt(reg.r_squared, 4)} / ${fmt(reg.adj_r_squared, 4)}`} />
        <KVRow k="F-statistic" v={reg.f_stat} hint={`p = ${fmt(reg.f_p_value, 5)}`} />
        <KVRow k="Slope (beta)" v={slope?.coefficient} tone={T.blue} />
        <KVRow k="Slope t-stat" v={slope?.t_stat}
          tone={slope?.significant_5pct ? T.green : T.amber}
          hint={slope?.significant_5pct ? "significant at 5%" : "not significant"} />
        <KVRow k="Breusch-Pagan p" v={reg.breusch_pagan?.p_value}
          tone={reg.breusch_pagan?.heteroskedastic_5pct ? T.red : T.green} />
        {reg.multicollinearity_flag && (
          <KVRow k="VIF flagged" v={reg.multicollinearity_flag.join(", ")} tone={T.red} />)}
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8 }}>{reg.diagnostics_note}</div>
      </Panel>
      <Panel title="Stationarity & correlation" sub="AR(1) plus linear vs rank association">
        <KVRow k="AR(1) φ — returns" v={ts?.ar1_returns?.phi} />
        <KVRow k="Stationary — returns" v={String(ts?.ar1_returns?.covariance_stationary ?? "—")}
          tone={ts?.ar1_returns?.covariance_stationary ? T.green : T.amber} />
        <KVRow k="AR(1) φ — equity curve" v={ts?.ar1_level?.phi} />
        <KVRow k="Near unit root" v={String(ts?.ar1_level?.near_unit_root ?? "—")}
          tone={ts?.ar1_level?.near_unit_root ? T.amber : T.green} />
        <KVRow k="Pearson / Spearman" v={ts?.rank_correlation_vs_benchmark
          ? `${fmt(ts.rank_correlation_vs_benchmark.pearson, 3)} / ${fmt(ts.rank_correlation_vs_benchmark.spearman_rho, 3)}` : null} />
        <KVRow k="Kendall τ" v={ts?.rank_correlation_vs_benchmark?.kendall_tau} />
        <KVRow k="t-test mean = 0" v={tests?.t_test_mean_return?.t_stat}
          hint={`p = ${fmt(tests?.t_test_mean_return?.p_value, 4)}`} />
        {ts?.ar1_level?.warning && (
          <div style={{ fontSize: 11.5, color: T.amber, marginTop: 8 }}>{ts.ar1_level.warning}</div>)}
      </Panel>
    </div>);
}

// Credit loss and regulatory capital — mounted on Limits & Alerts, because
// capital is a limit: it is the balance-sheet constraint the desk consumes.
function CreditCapital() {
  const [d, setD] = useState(null);
  const [p, setP] = useState({ ead: 1000000, pd: 0.02, lgd: 0.45, lgd_volatility: 0.25 });
  const load = async (params) => {
    try {
      const r = await fetch("/api/frm/credit", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) });
      setD(r.ok ? await r.json() : null);
    } catch { setD(null); }
  };
  useEffect(() => { load(p); /* eslint-disable-next-line */ }, [p]);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
      <Panel title="Expected vs unexpected loss" sub="the distinction capital rests on">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginBottom: 12 }}>
          {[["EAD", "ead", 100000], ["PD", "pd", 0.005], ["LGD", "lgd", 0.05],
            ["σ LGD", "lgd_volatility", 0.05]].map(([l, k, st]) => (
            <label key={k} style={{ display: "grid", gap: 3 }}>
              <span style={{ color: T.dim, fontSize: 11 }}>{l}</span>
              <input type="number" value={p[k]} step={st} style={{ ...S.num, padding: "6px 8px" }}
                onChange={(e) => setP({ ...p, [k]: parseFloat(e.target.value) || 0 })} /></label>))}
        </div>
        <KVRow k="Expected loss" v={d?.loss?.expected_loss != null ? `$${fmt(d.loss.expected_loss, 0)}` : null} tone={T.amber} />
        <KVRow k="Unexpected loss" v={d?.loss?.unexpected_loss != null ? `$${fmt(d.loss.unexpected_loss, 0)}` : null} tone={T.red} />
        <KVRow k="UL ÷ EL" v={d?.loss?.ul_to_el_ratio} tone={T.red} />
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8 }}>
          EL is a cost of doing business — priced into the spread and provisioned for.
          UL is the volatility around it, and that is what <b>capital</b> absorbs.
          Reserving only for EL leaves the firm undercapitalised for exactly the
          outcomes that matter.
        </div>
      </Panel>
      <Panel title="Basel IRB capital" sub="Vasicek single-factor · 99.9% · ρ = 0.15">
        <KVRow k="Unconditional PD" v={d?.basel_irb?.unconditional_pd} />
        <KVRow k="Conditional PD (stressed)" v={d?.basel_irb?.conditional_pd} tone={T.red} />
        <KVRow k="Stress multiple" v={d?.basel_irb?.stress_multiple != null ? `${fmt(d.basel_irb.stress_multiple, 2)}×` : null} tone={T.amber} />
        <KVRow k="Maturity adjustment" v={d?.basel_irb?.maturity_adjustment} />
        <KVRow k="Capital requirement" v={d?.basel_irb?.capital_amount != null ? `$${fmt(d.basel_irb.capital_amount, 0)}` : null} tone={T.blue} />
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 8 }}>
          Capital is charged on conditional PD <b>minus</b> PD — the expected part is
          already provisioned, so charging the full conditional loss double-counts it.
        </div>
      </Panel>
    </div>);
}

/* ---------- MARKET RISK ---------- */
function MarketRisk() {
  // Cross-method VaR on the live book. This replaced a hardcoded table that
  // quoted "$5.14M / Monte Carlo (C++, 1M) impl=cpp" for numbers that had
  // never been computed -- a specific, checkable false claim.
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/portfolio/var-suite?days=500&confidence=0.99");
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  if (d?.empty) return <Panel title="Market risk"><Empty text={d.message} /></Panel>;
  const rows = d?.methods || [];
  const bt = d?.backtest;
  const zone = { GREEN: T.green, YELLOW: T.amber, RED: T.red }[bt?.basel_zone] || T.dim;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Value-at-Risk / Expected Shortfall"
        sub={d ? `${d.n_observations} obs · ${d.alignment} · gross ${fmt(d.gross_exposure, 0)}` : "—"}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
          <button onClick={load} disabled={loading} style={{ ...S.seg, ...S.segOn, padding: "6px 14px" }}>
            {loading ? "…" : "Recompute"}</button>
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
        {rows.length ? (
          <table style={S.table}><thead><tr>
            {["Method", "VaR 99% (%)", "VaR 95% (%)", "ES (%)", "VaR (ccy)", "ES (ccy)"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: h === "Method" ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{rows.map((r) => (
            <tr key={r.method} className="tr">
              <td style={S.td}>{r.method.replace(/_/g, " ")}</td>
              <td style={{ ...S.tdN, color: T.amber }}>{r.var_pct != null ? fmt(r.var_pct, 3) : "—"}</td>
              <td style={S.tdN}>{r.var95_pct != null ? fmt(r.var95_pct, 3) : "—"}</td>
              <td style={{ ...S.tdN, color: T.red }}>{r.es_pct != null ? fmt(r.es_pct, 3) : "—"}</td>
              <td style={S.tdN}>{r.var_currency != null ? fmt(r.var_currency, 2) : "—"}</td>
              <td style={S.tdN}>{r.es_currency != null ? fmt(r.es_currency, 2) : "—"}</td>
            </tr>))}</tbody></table>) : <Empty text={loading ? "computing…" : "—"} />}
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
          All methods run on the SAME return series, so the spread between them IS the model
          risk. Parametric assumes normality and understates the tail; historical makes no
          distributional assumption; Cornish-Fisher adjusts for skew and kurtosis.
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="VaR backtest" sub="Kupiec POF · Christoffersen independence & CC · Basel zone">
          {bt ? (
            <div style={{ display: "grid", gap: 9 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 4 }}>
                <span style={{ ...S.pill, color: zone, borderColor: zone, fontSize: 12, padding: "4px 12px" }}>
                  BASEL {bt.basel_zone}</span>
                <span style={{ color: T.dim, fontSize: 12 }}>
                  {bt.exceptions} exceptions in {bt.observations} (expected {fmt(bt.expected, 1)})</span>
              </div>
              <KV k="Kupiec POF p-value" v={fmt(bt.kupiec_p, 4)}
                tone={bt.kupiec_p < 0.05 ? T.red : T.green} />
              <KV k="Christoffersen independence p" v={fmt(bt.christoffersen_ind_p, 4)}
                tone={bt.christoffersen_ind_p < 0.05 ? T.red : T.green} />
              <KV k="Christoffersen CC p" v={fmt(bt.christoffersen_cc_p, 4)}
                tone={bt.christoffersen_cc_p < 0.05 ? T.red : T.green} />
              <div style={{ fontSize: 11.5, color: T.dim }}>
                Kupiec tests whether the COUNT of breaches matches the confidence level;
                Christoffersen tests whether they CLUSTER. A model can pass the count and
                still fail badly by breaching several days in a row.
              </div>
            </div>) : <Empty text="needs 150+ observations for a rolling backtest" />}
        </Panel>

        <Panel title="Distribution & conditional volatility" sub="why one VaR number is not enough">
          <div style={{ display: "grid", gap: 9 }}>
            <KV k="Ann. volatility" v={d?.moments ? `${fmt(d.moments.vol_annual_pct, 2)}%` : "—"} />
            <KV k="Skew" v={d?.moments?.skew ?? "—"}
              tone={(d?.moments?.skew ?? 0) < 0 ? T.amber : T.text} />
            <KV k="Excess kurtosis" v={d?.moments?.excess_kurtosis ?? "—"}
              tone={(d?.moments?.excess_kurtosis ?? 0) > 1 ? T.red : T.green} />
            <KV k="EWMA σ (λ=0.94)" v={d?.ewma ? `${fmt(d.ewma.sigma_annual_pct, 2)}%` : "—"} />
            <KV k="GARCH persistence" v={d?.garch?.persistence ?? "—"}
              tone={(d?.garch?.persistence ?? 0) >= 1 ? T.red : T.green} />
            <KV k="GARCH current σ" v={d?.garch ? `${fmt(d.garch.current_vol_annual_pct, 2)}%` : "—"} />
            <div style={{ fontSize: 11.5, color: T.dim }}>
              Excess kurtosis above 0 means fatter tails than normal — exactly the condition
              under which the parametric VaR above understates risk.
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}

/* ---------- PRICING incl. LSM ---------- */
// Every price here now comes from the backend engine. This screen used to run
// Black-Scholes and a binomial tree in browser JavaScript, then label the
// output "LSM (C++, 200k×50)" -- and the American and "LSM" panels rendered
// the SAME JavaScript variable under two different engine names. The compiled
// Longstaff-Schwartz binding existed the whole time and was never called.
// `impl` and `latency_ms` below are reported by the API, so the engine named
// is the engine that ran.
function Pricing() {
  const [tab, setTab] = useState("euro");
  const [p, setP] = useState({ spot: 36, strike: 40, maturity: 1, rate: 0.06,
                               dividend: 0, vol: 0.20 });
  const [call, setCall] = useState(false);
  const [res, setRes] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const price = async () => {
    setBusy(true); setErr(null);
    const body = (exercise) => JSON.stringify({ ...p, kind: call ? "call" : "put", exercise });
    try {
      const post = (u, ex) => fetch(u, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: body(ex) })
        .then((r) => r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.detail || r.status))));
      const [bsm, lsm, binA, binE] = await Promise.all([
        post("/api/pricing/black-scholes", "european"),
        post("/api/pricing/lsm?paths=200000&steps=50", "american"),
        post("/api/pricing/binomial", "american"),
        post("/api/pricing/binomial", "european"),
      ]);
      setRes({ bsm, lsm, binA, binE });
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };
  useEffect(() => { price(); /* eslint-disable-next-line */ }, []);

  const g = res.bsm?.greeks || {};
  const inputs = [["Spot S", "spot", 1], ["Strike K", "strike", 1], ["Maturity T", "maturity", 0.1],
                  ["Rate r", "rate", 0.005], ["Div q", "dividend", 0.005], ["Vol σ", "vol", 0.01]];

  // Greeks must sit inside their analytic bounds. Delta outside (-1, 1) or a
  // negative gamma/vega on a vanilla is a broken engine, not a market view.
  const deltaOK = g.delta == null || (g.delta > -1 && g.delta < 1);
  const gammaOK = g.gamma == null || g.gamma >= 0;
  const vegaOK = g.vega == null || g.vega >= 0;

  const Impl = ({ r }) => r ? (
    <span style={{ ...S.pill, color: r.meta?.impl === "cpp" ? T.teal : T.amber,
      borderColor: r.meta?.impl === "cpp" ? T.teal : T.amber, marginLeft: 6 }}>
      {r.meta?.impl || "?"} · {fmt(r.latency_ms, 2)}ms</span>) : null;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 14 }}>
      <Panel title="Contract" sub="priced server-side by the selected engine">
        <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
          {["Call", "Put"].map((o, i) => (
            <button key={o} onClick={() => setCall(i === 0)}
              style={{ ...S.seg, ...((i === 0) === call ? S.segOn : null) }}>{o}</button>))}
        </div>
        <div style={{ display: "grid", gap: 10 }}>
          {inputs.map(([l, k, st]) => (
            <label key={k} style={{ display: "grid", gap: 4 }}>
              <span style={{ color: T.dim, fontSize: 11.5 }}>{l}</span>
              <input type="number" value={p[k]} step={st} style={S.num}
                onChange={(e) => setP({ ...p, [k]: parseFloat(e.target.value) || 0 })} /></label>))}
        </div>
        <button onClick={price} disabled={busy}
          style={{ ...S.seg, ...S.segOn, marginTop: 12, padding: "9px" }}>
          {busy ? "Pricing…" : "Reprice"}</button>
        {err && <div style={{ color: T.red, fontSize: 12, marginTop: 8 }}>{err}</div>}
      </Panel>

      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", gap: 6 }}>
          {[["euro", "European (BSM)"], ["amer", "American (LSM)"]].map(([id, l]) => (
            <button key={id} onClick={() => setTab(id)}
              style={{ ...S.tab, ...(tab === id ? S.tabOn : null) }}>{l}</button>))}
        </div>

        {tab === "euro" && (
          <>
            <Panel title={<>European price & Greeks <Impl r={res.bsm} /></>}
              sub="closed-form Black-Scholes-Merton">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginBottom: 12 }}>
                <Big l="Price" v={res.bsm?.price ?? 0} accent={T.teal} />
                <Big l="Binomial (European)" v={res.binE?.price ?? 0} />
                <Big l="Tree vs closed-form" v={res.bsm && res.binE
                  ? Math.abs(res.bsm.price - res.binE.price) : 0} />
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 10 }}>
                {[["Δ delta", g.delta, deltaOK], ["Γ gamma", g.gamma, gammaOK],
                  ["ν vega", g.vega, vegaOK], ["Θ theta", g.theta, true],
                  ["ρ rho", g.rho, true]].map(([l, v, ok]) => (
                  <Big key={l} l={l} v={v ?? 0} accent={ok ? undefined : T.red} />))}
              </div>
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
                Bounds check: |Δ| &lt; 1 {deltaOK ? "✓" : "✗"} · Γ ≥ 0 {gammaOK ? "✓" : "✗"} ·
                ν ≥ 0 {vegaOK ? "✓" : "✗"}. A vanilla delta outside (−1, 1), or negative
                gamma or vega, is a broken engine rather than a market view — so the
                bound is asserted here rather than assumed.
              </div>
            </Panel>
          </>)}

        {tab === "amer" && (
          <>
            <Panel title={<>American price — Longstaff-Schwartz <Impl r={res.lsm} /></>}
              sub="regression exercise boundary · cross-checked against the binomial tree">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
                <Big l={`LSM (${res.lsm?.meta?.paths?.toLocaleString() || "—"}×${res.lsm?.meta?.steps || "—"})`}
                  v={res.lsm?.price ?? 0} accent={T.teal} />
                <Big l="Binomial American" v={res.binA?.price ?? 0} />
                <Big l="European control" v={res.lsm?.meta?.european ?? res.bsm?.price ?? 0} />
                <Big l="Early-exercise premium" v={res.lsm?.meta?.early_exercise_premium ?? 0}
                  accent={T.amber} />
              </div>
              <div style={{ display: "grid", gap: 7, marginTop: 12 }}>
                <KV k="LSM vs binomial" v={res.lsm && res.binA
                  ? fmt(Math.abs(res.lsm.price - res.binA.price), 4) : "—"}
                  tone={res.lsm && res.binA && Math.abs(res.lsm.price - res.binA.price) < 0.01
                    ? T.green : T.amber} />
                <KV k="Monte Carlo std error" v={res.lsm?.stderr != null ? fmt(res.lsm.stderr, 5) : "—"} />
              </div>
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
                The <b>early-exercise premium</b> is the self-check that matters: if LSM and
                its own European control converge to the same number, the regression never
                found an exercise boundary worth taking and the "American" price is European
                in all but name. Two independent methods — a lattice and a regression Monte
                Carlo — agreeing to within the MC standard error is what makes either
                trustworthy.
              </div>
            </Panel>
          </>)}
      </div>
    </div>
  );
}

/* ---------- CRYPTO DESK ---------- */
// Previously an invented position (entry 2.10, mark 2.02, 40,000 units,
// funding 0.0001) rendered under a panel subtitled "isolated margin · live".
// The maths was right and the inputs were fiction, which is the combination
// that reads as real. It now marks against a genuinely live consolidated mid
// and runs the margin/liquidation maths in the backend engine.
function CryptoDesk({ tab }) {
  const [pair, setPair] = useState("XRPUSDT");
  const [marks, setMarks] = useState([]);
  const [entry, setEntry] = useState(null);
  const [qty, setQty] = useState(40000);
  const [lev, setLev] = useState(10);
  const [margin, setMargin] = useState(null);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const r = await fetch("/api/marketdata/board?days=30");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const cr = (d.rows || []).filter((x) => x.asset_class === "CRYPTO" && x.status === "ok");
      setMarks(cr);
      const m = cr.find((c) => c.symbol === pair);
      // Seed entry at the live mark on first load so the position starts flat
      // rather than at a fabricated P&L.
      if (m && entry == null) setEntry(m.last);
    } catch (e) { setErr(String(e.message || e)); }
  };
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t);
    /* eslint-disable-next-line */ }, [pair]);

  const mark = marks.find((c) => c.symbol === pair)?.last ?? null;

  useEffect(() => {
    (async () => {
      if (!mark || !entry) return;
      try {
        const r = await fetch("/api/crypto/margin", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entry: Number(entry), mark, qty: Number(qty),
                                 leverage: Number(lev), is_long: true,
                                 maintenance_margin_rate: 0.005 }) });
        setMargin(r.ok ? await r.json() : null);
      } catch { setMargin(null); }
    })();
  }, [mark, entry, qty, lev]);

  const upnl = mark && entry ? (mark - entry) * qty : 0;
  const notional = mark ? mark * qty : 0;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title={tab === "liq" ? "Liquidation Risk" : "Perpetual Futures — Funding & Carry"}
        sub={mark ? `${pair} · live consolidated mid across public venues · marked ${fmt(mark, 4)}`
                  : "loading live venue quotes…"}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
          {marks.map((c) => (
            <button key={c.symbol} onClick={() => { setPair(c.symbol); setEntry(null); }}
              style={{ ...S.seg, ...(pair === c.symbol ? S.segOn : {}), flex: "0 0 auto", padding: "6px 12px" }}>
              {c.symbol} {fmt(c.last, 2)}</button>))}
          {!marks.length && <span style={{ color: T.dim, fontSize: 12 }}>no live venue quote available</span>}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
          <Big l="Mark (live)" v={mark ?? 0} accent={T.teal} />
          <Big l="Notional" v={notional} prefix="$" />
          <Big l="Position uPnL" v={upnl} prefix="$" accent={upnl >= 0 ? T.green : T.red} />
          <Big l="Leverage" v={lev} suffix="×" />
        </div>
        {err && <div style={{ color: T.red, fontSize: 12, marginTop: 8 }}>{err}</div>}
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="Position" sub="entry seeded at the live mark — adjust to model a P&L">
          <div style={{ display: "grid", gap: 10 }}>
            {[["Entry", entry ?? 0, setEntry, 0.01], ["Quantity", qty, setQty, 1000],
              ["Leverage", lev, setLev, 1]].map(([l, v, set, st]) => (
              <label key={l} style={{ display: "grid", gridTemplateColumns: "88px 1fr",
                alignItems: "center", gap: 8 }}>
                <span style={{ color: T.dim, fontSize: 12 }}>{l}</span>
                <input type="number" value={v} step={st}
                  onChange={(e) => set(+e.target.value || 0)} style={S.num} /></label>))}
          </div>
        </Panel>

        <Panel title="Margin & liquidation" sub="computed by /api/crypto/margin · isolated margin, 50bp MMR">
          {margin ? (
            <div style={{ display: "grid", gap: 8 }}>
              {/* Field names follow MarginState exactly: `distance_to_liq` is a
                  signed FRACTION (not a percent, and negative for a long, whose
                  liquidation sits below the mark), and the P&L field is spelled
                  `unrealised_pnl`. Reading `unrealized_pnl` and a
                  `maintenance_margin` the API never returned is what blanked
                  this screen; `equity` is what the payload actually carries. */}
              <KV k="Liquidation price" v={fmt(margin.liquidation_price, 4)} tone={T.red} />
              <KV k="Bankruptcy price" v={fmt(margin.bankruptcy_price, 4)} tone={T.red} />
              <KV k="Distance to liquidation" v={margin.distance_to_liq != null
                ? `${fmt(Math.abs(margin.distance_to_liq) * 100, 2)}%` : "—"}
                tone={Math.abs(margin.distance_to_liq ?? 1) * 100 < 10 ? T.red : T.green} />
              <KV k="Margin ratio" v={margin.margin_ratio != null
                ? `${fmt(margin.margin_ratio * 100, 2)}%` : "—"}
                tone={(margin.margin_ratio ?? 0) >= 1 ? T.red : T.dim} />
              <KV k="Equity" v={fmt(margin.equity, 2)} />
              <KV k="Unrealised P&L" v={fmt(margin.unrealised_pnl, 2)}
                tone={(margin.unrealised_pnl ?? 0) < 0 ? T.red : T.green} />
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 6 }}>
                Liquidation happens BEFORE bankruptcy: the exchange closes the position
                while equity still covers the maintenance margin, so the gap between the
                two prices is the venue's buffer, not the trader's.
              </div>
            </div>) : <Empty text="waiting for a live mark" />}
        </Panel>
      </div>
    </div>
  );
}

/* ---------- OBSERVABILITY ---------- */
function Observability() {
  // Reads /api/obs/profiling and /api/obs/speedup -- the real timed() seam
  // that also feeds Prometheus. The engine badge comes from /api/health, so
  // this screen can never claim a C++ path the deployment does not have.
  const [rows, setRows] = useState([]);
  const [speedup, setSpeedup] = useState({});
  const [health, setHealth] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const [p, s2, h] = await Promise.all([
        fetch("/api/obs/profiling").then((r) => r.json()),
        fetch("/api/obs/speedup").then((r) => r.json()),
        fetch("/api/health").then((r) => r.json()),
      ]);
      setRows(Array.isArray(p) ? p : []); setSpeedup(s2 || {}); setHealth(h);
      setErr(null);
    } catch (e) { setErr(e.message); } finally { setLoading(false); }
  };
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, []);

  const maxP99 = Math.max(...rows.map((r) => r.p99_ms), 0.001);
  const implColor = { cpp: T.green, numpy: T.blue, python: T.violet };
  const cppAvailable = health?.engines?.cpp_available;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Engine latency — live profiling"
        sub="p50 / p95 / p99 measured at the timed() seam · the same histogram Prometheus scrapes">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
          <button onClick={load} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "6px 14px" }}>{loading ? "…" : "Refresh"}</button>
          <span style={{ ...S.pill, color: cppAvailable ? T.green : T.amber,
            borderColor: cppAvailable ? T.green : T.amber }}>
            {cppAvailable ? "C++ EXTENSION LOADED" : "NUMPY ONLY — C++ EXTENSION NOT BUILT"}
          </span>
          {health && <span style={{ color: T.dim, fontSize: 12 }}>
            active default: <b style={{ fontFamily: MONO, color: T.text }}>{health.engines?.active_default}</b></span>}
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
        {rows.length ? (
          <table style={S.table}><thead><tr>{["Engine", "impl", "calls", "p50 (ms)", "p95 (ms)", "p99 (ms)", "distribution"].map((h) =>
            <th key={h} style={{ ...S.th, textAlign: ["Engine", "impl", "distribution"].includes(h) ? "left" : "right" }}>{h}</th>)}</tr></thead>
            <tbody>{rows.map((r, i) => (
              <tr key={i} className="tr">
                <td style={S.td}>{r.engine}</td>
                <td style={S.td}><span style={{ ...S.pill, color: implColor[r.impl] || T.dim,
                  borderColor: implColor[r.impl] || T.line, fontFamily: MONO }}>{r.impl}</span></td>
                <td style={{ ...S.tdN, color: T.dim }}>{r.count}</td>
                <td style={S.tdN}>{fmt(r.p50_ms, 4)}</td>
                <td style={S.tdN}>{fmt(r.p95_ms, 4)}</td>
                <td style={{ ...S.tdN, color: r.p99_ms > 10 ? T.amber : T.text }}>{fmt(r.p99_ms, 4)}</td>
                <td style={{ ...S.td, width: "24%" }}>
                  <DistBar p50={r.p50_ms} p95={r.p95_ms} p99={r.p99_ms} max={maxP99}
                    c={implColor[r.impl] || T.dim} /></td>
              </tr>))}</tbody></table>
        ) : (
          <Empty text={loading ? "loading…" : "No engine calls recorded yet — the profiler fills as endpoints are exercised. Price a bond on Rates & Fixed Income, or route an order, then refresh."} />
        )}
      </Panel>

      <EngineBench />

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <EngineCompare cppAvailable={cppAvailable} />
        <Panel title="Prometheus / Grafana" sub="the same seam, exported for dashboards">
          <div style={{ display: "grid", gap: 10 }}>
            <KV k="Metrics endpoint" v="/metrics" />
            <KV k="Scrape target" v="api:8000" />
            <div>
              <div style={{ color: T.dim, fontSize: 11 }}>Grafana</div>
              <a href="https://grafana.quantgenlabs.com" target="_blank" rel="noopener noreferrer"
                 style={{ fontFamily: MONO, fontSize: 15, color: T.teal,
                          textDecoration: "none", display: "inline-flex",
                          alignItems: "center", gap: 4 }}>
                grafana.quantgenlabs.com <ArrowUpRight size={12} /></a>
            </div>
            <div style={{ fontSize: 11.5, color: T.dim }}>
              One timed(engine, impl) call site feeds both this table and the Prometheus
              histogram, so the console and Grafana cannot disagree.
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}


// The profiler-derived speedup only has a ratio when BOTH impls have been
// exercised, and in normal operation the selector always picks C++ -- so that
// panel was permanently empty. This runs both paths explicitly instead.
function EngineCompare({ cppAvailable }) {
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setD(await (await fetch("/api/bench/engines?paths=1000000&repeat=3")).json()); }
    catch (e) { setD({ cpp_available: false, reason: String(e) }); }
    finally { setBusy(false); }
  };
  return (
    <Panel title="C++ vs numpy — head to head"
      sub="same inputs, best-of-N wall clock, prices cross-checked">
      <button onClick={run} disabled={busy || !cppAvailable}
        style={{ ...S.seg, ...S.segOn, padding: "6px 14px", marginBottom: 12,
                 opacity: cppAvailable ? 1 : 0.5 }}>
        {busy ? "Benchmarking…" : "Run comparison"}</button>
      {!cppAvailable && <Empty text="C++ extension not built in this deployment — every engine runs on numpy, so there is nothing to compare." />}
      {d && !d.cpp_available && <Empty text={`unavailable: ${d.reason || "not built"}`} />}
      {d?.cpp_available && (
        <>
          <table style={S.table}><thead><tr>
            {["Engine", "C++ (ms)", "numpy (ms)", "Speedup", "Price check"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: h === "Engine" ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{d.results.map((r) => (
            <tr key={r.engine} className="tr">
              <td style={S.td}>{r.engine}</td>
              <td style={{ ...S.tdN, color: T.green }}>{fmt(r.cpp_ms, 3)}</td>
              <td style={{ ...S.tdN, color: T.blue }}>{fmt(r.numpy_ms, 3)}</td>
              <td style={{ ...S.tdN, color: r.speedup >= 2 ? T.green : T.amber }}>{fmt(r.speedup, 2)}×</td>
              <td style={{ ...S.tdN, color: r.price_diff < 0.01 ? T.green : T.dim }}>
                {r.engine === "monte_carlo" ? "RNG differs" : `Δ ${r.price_diff.toExponential(1)}`}</td>
            </tr>))}</tbody></table>
          <div style={{ marginTop: 10, fontSize: 11.5, color: T.dim }}>
            {d.method}. {d.note}
          </div>
        </>)}
    </Panel>
  );
}

/* ---------- COPILOT ---------- */
function Copilot() {
  // Calls POST /api/copilot, which grounds Claude in the live OMS blotter,
  // risk-limit utilisation, TCA and market data. Server-side it falls back to
  // a deterministic local summary when no API key is configured.
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const suggestions = [
    "What is my current exposure and P&L?",
    "How close am I to any risk limit?",
    "How good was my execution — which algo is cheaper?",
    "What does the yield curve imply about rate expectations?",
  ];

  const send = async (q) => {
    const question = (q ?? input).trim();
    if (!question || busy) return;
    setInput(""); setErr(null);
    const next = [...msgs, { role: "user", content: question }];
    setMsgs(next); setBusy(true);
    try {
      const r = await fetch("/api/copilot", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: next.map((m) => ({ role: m.role, content: m.content })),
          include_desk_state: true,
        }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const d = await r.json();
      setMsgs((m) => [...m, { role: "assistant", content: d.content,
                              model: d.model, grounded: d.grounded }]);
    } catch (e) {
      setErr(String(e.message || e));
      setMsgs((m) => m.slice(0, -1));
    } finally { setBusy(false); }
  };

  return (
    <div style={{ maxWidth: 820, margin: "0 auto", display: "flex", flexDirection: "column", height: "100%" }}>
      <Panel title="Risk copilot" sub="Claude, grounded in the live blotter, limit utilisation, TCA and market data">
        <div style={{ display: "grid", gap: 10, minHeight: 300 }}>
          {msgs.length === 0 && (
            <div style={{ display: "grid", gap: 8 }}>
              <div style={{ color: T.dim, fontSize: 12.5 }}>
                Ask about the book. The model receives the actual OMS state — if the blotter
                is empty it will say so rather than invent positions.
              </div>
              {suggestions.map((s2) => (
                <button key={s2} onClick={() => send(s2)} style={S.chip}>{s2}</button>))}
            </div>)}
          {msgs.map((m, i) => (
            <div key={i} style={{ display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
              <div style={m.role === "user" ? S.bubbleU : S.bubbleB}>
                <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
                {m.model && <div style={{ marginTop: 6, fontSize: 10, color: T.faint, fontFamily: MONO }}>
                  {m.model}{m.grounded ? " · grounded" : ""}</div>}
              </div>
            </div>))}
          {busy && <div style={{ color: T.dim, fontSize: 12.5 }}>thinking…</div>}
          {err && <div style={{ color: T.red, fontSize: 12.5 }}>copilot failed: {err}</div>}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12, borderTop: `1px solid ${T.line}`, paddingTop: 12 }}>
          <input placeholder="Ask the risk copilot…" style={{ ...S.num, flex: 1 }}
            value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <button style={S.sendBtn} onClick={() => send()} disabled={busy}>
            {busy ? "…" : "Send"}</button></div>
      </Panel>
    </div>
  );
}

/* ---------- SMART ORDER ROUTING (primary) ---------- */
const VENUE_COLORS = [T.blue, T.teal, T.violet, T.amber, T.green, T.red];
const venueColor = (name, i) => VENUE_COLORS[i % VENUE_COLORS.length];

// Every number on this screen comes from POST /api/sor/route -- the same
// engine backend/run_sor.py drives. `live` routes across real Binance/OKX/
// Kraken/KuCoin books via CCXT (open-source, public books, no API keys);
// with it off the backend uses its calibrated synthetic market so the screen
// still works when an exchange is unreachable.
function SmartOrderRouting() {
  const [side, setSide] = useState("buy");
  const [qty, setQty] = useState(0.5);
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [useML, setUseML] = useState(true);
  const [live, setLive] = useState(false);
  const [res, setRes] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const run = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch("/api/sor/route", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ side, qty, symbol, use_ml: useML, live }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRes(await r.json());
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  // Route once on mount (synthetic = fast) so the screen is never empty.
  useEffect(() => { run(); /* eslint-disable-next-line */ }, []);

  const venues = res?.venues ?? [];
  const alloc = res?.allocation ?? {};
  const fillP = res?.fill_probabilities ?? {};
  const ml = res?.ml_metrics ?? null;
  const bench = res?.benchmarks_bps ?? {};
  const maxAlloc = Math.max(0, ...Object.values(alloc));
  const cidx = Object.fromEntries(venues.map((v, i) => [v.venue, venueColor(v.venue, i)]));
  const isLive = res?.data_source === "ccxt-live";
  const partial = res && res.filled_qty + 1e-9 < res.target_qty;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Smart Order Routing — best execution across venues"
        sub="marginal-cost allocation · ML fill probability · live books via CCXT">
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: 6 }}>
            {["Buy", "Sell"].map((o) => <button key={o} onClick={() => setSide(o.toLowerCase())}
              style={{ ...S.seg, padding: "7px 14px", ...(side === o.toLowerCase() ? S.segOn : null) }}>{o}</button>)}
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Symbol</span>
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)}
              style={{ ...S.num, width: 120 }} /></label>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Order qty</span>
            <input type="number" value={qty} step={0.1} onChange={(e) => setQty(+e.target.value || 0)}
              style={{ ...S.num, width: 110 }} /></label>
          <button onClick={() => setUseML((v) => !v)}
            style={{ ...S.tab, ...(useML ? S.tabOn : null) }}>ML fill model: {useML ? "ON" : "OFF"}</button>
          <button onClick={() => setLive((v) => !v)}
            style={{ ...S.tab, ...(live ? S.tabOn : null) }}>Live books: {live ? "ON" : "OFF"}</button>
          <button onClick={run} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px", marginLeft: "auto",
              opacity: loading ? 0.6 : 1 }}>{loading ? "Routing…" : "Route order"}</button>
        </div>
        <div style={{ marginTop: 10, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <span style={{ ...S.pill, color: isLive ? T.green : T.amber, borderColor: isLive ? T.green : T.amber }}>
            {res ? (isLive ? "CCXT LIVE BOOKS" : "SYNTHETIC BOOK") : "—"}
          </span>
          {res && <span style={{ color: T.dim }}>arrival mid{" "}
            <b style={{ fontFamily: MONO, color: T.text }}>{res.arrival_mid?.toLocaleString(undefined, { maximumFractionDigits: 2 })}</b></span>}
          {res && <span style={{ color: T.dim }}>filled{" "}
            <b style={{ fontFamily: MONO, color: partial ? T.amber : T.text }}>
              {res.filled_qty} / {res.target_qty}</b></span>}
          {live && !isLive && res && <span style={{ color: T.amber }}>
            exchanges unreachable — fell back to the synthetic book</span>}
          {err && <span style={{ color: T.red }}>route failed: {err}</span>}
        </div>
        {partial && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: T.amber }}>
            Partial fill: the order is larger than the depth in the fetched book levels, so the
            benchmark comparison below is not like-for-like — reduce size for a clean read.
          </div>
        )}
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 14 }}>
        <Panel title="Venue liquidity" sub={isLive ? "live L2 books · fees from exchange metadata" : "calibrated synthetic venues"}>
          <table style={S.table}><thead><tr>{["Venue", "Type", "Fee", "Depth", "Top", "Lat", useML ? "P(fill)" : ""].filter(Boolean).map((h) =>
            <th key={h} style={{ ...S.th, textAlign: ["Venue", "Type"].includes(h) ? "left" : "right" }}>{h}</th>)}</tr></thead>
            <tbody>{venues.map((v, i) => {
              const p = fillP[v.venue];
              return (<tr key={v.venue} className="tr">
                <td style={S.td}><span style={{ color: cidx[v.venue], fontFamily: MONO }}>{v.venue}</span></td>
                <td style={S.td}><span style={{ ...S.pill, color: T.dim, borderColor: T.line }}>{v.type}</span></td>
                <td style={S.tdN}>{v.fee_bps?.toFixed(2)}</td>
                <td style={S.tdN}>{v.depth?.toLocaleString()}</td>
                <td style={S.tdN}>{v.top?.toLocaleString(undefined, { maximumFractionDigits: 4 })}</td>
                <td style={S.tdN}>{v.latency_ms?.toFixed(0)}ms</td>
                {useML && <td style={{ ...S.tdN, color: p > 0.5 ? T.green : p > 0.2 ? T.amber : T.red }}>
                  {p == null ? "—" : `${(p * 100).toFixed(0)}%`}</td>}
              </tr>);
            })}</tbody></table>
          {!venues.length && <div style={{ color: T.dim, fontSize: 12.5 }}>
            {loading ? "fetching books…" : "no venues returned"}</div>}
        </Panel>
        <Panel title="Routing allocation" sub="cost-minimising split">
          <div style={{ display: "grid", gap: 11 }}>{Object.entries(alloc).map(([k, q]) => (
            <div key={k}><div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: cidx[k] || T.dim, fontFamily: MONO }}>{k}</span>
              <span style={{ fontFamily: MONO, color: T.text }}>{q?.toLocaleString()}</span></div>
              <Bar pct={maxAlloc ? q / maxAlloc * 100 : 0} c={cidx[k] || T.dim} /></div>))}
            {!Object.keys(alloc).length && <div style={{ color: T.dim, fontSize: 12.5 }}>—</div>}
          </div>
        </Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
        <Panel title="Execution quality" sub="implementation shortfall vs arrival mid">
          <div style={{ display: "grid", gap: 10 }}>
            <Big l="SOR slippage" v={res?.slippage_bps ?? 0} suffix=" bps" accent={T.teal} />
            <KV k="of which fees" v={res ? `${res.fees_bps?.toFixed(2)} bps` : "—"} />
            <KV k="achieved VWAP" v={res ? res.vwap?.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "—"} />
          </div>
        </Panel>
        <Panel title="vs benchmarks" sub="what a desk compares to">
          <div style={{ display: "grid", gap: 10 }}>
            <KV k="Best single venue" v={bench.best_single_venue != null ? `${bench.best_single_venue.toFixed(2)} bps` : "—"} tone={T.amber} />
            <KV k="Naive pro-rata" v={bench.pro_rata != null ? `${bench.pro_rata.toFixed(2)} bps` : "—"} tone={T.red} />
            <KV k="SOR improvement" v={res?.improvement_vs_best_single_bps != null ? `${res.improvement_vs_best_single_bps.toFixed(2)} bps` : "—"} tone={T.green} />
          </div>
        </Panel>
        <Panel title="ML fill model" sub="discrimination & calibration">
          <div style={{ display: "grid", gap: 10 }}>
            <KV k="ROC-AUC" v={ml ? ml.auc?.toFixed(3) : "—"} tone={T.teal} />
            <KV k="Brier score" v={ml ? ml.brier?.toFixed(3) : "—"} />
            <KV k="Accuracy" v={ml ? `${(ml.accuracy * 100).toFixed(1)}%` : "—"} />
          </div>
        </Panel>
      </div>

      <Panel title="Execution rationale" sub="generated from this routing result">
        <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <Split size={16} color={T.teal} style={{ marginTop: 2 }} />
          <div style={{ fontSize: 13, lineHeight: 1.6, color: T.text, whiteSpace: "pre-wrap" }}>
            {res?.explanation || (loading ? "routing…" : "—")}
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* ---------- HOLDINGS & BLOTTER (OMS) ---------- */
// Every figure here comes from the OMS: /api/oms/blotter reads positions,
// orders and fills out of SQLite, marked against real end-of-day levels
// (FRED S&P 500 / VIX). Nothing on this screen is hardcoded.
const STATUS_COLOR = {
  filled: T.green, partially_filled: T.amber, new: T.blue,
  cancelled: T.dim, rejected: T.red,
};

function Blotter() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [symbol, setSymbol] = useState("SPX");
  const [side, setSide] = useState("buy");
  const [qty, setQty] = useState(10);
  const [tab, setTab] = useState("positions");
  const [state, setState] = useState("loading");
  const [greeks, setGreeks] = useState(null);
  const [book, setBook] = useState("DEFAULT");
  // The tradable universe, from the instrument master. This dropdown used to
  // be two hardcoded <option> tags, which is why the desk appeared to trade
  // only SP500 and VIX no matter what the backend supported.
  const [insts, setInsts] = useState([]);

  // A failed fetch used to leave `data` null, which rendered $0.00 across
  // every tile and "No open positions" -- a backend outage displayed as a
  // confidently flat book. In a risk system that is the most dangerous
  // failure mode there is, so load state is now explicit and the numbers are
  // withheld rather than defaulted to zero.
  const load = async () => {
    try {
      const r = await fetch("/api/oms/blotter?desk=traditional");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json()); setErr(null); setState("ready");
    } catch (e) { setErr(String(e.message || e)); setState("error"); }
  };
  const loadUniverse = async () => {
    try {
      // Explicit desk scope. This dropdown is an order-entry surface;
      // it must not offer instruments this screen cannot settle.
      const r = await fetch("/api/marketdata/universe?desk=traditional");
      if (!r.ok) return;
      const u = await r.json();
      // Rate and spread instruments are excluded: they are risk factors with
      // no price to multiply by quantity, and the API rejects them anyway.
      setInsts((u.instruments || []).filter((i) => i.tradable));
    } catch { /* dropdown falls back to whatever is already held */ }
  };
  const loadGreeks = async () => {
    try {
      const r = await fetch("/api/portfolio/greeks");
      setGreeks(r.ok ? await r.json() : null);
    } catch { setGreeks(null); }
  };
  useEffect(() => { load(); loadUniverse(); loadGreeks(); /* eslint-disable-next-line */ }, []);

  // Group by asset class so a 24-instrument list stays navigable.
  const grouped = insts.reduce((acc, i) => {
    (acc[i.asset_class] = acc[i.asset_class] || []).push(i); return acc; }, {});
  const books = data?.portfolios?.length
    ? data.portfolios.map((p) => p.id)
    : ["DEFAULT"];

  const submit = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch("/api/oms/orders", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, side, qty: Number(qty), algo: "MANUAL",
                               portfolio_id: book }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      await load(); loadGreeks();
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };

  const reset = async () => {
    setLoading(true);
    try { await fetch("/api/oms/reset", { method: "POST" }); await load(); }
    finally { setLoading(false); }
  };

  const tot = data?.totals ?? {};
  const pos = data?.positions ?? [];
  const orders = data?.orders ?? [];
  const fills = data?.fills ?? [];
  const pnlColor = (v) => v > 0 ? T.green : v < 0 ? T.red : T.dim;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Order entry" sub="simulated execution against the last published close — no broker connected">
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: 6 }}>
            {["Buy", "Sell"].map((o) => <button key={o} onClick={() => setSide(o.toLowerCase())}
              style={{ ...S.seg, padding: "7px 14px", ...(side === o.toLowerCase() ? S.segOn : null) }}>{o}</button>)}
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Symbol</span>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ ...S.num, width: 220 }}>
              {Object.keys(grouped).sort().map((cls) => (
                <optgroup key={cls} label={cls.replace(/_/g, " ").toLowerCase()}>
                  {grouped[cls].map((i) => (
                    <option key={i.symbol} value={i.symbol}>
                      {i.symbol} — {i.name}{i.latency_class === "REALTIME" ? " ·live" : ""}
                    </option>))}
                </optgroup>))}
              {!insts.length && <option value="SPX">SPX</option>}
            </select></label>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Book</span>
            <select value={book} onChange={(e) => setBook(e.target.value)} style={{ ...S.num, width: 120 }}>
              {books.map((b) => <option key={b} value={b}>{b}</option>)}
            </select></label>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Qty</span>
            <input type="number" value={qty} step={1} min={1}
              onChange={(e) => setQty(e.target.value)} style={{ ...S.num, width: 100 }} /></label>
          <button onClick={submit} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px", opacity: loading ? 0.6 : 1 }}>
            {loading ? "Working…" : "Submit order"}</button>
          <button onClick={reset} disabled={loading} style={{ ...S.tab, marginLeft: "auto" }}>Reset book</button>
        </div>
        {err && <div style={{ color: T.red, fontSize: 12, marginTop: 8 }}>{err}</div>}
        {data?.marks && <div style={{ color: T.dim, fontSize: 11.5, marginTop: 8 }}>
          marks — FRED end-of-day, crypto live: {Object.entries(data.marks).map(([k, v]) => `${k} ${fmt(v, 2)}`).join(" · ")}
        </div>}
        <div style={{ color: T.faint, fontSize: 11, marginTop: 6 }}>
          {insts.length} tradable instruments across {Object.keys(grouped).length} asset classes.
          Rates and credit spreads are deliberately absent — they are risk factors, not order
          lines, and the API rejects them.
        </div>
      </Panel>

      {state === "error" && (
        <Panel><div style={{ ...S.alert, borderColor: T.red }}>
          <span style={{ ...S.pill, color: T.red, borderColor: T.red }}>BOOK UNAVAILABLE</span>
          <span style={{ fontSize: 12.5 }}>
            Could not read the book ({err}). Figures are withheld rather than shown
            as zero — an unreachable backend is not a flat book.
            <button onClick={load} style={{ ...S.tab, marginLeft: 10, padding: "4px 10px" }}>Retry</button>
          </span></div></Panel>)}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14 }}>
        <Panel title="Gross notional" sub="sum |qty| x mark">
          <Big l="" v={tot.gross_notional ?? 0} accent={T.blue} /></Panel>
        <Panel title="Unrealised P&L" sub="open positions at mark">
          <Big l="" v={tot.unrealized_pnl ?? 0} accent={pnlColor(tot.unrealized_pnl)} /></Panel>
        <Panel title="Realised P&L" sub="booked on closing trades">
          <Big l="" v={tot.realized_pnl ?? 0} accent={pnlColor(tot.realized_pnl)} /></Panel>
        <Panel title="Total P&L" sub="realised + unrealised − fees">
          <Big l="" v={tot.total_pnl ?? 0} accent={pnlColor(tot.total_pnl)} /></Panel>
      </div>

      {greeks && !greeks.empty && (
        <Panel title="Book Greeks"
          sub={`${greeks.linear_positions} linear · ${greeks.option_positions} option position(s) · priced by the engine`}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 12 }}>
            <Big l="Cash delta" v={greeks.totals.cash_delta} prefix="$" accent={T.blue} />
            <Big l="Beta-adj cash delta" v={greeks.totals.beta_adj_cash_delta} prefix="$"
              accent={greeks.totals.beta_adj_cash_delta < 0 ? T.red : T.green} />
            <Big l="Cash gamma" v={greeks.totals.cash_gamma} prefix="$"
              accent={greeks.totals.cash_gamma ? T.amber : T.dim} />
            <Big l="Vega" v={greeks.totals.vega} accent={greeks.totals.vega ? T.violet : T.dim} />
            <Big l="Theta" v={greeks.totals.theta} accent={greeks.totals.theta ? T.amber : T.dim} />
            <Big l="P&L on +1%" v={greeks.pnl_1pct_move} prefix="$"
              accent={greeks.pnl_1pct_move < 0 ? T.red : T.green} />
          </div>

          {/* The gap between raw and beta-adjusted delta is the headline: it is
              the diversification the raw number silently assumes. */}
          <div style={{ ...S.alert, marginTop: 12,
            borderColor: Math.sign(greeks.totals.cash_delta) !== Math.sign(greeks.totals.beta_adj_cash_delta)
              ? T.amber : T.lineSoft }}>
            <span style={{ ...S.pill, color: T.amber, borderColor: T.amber }}>DELTA</span>
            <span style={{ fontSize: 12.5 }}>
              {Math.sign(greeks.totals.cash_delta) !== Math.sign(greeks.totals.beta_adj_cash_delta)
                ? `Raw cash delta is ${greeks.totals.cash_delta >= 0 ? "long" : "short"} $${fmt(Math.abs(greeks.totals.cash_delta), 0)}, but beta-adjusted it is ${greeks.totals.beta_adj_cash_delta >= 0 ? "long" : "short"} $${fmt(Math.abs(greeks.totals.beta_adj_cash_delta), 0)} — the book is directionally opposite to equities once each position is scaled by its own beta. Summing raw delta across an index, a commodity and a currency assumes they move together.`
                : `Net/gross delta ${greeks.net_over_gross_delta} — ${greeks.net_over_gross_delta > 0.8 ? "largely directional" : "materially offsetting"}.`}
            </span>
          </div>

          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>{greeks.convexity_note}</div>
          {greeks.positions_without_beta && (
            <div style={{ fontSize: 11.5, color: T.amber, marginTop: 6 }}>
              Excluded from beta-adjusted delta (never regressed on the benchmark):{" "}
              {greeks.positions_without_beta.join(", ")} — defaulting them to β=1 would
              claim they track equities.
            </div>)}

          {greeks.ladder && (
            <div style={{ marginTop: 14 }}>
              <div style={{ color: T.dim, fontSize: 11.5, marginBottom: 6 }}>
                Delta ladder — full revaluation at each shock, not a Taylor expansion
              </div>
              <div style={{ overflowX: "auto" }}>
                <table style={S.table}><thead><tr>
                  {greeks.ladder.ladder.map((r) => (
                    <th key={r.shock_pct} style={{ ...S.th, textAlign: "right" }}>
                      {r.shock_pct > 0 ? "+" : ""}{r.shock_pct}%</th>))}
                </tr></thead><tbody><tr className="tr">
                  {greeks.ladder.ladder.map((r) => (
                    <td key={r.shock_pct} style={{ ...S.tdN,
                      color: r.pnl < 0 ? T.red : r.pnl > 0 ? T.green : T.dim }}>
                      {fmt(r.pnl, 0)}</td>))}
                </tr></tbody></table>
              </div>
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 6 }}>
                {greeks.ladder.note}
              </div>
            </div>)}

          <div style={{ fontSize: 11, color: T.faint, marginTop: 10 }}>
            {greeks.vol_source}
          </div>
        </Panel>)}

      <Panel title="Book" sub={`${pos.length} open position(s) · ${orders.length} order(s) · ${fills.length} fill(s)`}>
        <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
          {[["positions", "Positions"], ["orders", "Orders"], ["fills", "Fills"]].map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)} style={{ ...S.tab, ...(tab === k ? S.tabOn : null) }}>{l}</button>))}
        </div>

        {tab === "positions" && (pos.length ? (
          <table style={S.table}><thead><tr>
            {["Book", "Symbol", "Side", "Qty", "Avg price", "Mark", "Notional", "Unreal P&L", "Real P&L", "Fees", "Total P&L"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: ["Book", "Symbol", "Side"].includes(h) ? "left" : "right" }}>{h}</th>)}
          {/* Keyed on book+symbol, not symbol: the same instrument can be held
              by several portfolios, so a symbol-only key collides and React
              reconciles two distinct positions onto one row. */}
          </tr></thead><tbody>{pos.map((p) => (
            <tr key={`${p.portfolio_id || "DEFAULT"}:${p.symbol}`} className="tr">
              <td style={S.td}><span style={{ ...S.pill, color: T.dim, borderColor: T.line }}>
                {p.portfolio_id || "DEFAULT"}</span></td>
              <td style={S.td}><span style={{ fontFamily: MONO }}>{p.symbol}</span></td>
              <td style={S.td}><span style={{ ...S.pill, color: p.side === "long" ? T.green : T.red,
                borderColor: p.side === "long" ? T.green : T.red }}>{p.side}</span></td>
              <td style={S.tdN}>{fmt(p.qty, 2)}</td>
              <td style={S.tdN}>{fmt(p.avg_price, 2)}</td>
              <td style={S.tdN}>{fmt(p.mark, 2)}{!p.marked && <span style={{ color: T.amber }}> *</span>}</td>
              <td style={S.tdN}>{fmt(p.notional, 0)}</td>
              <td style={{ ...S.tdN, color: pnlColor(p.unrealized_pnl) }}>{fmt(p.unrealized_pnl, 2)}</td>
              <td style={{ ...S.tdN, color: pnlColor(p.realized_pnl) }}>{fmt(p.realized_pnl, 2)}</td>
              <td style={S.tdN}>{fmt(p.fees, 2)}</td>
              <td style={{ ...S.tdN, color: pnlColor(p.total_pnl) }}>{fmt(p.total_pnl, 2)}</td>
            </tr>))}</tbody></table>
        ) : <Empty text={state === "error"
              ? "Book could not be read — this is a connection failure, not an empty book."
              : state === "loading" ? "Loading book…"
              : "No open positions — submit an order above."} />)}

        {tab === "orders" && (orders.length ? (
          <table style={S.table}><thead><tr>
            {["Order ID", "Symbol", "Side", "Qty", "Filled", "Avg price", "Status", "Shortfall", "Algo", "Created"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: ["Order ID", "Symbol", "Side", "Status", "Algo", "Created"].includes(h) ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{orders.map((o) => (
            <tr key={o.id} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11 }}>{o.id}</span></td>
              <td style={S.td}>{o.symbol}</td>
              <td style={S.td}><span style={{ color: o.side === "buy" ? T.green : T.red }}>{o.side}</span></td>
              <td style={S.tdN}>{fmt(o.qty, 2)}</td>
              <td style={S.tdN}>{fmt(o.filled_qty, 2)}</td>
              <td style={S.tdN}>{o.avg_price ? fmt(o.avg_price, 2) : "—"}</td>
              <td style={S.td}><span style={{ ...S.pill, color: STATUS_COLOR[o.status] || T.dim,
                borderColor: STATUS_COLOR[o.status] || T.line }}>{o.status.replace("_", " ")}</span></td>
              <td style={{ ...S.tdN, color: o.shortfall_bps > 0 ? T.amber : T.green }}>
                {o.shortfall_bps == null ? "—" : `${o.shortfall_bps > 0 ? "+" : ""}${fmt(o.shortfall_bps, 2)} bps`}</td>
              <td style={S.td}>{o.algo || "—"}</td>
              <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>{(o.created_at || "").replace("T", " ").slice(0, 19)}</td>
            </tr>))}</tbody></table>
        ) : <Empty text="No orders yet." />)}

        {tab === "fills" && (fills.length ? (
          <table style={S.table}><thead><tr>
            {["Fill ID", "Order", "Symbol", "Side", "Qty", "Price", "Venue", "Fee", "Time"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: ["Fill ID", "Order", "Symbol", "Side", "Venue", "Time"].includes(h) ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{fills.map((f) => (
            <tr key={f.id} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11 }}>{f.id}</span></td>
              <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11, color: T.dim }}>{f.order_id}</span></td>
              <td style={S.td}>{f.symbol}</td>
              <td style={S.td}><span style={{ color: f.side === "buy" ? T.green : T.red }}>{f.side}</span></td>
              <td style={S.tdN}>{fmt(f.qty, 2)}</td>
              <td style={S.tdN}>{fmt(f.price, 2)}</td>
              <td style={S.td}>{f.venue || "—"}</td>
              <td style={S.tdN}>{fmt(f.fee, 2)}</td>
              <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>{(f.ts || "").replace("T", " ").slice(0, 19)}</td>
            </tr>))}</tbody></table>
        ) : <Empty text="No fills yet." />)}

        <div style={{ marginTop: 12, fontSize: 11.5, color: T.dim }}>
          Orders persist in SQLite and survive restarts. Fills are simulated against the last
          published close plus a modelled half-spread — the order lifecycle, average-cost
          accounting and implementation shortfall are real; the counterparty is not.
          <span style={{ color: T.amber }}> *</span> marks a position priced off its own average
          (no external mark available), so its unrealised P&L is zero by construction.
        </div>
      </Panel>
    
      <PerformancePanel />
</div>
  );
}

const Empty = ({ text }) => <div style={{ color: T.dim, fontSize: 12.5, padding: "18px 0" }}>{text}</div>;

/* ---------- LIMITS & ALERTS (pre-trade risk gates) ---------- */
// Reads /api/oms/limits: the live limit set, current utilisation, and the
// persisted breach log. Limits are editable and enforced server-side before
// an order is accepted — the UI cannot bypass them.
const STATUS_TONE = { ok: T.green, watch: T.blue, warning: T.amber, breach: T.red };

const LIMIT_FIELDS = [
  ["max_order_notional", "Max order notional", "single-order fat-finger cap"],
  ["max_position_notional", "Max position notional", "per symbol, post-trade"],
  ["max_gross_exposure", "Max gross exposure", "book-level, post-trade"],
  ["daily_loss_limit", "Daily loss limit", "realised P&L stop"],
  ["price_collar_pct", "Price collar %", "limit vs reference deviation"],
];

function LimitsAlerts() {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const r = await fetch("/api/oms/limits");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setData(d); setDraft(d.limits);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const save = async (patch) => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch("/api/oms/limits", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setData(d); setDraft(d.limits);
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(false); }
  };

  const lim = data?.limits ?? {};
  const util = data?.utilisation ?? [];
  const breaches = data?.breaches ?? [];
  const halted = !!lim.kill_switch;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Desk status" sub="pre-trade gates run before an order is accepted, not after it fills">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ ...S.pill, color: halted ? T.red : T.green,
            borderColor: halted ? T.red : T.green, fontSize: 12, padding: "5px 12px" }}>
            {halted ? "HALTED" : "ACTIVE"}
          </span>
          <button onClick={() => save({ kill_switch: !halted })} disabled={busy}
            style={{ ...S.seg, ...(halted ? null : S.segOn), padding: "7px 16px",
              borderColor: halted ? T.green : T.red,
              color: halted ? T.green : T.red }}>
            {halted ? "Resume trading" : "Activate kill switch"}
          </button>
          <span style={{ color: T.dim, fontSize: 12 }}>
            realised P&L today{" "}
            <b style={{ fontFamily: MONO, color: (data?.realized_pnl_today ?? 0) < 0 ? T.red : T.green }}>
              {fmt(data?.realized_pnl_today ?? 0, 2)}</b></span>
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
      </Panel>

      <Panel title="Limit utilisation" sub="watch ≥60% · warning ≥80% · breach ≥100%">
        <div style={{ display: "grid", gap: 14 }}>
          {util.map((u) => (
            <div key={u.limit}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 5 }}>
                <span style={{ color: T.dim }}>{u.limit}</span>
                <span style={{ fontFamily: MONO }}>
                  <span style={{ color: STATUS_TONE[u.status] }}>{fmt(u.used, 0)}</span>
                  <span style={{ color: T.faint }}> / {fmt(u.cap, 0)}</span>
                  <span style={{ color: STATUS_TONE[u.status], marginLeft: 10 }}>
                    {fmt(u.utilisation_pct, 1)}%</span>
                </span>
              </div>
              <Bar pct={Math.min(100, u.utilisation_pct)} c={STATUS_TONE[u.status]} />
            </div>
          ))}
          {!util.length && <Empty text="No positions — nothing consuming limits yet." />}
        </div>
      </Panel>

      <Panel title="Limit configuration" sub="changes take effect on the next order">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))", gap: 14 }}>
          {LIMIT_FIELDS.map(([key, label, hint]) => (
            <div key={key}>
              <div style={{ color: T.dim, fontSize: 11 }}>{label}</div>
              <div style={{ color: T.faint, fontSize: 10, marginBottom: 5 }}>{hint}</div>
              <input type="number" value={draft[key] ?? ""}
                onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                style={{ ...S.num, width: "100%" }} />
            </div>
          ))}
          <div>
            <div style={{ color: T.dim, fontSize: 11 }}>Restricted symbols</div>
            <div style={{ color: T.faint, fontSize: 10, marginBottom: 5 }}>comma separated, blocked outright</div>
            <input value={(draft.restricted_symbols || []).join(",")}
              onChange={(e) => setDraft({ ...draft,
                restricted_symbols: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
              style={{ ...S.num, width: "100%" }} />
          </div>
        </div>
        <button onClick={() => save({
            ...Object.fromEntries(LIMIT_FIELDS.map(([k]) => [k, Number(draft[k])])),
            restricted_symbols: draft.restricted_symbols || [],
          })} disabled={busy}
          style={{ ...S.seg, ...S.segOn, padding: "7px 16px", marginTop: 14 }}>
          {busy ? "Saving…" : "Save limits"}</button>
      </Panel>

      <Panel title="Breach log" sub="every rejected order, persisted — the record a reviewer asks for">
        {breaches.length ? (
          <table style={S.table}><thead><tr>
            {["Time", "Symbol", "Side", "Qty", "Gate", "Detail", "Observed", "Limit"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: ["Observed", "Limit", "Qty"].includes(h) ? "right" : "left" }}>{h}</th>)}
          </tr></thead><tbody>{breaches.map((b) => (
            <tr key={b.id} className="tr">
              <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>{(b.ts || "").replace("T", " ").slice(0, 19)}</td>
              <td style={S.td}>{b.symbol}</td>
              <td style={S.td}><span style={{ color: b.side === "buy" ? T.green : T.red }}>{b.side}</span></td>
              <td style={S.tdN}>{fmt(b.qty, 2)}</td>
              <td style={S.td}><span style={{ ...S.pill, color: T.red, borderColor: T.red }}>{b.gate}</span></td>
              <td style={{ ...S.td, color: T.dim, fontSize: 11.5 }}>{b.detail}</td>
              <td style={S.tdN}>{b.observed == null ? "—" : fmt(b.observed, 0)}</td>
              <td style={S.tdN}>{b.limit_val == null ? "—" : fmt(b.limit_val, 0)}</td>
            </tr>))}</tbody></table>
        ) : <Empty text="No breaches recorded. Try submitting an oversized order on the Blotter screen." />}
      </Panel>
    
      <CreditCapital />
</div>
  );
}

/* ---------- MARKET DATA (traditional, keyless public sources) ---------- */
// US Treasury (par yield curve), FRED (indices/VIX/credit), ECB (FX).
// All end-of-day and stamped with as_of — deliberately not presented as
// intraday ticks, because these sources don't publish them.
function YieldCurve({ points }) {
  if (!points?.length) return <Empty text="curve unavailable" />;
  const W = 560, H = 190, PL = 42, PB = 26;
  const ys = points.map((p) => p.yield_pct);
  const lo = Math.min(...ys) - 0.15, hi = Math.max(...ys) + 0.15;
  // Log-x: tenors span 1 month to 30 years, so a linear axis collapses the
  // entire front end into the first few pixels — where curve shape matters most.
  const lx = (y) => Math.log(y), x0 = lx(points[0].years), x1 = lx(points[points.length - 1].years);
  const px = (p) => PL + (lx(p.years) - x0) / (x1 - x0) * (W - PL - 12);
  const py = (v) => (H - PB) - (v - lo) / (hi - lo) * (H - PB - 12);
  const path = points.map((p, i) => `${i ? "L" : "M"}${px(p)},${py(p.yield_pct)}`).join(" ");
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W }}>
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={PL} y1={py(t)} x2={W - 12} y2={py(t)} stroke={T.lineSoft} strokeWidth="1" />
          <text x={PL - 6} y={py(t) + 3.5} textAnchor="end" fontSize="9.5" fill={T.faint}
            fontFamily={MONO}>{t.toFixed(2)}</text>
        </g>))}
      <path d={path} fill="none" stroke={T.teal} strokeWidth="2" strokeLinejoin="round" />
      {points.map((p) => (
        <g key={p.tenor}>
          <circle cx={px(p)} cy={py(p.yield_pct)} r="3" fill={T.teal} />
          <title>{p.tenor}: {p.yield_pct}%</title>
        </g>))}
      {points.filter((p) => ["1 Mo", "1 Yr", "5 Yr", "10 Yr", "30 Yr"].includes(p.tenor)).map((p) => (
        <text key={p.tenor} x={px(p)} y={H - 8} textAnchor="middle" fontSize="9.5"
          fill={T.faint} fontFamily={MONO}>{p.tenor}</text>))}
    </svg>
  );
}

/* ---------- CROSS-ASSET BOARD ---------- */
// The whole instrument master, live. Replaced a two-instrument view of the
// world (S&P 500 and VIX) that made every downstream risk screen look like a
// toy. Rate and spread instruments are shown in BASIS POINTS and are marked
// non-tradable, because a yield is a risk factor and not an order line.
function CrossAssetBoard() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [cls, setCls] = useState("ALL");

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/marketdata/board?days=260");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setD(await r.json()); setErr(null);
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  // Crypto rows are genuinely live, so the board is worth re-polling; the
  // EOD rows are served from a 5-minute cache and cost nothing to re-read.
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, []);

  const rows = (d?.rows || []).filter((r) => cls === "ALL" || r.asset_class === cls);
  const classes = ["ALL", ...(d?.asset_classes || [])];
  const dq = d?.data_quality;

  const num = (r) => {
    const price = r.quote_type === "PRICE";
    const chg = price ? r.change_pct : r.change_bp;
    const col = chg > 0 ? T.green : chg < 0 ? T.red : T.dim;
    return (
      <>
        <td style={{ ...S.tdN, fontWeight: 600 }}>
          {r.last != null ? fmt(r.last, Math.abs(r.last) < 10 ? 4 : 2) : "—"}</td>
        <td style={{ ...S.tdN, color: col }}>
          {chg == null ? "—" : `${chg > 0 ? "+" : ""}${fmt(chg, price ? 2 : 1)}${price ? "%" : "bp"}`}</td>
        <td style={S.tdN}>
          {price ? (r.vol_annual_pct != null ? `${fmt(r.vol_annual_pct, 1)}%` : "—")
                 : (r.vol_annual_bp != null ? `${fmt(r.vol_annual_bp, 0)}bp` : "—")}</td>
      </>
    );
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Cross-asset board"
        sub={d ? `${d.live}/${d.instruments} instruments live · FRED end-of-day + live crypto mids`
               : "loading instrument master…"}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
          {classes.map((c) => (
            <button key={c} onClick={() => setCls(c)}
              style={{ ...S.seg, ...(cls === c ? S.segOn : {}) }}>
              {c.replace(/_/g, " ").toLowerCase()}</button>))}
          <button onClick={load} disabled={loading} style={{ ...S.seg, marginLeft: "auto" }}>
            {loading ? "…" : "Refresh"}</button>
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>

        {/* Stale marks are stated, not hidden. FRED's FX block routinely
            trails its equity block by several business days, and any risk
            number computed across both is mixing dates. */}
        {dq && (dq.stale_count > 0 || dq.failed?.length > 0) && (
          <div style={{ ...S.alert, borderColor: T.amber, marginBottom: 10 }}>
            <span style={{ ...S.pill, color: T.amber, borderColor: T.amber }}>DATA QUALITY</span>
            <span style={{ fontSize: 12.5 }}>
              {dq.note}{dq.failed?.length ? ` · unavailable: ${dq.failed.join(", ")}` : ""}</span>
          </div>)}

        {rows.length ? (
          <div style={{ overflowX: "auto" }}>
            <table style={S.table}><thead><tr>
              {["Instrument", "Class", "Last", "Chg", "Ann. vol", "52w range", "As of", "Feed"].map((h) => (
                <th key={h} style={{ ...S.th, textAlign: ["Instrument", "Class", "Feed"].includes(h) ? "left" : "right" }}>{h}</th>))}
            </tr></thead><tbody>
              {rows.map((r) => (
                <tr key={r.symbol} className="tr" style={{ opacity: r.status === "ok" ? 1 : 0.45 }}>
                  <td style={S.td}>
                    <span style={{ fontFamily: MONO, fontWeight: 600 }}>{r.symbol}</span>
                    <span style={{ color: T.faint, fontSize: 10.5 }}> {r.name}</span>
                    {!r.tradable && <span style={{ ...S.pill, color: T.dim, borderColor: T.line,
                      marginLeft: 6, fontSize: 9.5 }}>factor</span>}
                  </td>
                  <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>
                    {r.asset_class.replace(/_/g, " ").toLowerCase()}</td>
                  {r.status === "ok" ? num(r) : (
                    <td colSpan={3} style={{ ...S.tdN, color: T.faint, fontSize: 11 }}>
                      {r.note || r.error || r.status}</td>)}
                  <td style={{ ...S.tdN, color: T.dim, fontSize: 11 }}>
                    {r.low_52w != null ? `${fmt(r.low_52w, 2)} – ${fmt(r.high_52w, 2)}` : "—"}</td>
                  <td style={{ ...S.tdN, fontSize: 11,
                    color: r.stale ? T.amber : T.dim }}>
                    {r.latency_class === "REALTIME" ? "live"
                      : r.as_of ? `${r.as_of}${r.staleness_business_days ? ` (+${r.staleness_business_days}d)` : ""}` : "—"}</td>
                  <td style={{ ...S.td, fontSize: 10.5 }}>
                    <span style={{ ...S.pill,
                      color: r.latency_class === "REALTIME" ? T.teal : T.dim,
                      borderColor: r.latency_class === "REALTIME" ? T.teal : T.line }}>
                      {r.latency_class === "REALTIME" ? "REALTIME" : "EOD"}</span></td>
                </tr>))}
            </tbody></table>
          </div>) : <Empty text={loading ? "fetching instrument master…" : "no instruments"} />}

        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
          Rates and credit spreads are quoted in <b>basis points</b>, not percent returns:
          the 10Y moving 4.77 → 4.78 is +1bp, and calling that “+0.21%” and annualising it
          would make a quiet rates market look violent. Instruments tagged
          <b> factor</b> are not order lines — a yield has no price to multiply by
          quantity, so rate risk enters the book through DV01 on the FI screen.
        </div>
      </Panel>

      {d?.top_movers?.length > 0 && (
        <Panel title="Largest moves" sub="by absolute daily percentage change, price-quoted instruments only">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10 }}>
            {d.top_movers.map((m) => (
              <div key={m.symbol} style={{ ...S.alert, display: "block" }}>
                <div style={{ fontFamily: MONO, fontWeight: 600 }}>{m.symbol}</div>
                <div style={{ fontSize: 10.5, color: T.faint, marginBottom: 4 }}>{m.name}</div>
                <div style={{ fontFamily: MONO, fontSize: 15,
                  color: m.change_pct > 0 ? T.green : T.red }}>
                  {m.change_pct > 0 ? "▲" : "▼"} {fmt(Math.abs(m.change_pct), 2)}%</div>
                <div style={{ fontSize: 11, color: T.dim }}>{fmt(m.last, 2)}</div>
              </div>))}
          </div>
        </Panel>)}
    </div>
  );
}

function MarketData() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/marketdata/snapshot");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(e.message); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const idx = d?.indices ?? [];
  const curve = d?.curve;
  const fx = d?.fx;
  const inverted = (() => {
    const p = curve?.points || [];
    const two = p.find((x) => x.tenor === "2 Yr"), ten = p.find((x) => x.tenor === "10 Yr");
    return two && ten ? ten.yield_pct - two.yield_pct : null;
  })();

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <CrossAssetBoard />
      <Panel title="Reference sources" sub="US Treasury · FRED · ECB — public endpoints, no API keys">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={load} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px" }}>{loading ? "Loading…" : "Refresh"}</button>
          {d && <span style={{ color: T.dim, fontSize: 12 }}>fetched {d.fetched_at?.replace("T", " ")}</span>}
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
          {d && Object.keys(d.errors || {}).length > 0 && (
            <span style={{ color: T.amber, fontSize: 12 }}>
              degraded: {Object.keys(d.errors).join(", ")}</span>)}
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))", gap: 14 }}>
        {idx.map((r) => (
          <Panel key={r.series_id} title={r.label} sub={r.date}>
            <div style={{ fontFamily: MONO, fontSize: 21, fontWeight: 600 }}>
              {fmt(r.value, r.unit === "pct" ? 2 : 2)}{r.unit === "pct" ? "%" : ""}</div>
            <div style={{ fontSize: 12, marginTop: 4,
              color: r.change > 0 ? T.green : r.change < 0 ? T.red : T.dim }}>
              {r.change > 0 ? "▲" : r.change < 0 ? "▼" : "—"} {fmt(Math.abs(r.change), 2)}
              <span style={{ color: T.faint }}> ({fmt(r.change_pct, 2)}%)</span></div>
          </Panel>))}
        {!idx.length && !loading && <Panel title="Indices"><Empty text="unavailable" /></Panel>}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 14 }}>
        <Panel title="US Treasury par yield curve"
          sub={curve?.as_of ? `as of ${curve.as_of} · log-scaled tenor axis` : "—"}>
          <YieldCurve points={curve?.points} />
          {inverted != null && (
            <div style={{ marginTop: 10, fontSize: 12,
              color: inverted < 0 ? T.red : T.dim }}>
              10Y−2Y spread <b style={{ fontFamily: MONO }}>{fmt(inverted, 2)}%</b>
              {inverted < 0 ? " — curve inverted" : " — upward sloping"}
            </div>)}
        </Panel>
        <Panel title="FX reference rates" sub={fx?.as_of ? `ECB, as of ${fx.as_of}` : "—"}>
          {fx?.usd_pairs?.length ? (
            <table style={S.table}><thead><tr>
              <th style={{ ...S.th, textAlign: "left" }}>Pair</th>
              <th style={S.th}>Rate</th></tr></thead>
              <tbody>{fx.usd_pairs.map((p) => (
                <tr key={p.pair} className="tr">
                  <td style={S.td}><span style={{ fontFamily: MONO }}>{p.pair}</span></td>
                  <td style={S.tdN}>{fmt(p.rate, 4)}</td></tr>))}</tbody></table>
          ) : <Empty text="unavailable" />}
        </Panel>
      </div>

      <Panel title="Full curve" sub="every published tenor">
        {curve?.points?.length ? (
          <table style={S.table}><thead><tr>
            {["Tenor", "Years", "Yield %"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: h === "Tenor" ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{curve.points.map((p) => (
            <tr key={p.tenor} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO }}>{p.tenor}</span></td>
              <td style={S.tdN}>{fmt(p.years, 2)}</td>
              <td style={{ ...S.tdN, color: T.teal }}>{fmt(p.yield_pct, 2)}</td>
            </tr>))}</tbody></table>
        ) : <Empty text="unavailable" />}
        <div style={{ marginTop: 12, fontSize: 11.5, color: T.dim }}>
          Sources publish end-of-day only. Yahoo/yfinance is deliberately not used: it returns
          HTTP 429 to datacenter IPs, so anything built on it silently degrades to synthetic
          data once deployed. Intraday equity quotes require a keyed provider.
        </div>
      </Panel>
    </div>
  );
}

/* ---------- RATES & FIXED INCOME ---------- */
// Live US Treasury curve -> bootstrapped zeros -> bond analytics, key-rate
// durations, Nelson-Siegel rich/cheap, and a PCA factor model driving
// historical-simulation VaR. Every number is computed server-side from the
// real curve; nothing on this screen is hardcoded.
const FACTOR_COLOR = { Level: T.teal, Slope: T.blue, Curvature: T.violet };

function FactorLoadings({ factors, years }) {
  if (!factors?.length) return <Empty text="factor model unavailable" />;
  const W = 560, H = 200, PL = 40, PB = 26;
  const all = factors.flatMap((f) => f.loadings);
  const lo = Math.min(...all, 0), hi = Math.max(...all, 0);
  const lx = (y) => Math.log(Math.max(y, 1e-3));
  const x0 = lx(years[0]), x1 = lx(years[years.length - 1]);
  const px = (i) => PL + (lx(years[i]) - x0) / (x1 - x0) * (W - PL - 12);
  const py = (v) => (H - PB) - (v - lo) / (hi - lo) * (H - PB - 14);
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W }}>
      <line x1={PL} y1={py(0)} x2={W - 12} y2={py(0)} stroke={T.line} strokeWidth="1" />
      <text x={PL - 6} y={py(0) + 3.5} textAnchor="end" fontSize="9.5" fill={T.faint} fontFamily={MONO}>0</text>
      {factors.map((f) => (
        <path key={f.name} fill="none" strokeWidth="2" stroke={FACTOR_COLOR[f.name] || T.dim}
          d={f.loadings.map((v, i) => `${i ? "L" : "M"}${px(i)},${py(v)}`).join(" ")} />))}
      {["1M", "1Y", "5Y", "10Y", "30Y"].map((lbl, j) => {
        const target = [1 / 12, 1, 5, 10, 30][j];
        const i = years.reduce((b, y, k) => Math.abs(y - target) < Math.abs(years[b] - target) ? k : b, 0);
        return <text key={lbl} x={px(i)} y={H - 8} textAnchor="middle" fontSize="9.5"
          fill={T.faint} fontFamily={MONO}>{lbl}</text>;
      })}
    </svg>
  );
}

function RatesFI() {
  const [coupon, setCoupon] = useState(4.0);
  const [maturity, setMaturity] = useState(10);
  const [face, setFace] = useState(100);
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const run = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch("/api/rates/analyze", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coupon_pct: Number(coupon), maturity: Number(maturity),
                               face: Number(face), confidence: 0.99 }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  useEffect(() => { run(); /* eslint-disable-next-line */ }, []);

  const a = d?.analytics, kr = d?.key_rate, ns = d?.nelson_siegel;
  const fm = d?.factor_model, fv = d?.factor_var;
  const years = fm?.tenor_years || [];

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Bond analytics" sub={d?.as_of ? `priced off the US Treasury par curve as of ${d.as_of}` : "—"}>
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
          {[["Coupon %", coupon, setCoupon, 0.25], ["Maturity (y)", maturity, setMaturity, 1],
            ["Face", face, setFace, 100]].map(([l, v, set, step]) => (
            <label key={l} style={{ display: "grid", gap: 4 }}>
              <span style={{ color: T.dim, fontSize: 11 }}>{l}</span>
              <input type="number" value={v} step={step}
                onChange={(e) => set(e.target.value)} style={{ ...S.num, width: 110 }} /></label>))}
          <button onClick={run} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px" }}>{loading ? "Pricing…" : "Analyse"}</button>
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
      </Panel>

      {a && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14 }}>
          <Panel title="Price" sub={`YTM ${fmt(a.ytm_pct, 3)}%`}>
            <Big l="" v={a.price} accent={T.teal} /></Panel>
          <Panel title="Modified duration" sub={`effective ${fmt(a.effective_duration, 3)}`}>
            <Big l="" v={a.modified_duration} accent={T.blue} /></Panel>
          <Panel title="Convexity" sub={`effective ${fmt(a.effective_convexity, 2)}`}>
            <Big l="" v={a.convexity} accent={T.violet} /></Panel>
          <Panel title="DV01" sub={`numeric ${fmt(a.dv01_numeric, 6)}`}>
            <Big l="" v={a.dv01} accent={T.amber} /></Panel>
        </div>)}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="Key rate durations"
          sub={kr ? `sum ${fmt(kr.sum_krd, 4)} vs effective ${fmt(kr.effective_duration, 4)} · residual ${fmt(kr.reconciliation_diff, 6)}` : "—"}>
          {kr?.key_rate_durations?.length ? (
            <div style={{ display: "grid", gap: 11 }}>
              {kr.key_rate_durations.map((r) => {
                const mx = Math.max(...kr.key_rate_durations.map((x) => Math.abs(x.krd))) || 1;
                return (
                  <div key={r.tenor_years}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                      <span style={{ color: T.dim, fontFamily: MONO }}>{r.tenor_years}y</span>
                      <span style={{ fontFamily: MONO }}>{fmt(r.krd, 4)}</span></div>
                    <Bar pct={Math.abs(r.krd) / mx * 100} c={r.krd >= 0 ? T.teal : T.red} /></div>);
              })}
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 4 }}>
                A single duration says what a parallel shift costs. KRDs say where the risk
                actually sits — a barbell and a bullet with equal duration behave very
                differently in a steepener.
              </div>
            </div>) : <Empty text="—" />}
        </Panel>

        <Panel title="Scenario P&L" sub="curve moves a rates PM actually hedges">
          {d?.scenarios?.length ? (
            <table style={S.table}><thead><tr>
              {["Scenario", "Price", "P&L", "%"].map((h) =>
                <th key={h} style={{ ...S.th, textAlign: h === "Scenario" ? "left" : "right" }}>{h}</th>)}
            </tr></thead><tbody>{d.scenarios.map((s) => (
              <tr key={s.scenario} className="tr">
                <td style={S.td}>{s.scenario}</td>
                <td style={S.tdN}>{fmt(s.price, 3)}</td>
                <td style={{ ...S.tdN, color: s.pnl >= 0 ? T.green : T.red }}>{fmt(s.pnl, 3)}</td>
                <td style={{ ...S.tdN, color: s.pnl >= 0 ? T.green : T.red }}>{fmt(s.pnl_pct, 2)}%</td>
              </tr>))}</tbody></table>) : <Empty text="—" />}
        </Panel>
      </div>

      <Panel title="Curve factor model (PCA)"
        sub={fm ? `${fm.n_observations} daily changes · ${fm.total_explained_pct}% explained by 3 factors · reconstruction RMSE ${fm.reconstruction_rmse_bp}bp` : "—"}>
        <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 16 }}>
          <div>
            <FactorLoadings factors={fm?.factors} years={years} />
            <div style={{ display: "flex", gap: 14, marginTop: 6, fontSize: 11.5 }}>
              {(fm?.factors || []).map((f) => (
                <span key={f.name} style={{ color: FACTOR_COLOR[f.name] || T.dim }}>
                  ■ {f.name}</span>))}
            </div>
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {(fm?.factors || []).map((f) => (
              <div key={f.name}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                  <span style={{ color: FACTOR_COLOR[f.name] || T.dim }}>{f.name}</span>
                  <span style={{ fontFamily: MONO }}>{fmt(f.explained_variance_pct, 2)}%
                    <span style={{ color: T.faint }}> · {fmt(f.daily_vol_bp, 2)}bp/day</span></span></div>
                <Bar pct={f.explained_variance_pct} c={FACTOR_COLOR[f.name] || T.dim} /></div>))}
            <div style={{ fontSize: 11.5, color: T.dim, marginTop: 4 }}>
              Fitted on daily <b>changes</b>, not levels — yields are near unit-root, so PCA on
              levels mostly recovers the sample mean. Loadings are sign-canonicalised so
              "+1σ slope" means a steepener on every refresh.
            </div>
          </div>
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="Factor VaR"
          sub={fv ? `${fv.confidence * 100}% / ${fv.horizon_days}d · ${fv.n_scenarios} historical scenarios` : "—"}>
          {fv ? (
            <div style={{ display: "grid", gap: 10 }}>
              <KV k="Position DV01" v={fmt(fv.position_dv01, 4)} />
              <KV k={`VaR ${fv.confidence * 100}%`} v={fmt(fv.var, 4)} tone={T.amber} />
              <KV k="Expected shortfall" v={fmt(fv.expected_shortfall, 4)} tone={T.red} />
              <KV k="Implied adverse move" v={`${fmt(fv.implied_move_bp, 2)} bp`} />
              <KV k="Worst historical day" v={fmt(fv.worst_day, 4)} />
              <div style={{ fontSize: 11.5, color: T.dim }}>
                Historical simulation through the factors, not parametric: curve factor scores
                are visibly fat-tailed, and a normal assumption understates exactly the days
                that matter. ES/VaR = {fmt(fv.expected_shortfall / fv.var, 2)}×.
              </div>
            </div>) : <Empty text="—" />}
        </Panel>

        <Panel title="Nelson-Siegel fit — rich / cheap"
          sub={ns ? `λ ${ns.lambda} · RMSE ${ns.rmse_bps}bp · R² ${ns.r_squared}` : "—"}>
          {ns ? (
            <>
              <div style={{ display: "flex", gap: 16, marginBottom: 10, fontSize: 12 }}>
                <span style={{ color: T.dim }}>β₀ level <b style={{ fontFamily: MONO, color: T.text }}>{fmt(ns.beta0_level, 3)}</b></span>
                <span style={{ color: T.dim }}>β₁ slope <b style={{ fontFamily: MONO, color: T.text }}>{fmt(ns.beta1_slope, 3)}</b></span>
                <span style={{ color: T.dim }}>β₂ curv <b style={{ fontFamily: MONO, color: T.text }}>{fmt(ns.beta2_curvature, 3)}</b></span>
              </div>
              <table style={S.table}><thead><tr>
                {["Tenor", "Quoted", "Fitted", "Residual"].map((h) =>
                  <th key={h} style={{ ...S.th, textAlign: h === "Tenor" ? "left" : "right" }}>{h}</th>)}
              </tr></thead><tbody>{(d.curve?.tenors || []).map((t, i) => {
                const res = ns.residuals_bps[i];
                return (<tr key={t} className="tr">
                  <td style={S.td}><span style={{ fontFamily: MONO }}>{t}</span></td>
                  <td style={S.tdN}>{fmt(d.curve.par_pct[i], 2)}</td>
                  <td style={S.tdN}>{fmt(ns.fitted[i], 3)}</td>
                  <td style={{ ...S.tdN, color: Math.abs(res) < 2 ? T.dim : res > 0 ? T.green : T.red }}>
                    {res > 0 ? "+" : ""}{fmt(res, 1)}bp</td>
                </tr>);})}</tbody></table>
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
                Positive residual = quoted above the smooth curve = <b style={{ color: T.green }}>cheap</b>.
                The fit deliberately does not reprice inputs exactly — the residual is the signal.
                λ is grid-searched with an exact linear solve at each step; a joint non-linear fit
                on 14 noisy points routinely lands in a local minimum with an absurd λ.
              </div>
            </>) : <Empty text="—" />}
        </Panel>
      </div>
    </div>
  );
}

/* ---------- BACKTESTING / TCA ---------- */
// Execution quality measured from the OMS's own fills. Costs are signed so
// positive is always bad on both sides, and computed against the ARRIVAL
// price — the two conventions that make aggregate TCA meaningful.
function CostStat({ label, s, unit = "bps" }) {
  if (!s || !s.n) return <div><div style={{ color: T.dim, fontSize: 11 }}>{label}</div>
    <div style={{ color: T.faint, fontSize: 13 }}>no data</div></div>;
  const tone = s.mean > 0 ? T.amber : T.green;
  return (
    <div>
      <div style={{ color: T.dim, fontSize: 11 }}>{label}</div>
      <div style={{ fontFamily: MONO, fontSize: 19, fontWeight: 600, color: tone }}>
        {fmt(s.mean, 2)} <span style={{ fontSize: 12, color: T.faint }}>{unit}</span></div>
      <div style={{ fontSize: 10.5, color: T.faint, fontFamily: MONO }}>
        med {fmt(s.median, 2)} · p95 {fmt(s.p95, 2)} · n {s.n}</div>
    </div>);
}

function TCA() {
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/tca/analysis");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(e.message); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const fm = d?.fill_model;
  const costTone = (v) => v > 0 ? T.amber : T.green;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Transaction cost analysis"
        sub="implementation shortfall vs arrival price · signed so positive is always a cost">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={load} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px" }}>{loading ? "Loading…" : "Refresh"}</button>
          {d && <span style={{ color: T.dim, fontSize: 12 }}>
            {d.orders_analysed} parent order(s) · {d.child_orders} child ·
            notional {fmt(d.total_notional, 0)}</span>}
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
      </Panel>

      {d && !d.orders_analysed ? (
        <Panel title="No execution history yet">
          <Empty text="Submit orders on the Holdings & Blotter screen, or route one on Smart Order Routing, then refresh — TCA is computed from real fills, not sample data." />
        </Panel>
      ) : (
        <>
          <Panel title="Cost summary" sub="means alongside medians and p95 — execution cost distributions are skewed">
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 16 }}>
              <CostStat label="Total cost" s={d?.cost_bps} />
              <CostStat label="Execution / impact" s={d?.execution_bps} />
              <CostStat label="Fees" s={d?.fee_bps} />
              <CostStat label="Fill rate" s={d?.fill_rate_pct} unit="%" />
              <div>
                <div style={{ color: T.dim, fontSize: 11 }}>Notional-weighted cost</div>
                <div style={{ fontFamily: MONO, fontSize: 19, fontWeight: 600,
                  color: costTone(d?.notional_weighted_cost_bps) }}>
                  {d?.notional_weighted_cost_bps == null ? "—" : `${fmt(d.notional_weighted_cost_bps, 2)} bps`}</div>
                <div style={{ fontSize: 10.5, color: T.faint }}>what the desk actually paid</div>
              </div>
            </div>
          </Panel>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <Panel title="By algo" sub="which algo earns the flow">
              {d?.by_algo?.length ? (
                <table style={S.table}><thead><tr>
                  {["Algo", "Orders", "Notional", "Fill %", "Mean bps", "Median", "p95"].map((h) =>
                    <th key={h} style={{ ...S.th, textAlign: h === "Algo" ? "left" : "right" }}>{h}</th>)}
                </tr></thead><tbody>{d.by_algo.map((a) => (
                  <tr key={a.algo} className="tr">
                    <td style={S.td}><span style={{ fontFamily: MONO }}>{a.algo}</span></td>
                    <td style={S.tdN}>{a.orders}</td>
                    <td style={S.tdN}>{fmt(a.notional, 0)}</td>
                    <td style={S.tdN}>{fmt(a.avg_fill_rate_pct, 1)}</td>
                    <td style={{ ...S.tdN, color: costTone(a.cost_mean) }}>{fmt(a.cost_mean, 2)}</td>
                    <td style={S.tdN}>{fmt(a.cost_median, 2)}</td>
                    <td style={S.tdN}>{fmt(a.cost_p95, 2)}</td>
                  </tr>))}</tbody></table>) : <Empty text="—" />}
            </Panel>

            <Panel title="By venue" sub="attribution from child orders, where routing happened">
              {d?.by_venue?.length ? (
                <table style={S.table}><thead><tr>
                  {["Venue", "Fills", "Share %", "Mean bps", "Worst"].map((h) =>
                    <th key={h} style={{ ...S.th, textAlign: h === "Venue" ? "left" : "right" }}>{h}</th>)}
                </tr></thead><tbody>{d.by_venue.map((v) => (
                  <tr key={v.venue} className="tr">
                    <td style={S.td}><span style={{ fontFamily: MONO }}>{v.venue}</span></td>
                    <td style={S.tdN}>{v.fills}</td>
                    <td style={S.tdN}>{fmt(v.share_pct, 1)}</td>
                    <td style={{ ...S.tdN, color: costTone(v.cost_mean) }}>{fmt(v.cost_mean, 2)}</td>
                    <td style={S.tdN}>{fmt(v.cost_worst, 2)}</td>
                  </tr>))}</tbody></table>) : <Empty text="—" />}
            </Panel>
          </div>

          <Panel title="Worst executions" sub="largest absolute cost — where to look first">
            {d?.outliers?.length ? (
              <table style={S.table}><thead><tr>
                {["Order", "Symbol", "Side", "Algo", "Qty", "Fill %", "Arrival", "Avg px", "Exec bps", "Total bps"].map((h) =>
                  <th key={h} style={{ ...S.th, textAlign: ["Order","Symbol","Side","Algo"].includes(h) ? "left" : "right" }}>{h}</th>)}
              </tr></thead><tbody>{d.outliers.map((o) => (
                <tr key={o.order_id} className="tr">
                  <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11 }}>{o.order_id}</span></td>
                  <td style={S.td}>{o.symbol}</td>
                  <td style={S.td}><span style={{ color: o.side === "buy" ? T.green : T.red }}>{o.side}</span></td>
                  <td style={S.td}>{o.algo}</td>
                  <td style={S.tdN}>{fmt(o.qty, 2)}</td>
                  <td style={S.tdN}>{fmt(o.fill_rate_pct, 1)}</td>
                  <td style={S.tdN}>{fmt(o.arrival, 2)}</td>
                  <td style={S.tdN}>{fmt(o.avg_price, 2)}</td>
                  <td style={{ ...S.tdN, color: costTone(o.execution_bps) }}>{fmt(o.execution_bps, 2)}</td>
                  <td style={{ ...S.tdN, color: costTone(o.total_cost_bps) }}>{fmt(o.total_cost_bps, 2)}</td>
                </tr>))}</tbody></table>) : <Empty text="—" />}
          </Panel>
        </>
      )}

      <Panel title="Fill-probability model" sub="the ML layer the router uses to discount unreachable size">
        {fm ? (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 14 }}>
              <KV k="ROC-AUC" v={fm.auc} tone={T.teal} />
              <KV k="Brier" v={fm.brier} />
              <KV k="Brier (baseline)" v={fm.brier_baseline} tone={T.dim} />
              <KV k="Brier skill score" v={fm.brier_skill_score}
                tone={fm.brier_skill_score > 0 ? T.green : T.red} />
              <KV k="Log loss" v={fm.log_loss} />
              <KV k="Base rate" v={fm.base_rate} />
              <KV k="Test rows" v={fm.n_test} />
            </div>
            <div style={{ marginTop: 12, fontSize: 11.5, color: T.amber,
              border: `1px solid ${T.line}`, borderRadius: 8, padding: 10 }}>
              <b>Evaluation: {fm.evaluation}.</b> Trained on {fm.sample_source}.
              Brier skill score &gt; 0 means it beats predicting the base rate for everything —
              without that comparison an AUC alone can hide a model that has learned nothing.
              Live labelled rows from the OMS: <b>{fm.live_labelled_rows}</b>
              {fm.live_complete_rate != null && <> (complete rate {fm.live_complete_rate})</>};
              retraining on live fills begins at {fm.retrain_threshold}.
            </div>
            {fm.calibration?.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div style={{ color: T.dim, fontSize: 11, marginBottom: 8 }}>
                  CALIBRATION — predicted vs realised frequency (the router multiplies size by
                  this probability, so over-confidence means over-allocation)
                </div>
                <table style={S.table}><thead><tr>
                  {["Bucket", "n", "Predicted", "Observed", "Gap"].map((h) =>
                    <th key={h} style={{ ...S.th, textAlign: h === "Bucket" ? "left" : "right" }}>{h}</th>)}
                </tr></thead><tbody>{fm.calibration.map((c) => {
                  const gap = c.predicted - c.observed;
                  return (<tr key={c.bucket} className="tr">
                    <td style={S.td}><span style={{ fontFamily: MONO }}>{c.bucket}</span></td>
                    <td style={S.tdN}>{c.n}</td>
                    <td style={S.tdN}>{fmt(c.predicted, 3)}</td>
                    <td style={S.tdN}>{fmt(c.observed, 3)}</td>
                    <td style={{ ...S.tdN, color: Math.abs(gap) < 0.05 ? T.green : T.amber }}>
                      {gap > 0 ? "+" : ""}{fmt(gap, 3)}</td>
                  </tr>);})}</tbody></table>
              </div>)}
          </>) : <Empty text="—" />}
      </Panel>
    </div>
  );
}

/* ---------- FACTOR & SENSITIVITY ---------- */
// Risk decomposition over the live OMS book using real FRED history.
// Component VaR sums exactly to portfolio VaR (Euler) — the check is shown.
function FactorSensitivity() {
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/portfolio/factors");
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  if (d?.empty) return <Panel title="Factor & sensitivity"><Empty text={d.message} /></Panel>;
  if (d?.error) return <Panel title="Factor & sensitivity"><Empty text={d.error} /></Panel>;
  const pos = d?.positions || [];
  const maxRisk = Math.max(...pos.map((p) => Math.abs(p.component_var)), 1);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Portfolio risk" sub={d?.alignment || "—"}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
          <button onClick={load} disabled={loading} style={{ ...S.seg, ...S.segOn, padding: "6px 14px" }}>
            {loading ? "…" : "Refresh"}</button>
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
          {d?.unmapped_symbols?.length > 0 && <span style={{ color: T.amber, fontSize: 12 }}>
            unmapped (no history): {d.unmapped_symbols.join(", ")}</span>}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 14 }}>
          <KV k="Gross exposure" v={d ? fmt(d.gross_exposure, 0) : "—"} />
          <KV k="Net exposure" v={d ? fmt(d.net_exposure, 0) : "—"} />
          <KV k="Portfolio vol (ann.)" v={d ? `${fmt(d.portfolio_vol_annual_pct, 2)}%` : "—"} />
          <KV k="Portfolio beta" v={d?.portfolio_beta ?? "—"} tone={T.blue} />
          <KV k={`VaR ${d ? d.confidence * 100 : 99}% (param)`} v={d ? fmt(d.var_parametric, 2) : "—"} tone={T.amber} />
          <KV k="VaR (historical)" v={d ? fmt(d.var_historical, 2) : "—"} tone={T.amber} />
          <KV k="ES (historical)" v={d ? fmt(d.es_historical, 2) : "—"} tone={T.red} />
          <KV k="Tail ratio hist/param" v={d?.tail_ratio_hist_over_param ?? "—"}
            tone={d?.tail_ratio_hist_over_param > 1 ? T.red : T.green} />
        </div>
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 12 }}>
          Both VaR methods are shown deliberately: parametric supports the exact Euler
          decomposition below but assumes normality; historical makes no distributional
          assumption and captures fat tails. A tail ratio above 1 means the empirical tail is
          fatter than normal — here {d?.tail_ratio_hist_over_param ?? "—"}×.
        </div>
      </Panel>

      <Panel title="Risk attribution (component VaR)"
        sub={d ? `sum ${fmt(d.component_var_sum, 2)} vs portfolio VaR ${fmt(d.var_parametric, 2)} · Euler residual ${d.euler_check_diff}` : "—"}>
        {pos.length ? (
          <>
            <table style={S.table}><thead><tr>
              {["Symbol", "Exposure", "Weight %", "Vol (ann.)", "Beta", "Marginal VaR", "Component VaR", "% of risk"].map((h) =>
                <th key={h} style={{ ...S.th, textAlign: h === "Symbol" ? "left" : "right" }}>{h}</th>)}
            </tr></thead><tbody>{pos.map((p) => (
              <tr key={p.symbol} className="tr">
                <td style={S.td}><span style={{ fontFamily: MONO }}>{p.symbol}</span></td>
                <td style={S.tdN}>{fmt(p.exposure, 0)}</td>
                <td style={S.tdN}>{fmt(p.weight_pct, 1)}</td>
                <td style={S.tdN}>{fmt(p.vol_annual_pct, 2)}%</td>
                <td style={{ ...S.tdN, color: p.beta < 0 ? T.green : T.text }}>{p.beta ?? "—"}</td>
                <td style={S.tdN}>{fmt(p.marginal_var, 2)}</td>
                <td style={{ ...S.tdN, color: p.component_var < 0 ? T.green : T.amber }}>{fmt(p.component_var, 2)}</td>
                <td style={{ ...S.tdN, color: p.pct_of_risk < 0 ? T.green : T.amber }}>{fmt(p.pct_of_risk, 1)}%</td>
              </tr>))}</tbody></table>
            <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
              {pos.map((p) => (
                <div key={p.symbol}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                    <span style={{ color: T.dim, fontFamily: MONO }}>{p.symbol}</span>
                    <span style={{ fontFamily: MONO, color: p.component_var < 0 ? T.green : T.amber }}>
                      {fmt(p.pct_of_risk, 1)}% of risk</span></div>
                  <Bar pct={Math.abs(p.component_var) / maxRisk * 100}
                    c={p.component_var < 0 ? T.green : T.amber} /></div>))}
            </div>
            <div style={{ fontSize: 11.5, color: T.dim, marginTop: 12 }}>
              A <b style={{ color: T.green }}>negative</b> component VaR means the position is
              <b> diversifying</b> — it reduces total portfolio risk rather than adding to it.
              Components sum exactly to portfolio VaR under Euler allocation, so "cut this
              position, lose that much risk" ties out.
            </div>
          </>) : <Empty text="—" />}
      </Panel>

      {d?.correlation && (
        <Panel title="Correlation matrix" sub="date-aligned daily returns">
          <table style={S.table}><thead><tr>
            <th style={{ ...S.th, textAlign: "left" }}></th>
            {d.symbols.map((s) => <th key={s} style={S.th}>{s}</th>)}
          </tr></thead><tbody>{d.correlation.map((row, i) => (
            <tr key={i} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO }}>{d.symbols[i]}</span></td>
              {row.map((v, j) => (
                <td key={j} style={{ ...S.tdN,
                  color: v == null ? T.faint : v > 0.5 ? T.red : v < -0.5 ? T.green : T.text }}>
                  {v == null ? "—" : fmt(v, 3)}</td>))}
            </tr>))}</tbody></table>
        </Panel>)}
    
      <RegressionDiagnostics />
</div>
  );
}

/* ---------- STRESS & SCENARIOS ---------- */
function StressScenarios() {
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/portfolio/stress");
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  if (d?.empty) return <Panel title="Stress & scenarios"><Empty text={d.message} /></Panel>;
  const hyp = d?.hypothetical?.scenarios || [];
  const hist = d?.historical || {};
  const rev = d?.reverse || {};
  const worstHyp = Math.min(...hyp.map((h) => h.pnl), 0);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Stress testing" sub="historical replay · hypothetical shocks · reverse stress">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={load} disabled={loading} style={{ ...S.seg, ...S.segOn, padding: "6px 14px" }}>
            {loading ? "…" : "Re-run"}</button>
          {d && <span style={{ color: T.dim, fontSize: 12 }}>
            gross {fmt(d.gross_exposure || 0, 0)} · net {fmt(d.net_exposure || 0, 0)} ·
            1d VaR {fmt(d.var_historical || 0, 0)}</span>}
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
      </Panel>

      <Panel title="Reverse stress — what move breaks the book?"
        sub="inverts the usual question; regulators ask for this and most systems don't produce it">
        {rev.breach_move_pct != null ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 14 }}>
            <div>
              <div style={{ color: T.dim, fontSize: 11 }}>Market move to breach limit</div>
              <div style={{ fontFamily: MONO, fontSize: 26, fontWeight: 600,
                color: rev.breach_move_pct < 5 ? T.red : rev.breach_move_pct < 15 ? T.amber : T.green }}>
                {fmt(rev.breach_move_pct, 2)}%</div>
              <div style={{ fontSize: 11, color: T.faint }}>{rev.direction}</div>
            </div>
            <KV k="Daily loss limit" v={fmt(rev.loss_limit, 0)} />
            <KV k="Net delta" v={fmt(rev.net_delta, 0)} />
            <KV k="Gross delta" v={fmt(rev.gross_delta, 0)} />
          </div>) : (
          <div style={{ fontSize: 12.5, color: T.dim }}>{rev.interpretation || "—"}</div>)}
        {rev.interpretation && rev.breach_move_pct != null && (
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
            {rev.interpretation}. {rev.hedge_note}
          </div>)}
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="Hypothetical scenarios" sub="forward-looking — history is not an upper bound">
          {hyp.length ? (
            <div style={{ display: "grid", gap: 12 }}>
              {hyp.map((h) => (
                <div key={h.scenario}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                    <span style={{ color: T.dim }}>{h.scenario}</span>
                    <span style={{ fontFamily: MONO, color: h.pnl < 0 ? T.red : T.green }}>
                      {fmt(h.pnl, 0)}</span></div>
                  <Bar pct={worstHyp ? Math.abs(h.pnl) / Math.abs(worstHyp) * 100 : 0}
                    c={h.pnl < 0 ? T.red : T.green} /></div>))}
              {d?.hypothetical?.unmapped_symbols?.length > 0 && (
                <div style={{ fontSize: 11.5, color: T.amber }}>
                  {d.hypothetical.note}: {d.hypothetical.unmapped_symbols.join(", ")}</div>)}
            </div>) : <Empty text="—" />}
        </Panel>

        <Panel title="Historical replay — worst actual days"
          sub={hist.n_observations ? `${hist.n_observations} observations` : "—"}>
          {hist.worst_days?.length ? (
            <>
              <table style={S.table}><thead><tr>
                {["Date", "P&L", ...Object.keys(hist.worst_days[0].moves).map((s) => `${s} %`)].map((h) =>
                  <th key={h} style={{ ...S.th, textAlign: h === "Date" ? "left" : "right" }}>{h}</th>)}
              </tr></thead><tbody>{hist.worst_days.map((w) => (
                <tr key={w.date} className="tr">
                  <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11 }}>{w.date}</span></td>
                  <td style={{ ...S.tdN, color: w.pnl < 0 ? T.red : T.green }}>{fmt(w.pnl, 0)}</td>
                  {Object.values(w.moves).map((m, i) => (
                    <td key={i} style={{ ...S.tdN, color: m < 0 ? T.red : T.green }}>{fmt(m, 2)}</td>))}
                </tr>))}</tbody></table>
              <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>{hist.note}</div>
            </>) : <Empty text={hist.error || "—"} />}
        </Panel>
      </div>
    </div>
  );
}

/* ---------- OPTIMIZATION ---------- */
// Min-variance / risk-parity / max-Sharpe on the SAME covariance the Factor
// screen uses, so construction and measurement cannot disagree.
const METHOD_LABEL = {
  min_variance: "Minimum variance", risk_parity: "Risk parity",
  max_sharpe: "Maximum Sharpe", equal_weight: "Equal weight (1/N)",
  current: "Current book",
};
const METHOD_COLOR = {
  min_variance: T.teal, risk_parity: T.blue, max_sharpe: T.violet,
  equal_weight: T.dim, current: T.amber,
};

function Optimization() {
  const [d, setD] = useState(null);
  const [rf, setRf] = useState(0.04);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const load = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch(`/api/portfolio/optimise?rf=${rf}`);
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      setD(await r.json());
    } catch (e) { setErr(String(e.message || e)); } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  if (d?.empty) return <Panel title="Portfolio optimisation"><Empty text={d.message} /></Panel>;
  if (d?.error) return <Panel title="Portfolio optimisation"><Empty text={d.error} /></Panel>;

  const syms = d?.symbols || [];
  const methods = d?.methods || {};
  const order = ["min_variance", "risk_parity", "max_sharpe", "equal_weight", "current"]
    .filter((k) => methods[k]);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Portfolio optimisation" sub={d?.alignment || "—"}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Risk-free (annual)</span>
            <input type="number" step="0.005" value={rf}
              onChange={(e) => setRf(e.target.value)} style={{ ...S.num, width: 100 }} /></label>
          <button onClick={load} disabled={loading}
            style={{ ...S.seg, ...S.segOn, padding: "7px 16px" }}>{loading ? "Solving…" : "Optimise"}</button>
          {d && <span style={{ color: T.dim, fontSize: 12 }}>{d.n_observations} observations · {syms.length} assets</span>}
          {err && <span style={{ color: T.red, fontSize: 12 }}>{err}</span>}
        </div>
      </Panel>

      <Panel title="Method comparison" sub="all solved on one covariance estimate">
        <table style={S.table}><thead><tr>
          {["Method", ...syms.map((s) => `${s} %`), "Exp. return", "Vol", "Sharpe", "Risk conc.", "Eff. N"].map((h) =>
            <th key={h} style={{ ...S.th, textAlign: h === "Method" ? "left" : "right" }}>{h}</th>)}
        </tr></thead><tbody>{order.map((k) => {
          const m = methods[k];
          return (<tr key={k} className="tr">
            <td style={S.td}><span style={{ color: METHOD_COLOR[k] }}>{METHOD_LABEL[k]}</span></td>
            {m.weights.map((w, i) => <td key={i} style={S.tdN}>{fmt(w * 100, 1)}</td>)}
            <td style={S.tdN}>{fmt(m.expected_return_pct, 2)}%</td>
            <td style={S.tdN}>{fmt(m.volatility_pct, 2)}%</td>
            <td style={{ ...S.tdN, color: m.sharpe > 1 ? T.green : T.text }}>{m.sharpe ?? "—"}</td>
            <td style={{ ...S.tdN, color: m.risk_concentration < 0.05 ? T.green : T.amber }}>
              {fmt(m.risk_concentration, 3)}</td>
            <td style={S.tdN}>{m.effective_n ?? "—"}</td>
          </tr>);})}</tbody></table>
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 12 }}>
          <b>Risk concentration</b> is the spread of risk contributions — 0 means perfect risk
          parity. Note how equal weight is usually the <i>least</i> balanced by risk: equal
          money is not equal risk when volatilities differ.
        </div>
      </Panel>

      <Panel title="Risk contribution by method" sub="where each portfolio's risk actually sits">
        <div style={{ display: "grid", gap: 16 }}>
          {order.map((k) => (
            <div key={k}>
              <div style={{ fontSize: 12, color: METHOD_COLOR[k], marginBottom: 6 }}>{METHOD_LABEL[k]}</div>
              <div style={{ display: "flex", gap: 2, height: 22, borderRadius: 5, overflow: "hidden" }}>
                {methods[k].risk_contributions_pct.map((rc, i) => (
                  <div key={i} title={`${syms[i]}: ${fmt(rc, 1)}%`}
                    style={{ width: `${Math.max(0, Math.abs(rc))}%`,
                      background: rc < 0 ? T.green : [T.blue, T.violet, T.amber, T.teal][i % 4],
                      display: "grid", placeItems: "center", fontSize: 10,
                      fontFamily: MONO, color: T.bg }}>
                    {Math.abs(rc) > 12 ? `${syms[i]} ${fmt(rc, 0)}%` : ""}</div>))}
              </div>
            </div>))}
        </div>
      </Panel>

      {d?.caveat && (
        <Panel title="Estimation caveat">
          <div style={{ fontSize: 12.5, color: T.amber, lineHeight: 1.6 }}>{d.caveat}</div>
        </Panel>)}
    </div>
  );
}

/* ---------- ENGINE BENCHMARK (C++ parallel Monte Carlo) ---------- */
function EngineBench() {
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const r = await fetch("/api/bench/monte-carlo?paths=2000000&threads=1,2,4,8");
      setD(await r.json());
    } catch (e) { setD({ cpp_available: false, reason: String(e) }); }
    finally { setBusy(false); }
  };
  return (
    <Panel title="C++ parallel Monte Carlo — measured on this host"
      sub="throughput is benchmarked live, not quoted from a spec sheet">
      <button onClick={run} disabled={busy}
        style={{ ...S.seg, ...S.segOn, padding: "6px 14px", marginBottom: 12 }}>
        {busy ? "Benchmarking…" : "Run benchmark"}</button>
      {d && !d.cpp_available && (
        <Empty text={`C++ extension unavailable: ${d.reason || "not built"}. Engines run on numpy.`} />)}
      {d?.cpp_available && (
        <>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 12, fontSize: 12 }}>
            <span style={{ color: T.dim }}>host cores <b style={{ fontFamily: MONO, color: T.text }}>{d.host_cores}</b></span>
            <span style={{ color: T.dim }}>paths/run <b style={{ fontFamily: MONO, color: T.text }}>{fmt(d.paths_per_run, 0)}</b></span>
            <span style={{ ...S.pill, color: d.deterministic_across_threads ? T.green : T.red,
              borderColor: d.deterministic_across_threads ? T.green : T.red }}>
              {d.deterministic_across_threads ? "BITWISE DETERMINISTIC ACROSS THREAD COUNTS" : "NON-DETERMINISTIC"}
            </span>
          </div>
          <table style={S.table}><thead><tr>
            {["Threads", "ms", "M paths/s", "Speedup", "Price"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: h === "Threads" ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{d.results.map((r) => (
            <tr key={r.threads} className="tr">
              <td style={S.td}><span style={{ fontFamily: MONO }}>{r.threads}</span>
                {r.oversubscribed && <span style={{ color: T.amber, fontSize: 10 }}> oversubscribed</span>}</td>
              <td style={S.tdN}>{fmt(r.ms, 1)}</td>
              <td style={{ ...S.tdN, color: T.teal }}>{fmt(r.millions_per_sec, 2)}</td>
              <td style={S.tdN}>{fmt(r.speedup_vs_1_thread, 2)}×</td>
              <td style={{ ...S.tdN, color: T.dim }}>{r.price?.toFixed(9)}</td>
            </tr>))}</tbody></table>
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>{d.note}</div>
        </>)}
    </Panel>
  );
}

/* ---------- GENAI MONITOR ---------- */
// Operational risk over the platform's OWN Claude usage: latency/cost VaR,
// anomaly z-scores, split-window drift, error rate. Recorded per invocation
// in SQLite by the copilot service.
function GenAIMonitor() {
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const load = async () => {
    setLoading(true);
    try { setD(await (await fetch("/api/genai/monitor")).json()); }
    catch (e) { setD({ empty: true, message: String(e) }); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, []);

  if (d?.empty) return (
    <Panel title="GenAI monitor" sub="operational risk over this platform's own model usage">
      <Empty text={d.message} /></Panel>);

  const c = d?.components || {};
  const bandColor = { LOW: T.teal, MEDIUM: T.blue, HIGH: T.amber, CRITICAL: T.red };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="GenAI operational risk"
        sub={d ? `${d.n_total} invocation(s) recorded · scoring the last ${d.n_window}` : "—"}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
          <button onClick={load} disabled={loading} style={{ ...S.seg, ...S.segOn, padding: "6px 14px" }}>
            {loading ? "…" : "Refresh"}</button>
          <span style={{ ...S.pill, color: bandColor[c.band] || T.dim, borderColor: bandColor[c.band] || T.line,
            fontSize: 12, padding: "5px 12px" }}>{c.band || "—"}</span>
          <span style={{ fontFamily: MONO, fontSize: 20, fontWeight: 600, color: bandColor[c.band] }}>
            {fmt(c.unified_score ?? 0, 1)}</span>
          <span style={{ color: T.dim, fontSize: 12 }}>unified score</span>
        </div>
        <div style={{ display: "grid", gap: 12 }}>
          {[["Operational (latency/cost tail)", c.operational_risk, 0.35],
            ["Anomaly (z vs window)", c.anomaly_risk, 0.25],
            ["Drift (split-window)", c.drift_risk, 0.20],
            ["Reliability (error rate)", c.reliability_risk, 0.20]].map(([l, v, w]) => (
            <div key={l}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                <span style={{ color: T.dim }}>{l} <span style={{ color: T.faint }}>· weight {w}</span></span>
                <span style={{ fontFamily: MONO }}>{fmt(v ?? 0, 1)}</span></div>
              <Bar pct={Math.min(100, v ?? 0)} c={(v ?? 0) >= 50 ? T.amber : T.teal} /></div>))}
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: 14 }}>
        <Panel title="Latency" sub="VaR95 is what breaks an SLA, not the mean">
          <div style={{ display: "grid", gap: 8 }}>
            <KV k="Mean" v={d?.latency?.mean_ms ? `${fmt(d.latency.mean_ms, 0)} ms` : "—"} />
            <KV k="Median" v={d?.latency?.median_ms ? `${fmt(d.latency.median_ms, 0)} ms` : "—"} />
            <KV k="VaR 95%" v={d?.latency?.var95_ms ? `${fmt(d.latency.var95_ms, 0)} ms` : "—"} tone={T.amber} />
            <KV k="Max" v={d?.latency?.max_ms ? `${fmt(d.latency.max_ms, 0)} ms` : "—"} />
          </div></Panel>
        <Panel title="Cost" sub="cumulative spend on this platform">
          <div style={{ display: "grid", gap: 8 }}>
            <KV k="Total" v={`$${fmt(d?.cost?.total_usd ?? 0, 6)}`} tone={T.teal} />
            <KV k="Mean / call" v={d?.cost?.mean_usd ? `$${fmt(d.cost.mean_usd, 6)}` : "—"} />
            <KV k="VaR 95%" v={d?.cost?.var95_usd ? `$${fmt(d.cost.var95_usd, 6)}` : "—"} tone={T.amber} />
            <KV k="Tokens in/out" v={`${fmt(d?.tokens?.input ?? 0, 0)} / ${fmt(d?.tokens?.output ?? 0, 0)}`} />
          </div></Panel>
        <Panel title="Reliability & drift" sub="errors are recorded, not just logged">
          <div style={{ display: "grid", gap: 8 }}>
            <KV k="Error rate" v={`${fmt(d?.reliability?.error_rate_pct ?? 0, 2)}%`}
              tone={(d?.reliability?.error_rate_pct ?? 0) > 0 ? T.red : T.green} />
            <KV k="Latency z (latest)" v={d?.anomaly?.latency_z ?? "—"} />
            <KV k="Reply-length z" v={d?.anomaly?.reply_length_z ?? "—"} />
            <KV k="Latency drift" v={d?.drift ? `${fmt(d.drift.latency_shift_pct, 1)}%` : "insufficient history"} />
          </div></Panel>
      </div>

      <Panel title="Recent invocations" sub="every call the platform made to the model">
        {d?.recent?.length ? (
          <table style={S.table}><thead><tr>
            {["Time", "Model", "Endpoint", "Latency", "In", "Out", "Cost", "OK"].map((h) =>
              <th key={h} style={{ ...S.th, textAlign: ["Time","Model","Endpoint"].includes(h) ? "left" : "right" }}>{h}</th>)}
          </tr></thead><tbody>{d.recent.map((r, i) => (
            <tr key={i} className="tr">
              <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>{(r.ts || "").replace("T", " ").slice(0, 19)}</td>
              <td style={S.td}><span style={{ fontFamily: MONO, fontSize: 11 }}>{r.model}</span></td>
              <td style={{ ...S.td, color: T.dim, fontSize: 11 }}>{r.endpoint}</td>
              <td style={S.tdN}>{fmt(r.latency_ms, 0)} ms</td>
              <td style={S.tdN}>{r.input_tokens}</td>
              <td style={S.tdN}>{r.output_tokens}</td>
              <td style={S.tdN}>${fmt(r.cost_usd, 6)}</td>
              <td style={{ ...S.tdN, color: r.ok ? T.green : T.red }}>{r.ok ? "✓" : "✗"}</td>
            </tr>))}</tbody></table>) : <Empty text="—" />}
        <div style={{ marginTop: 12, fontSize: 11.5, color: T.amber }}>
          <b>Limitation:</b> {d?.limitation}
        </div>
      </Panel>
    </div>
  );
}

/* ---------- MODEL LIBRARY (inventory) ---------- */
const MODEL_STATUS_TONE = { validated: T.green, tested: T.blue, illustrative: T.amber };

function ModelLibrary() {
  const [d, setD] = useState(null);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState(null);
  useEffect(() => { fetch("/api/genai/models").then((r) => r.json()).then(setD).catch(() => setD(null)); }, []);

  const models = d?.models || [];
  const cats = ["all", ...Array.from(new Set(models.map((m) => m.category)))];
  const shown = filter === "all" ? models : models.filter((m) => m.category === filter);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="Model inventory"
        sub="what each model does, what it assumes, how it was validated, and where it breaks">
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 12 }}>
          {Object.entries(d?.by_status || {}).map(([k, v]) => (
            <span key={k} style={{ ...S.pill, color: MODEL_STATUS_TONE[k], borderColor: MODEL_STATUS_TONE[k] }}>
              {v} {k}</span>))}
          <span style={{ color: T.dim, fontSize: 12 }}>{d?.total ?? 0} models</span>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {cats.map((c) => (
            <button key={c} onClick={() => setFilter(c)}
              style={{ ...S.tab, ...(filter === c ? S.tabOn : null) }}>{c}</button>))}
        </div>
        {d?.status_definitions && (
          <div style={{ marginTop: 12, fontSize: 11.5, color: T.dim, display: "grid", gap: 4 }}>
            {Object.entries(d.status_definitions).map(([k, v]) => (
              <div key={k}><b style={{ color: MODEL_STATUS_TONE[k] }}>{k}</b> — {v}</div>))}
          </div>)}
      </Panel>

      <div style={{ display: "grid", gap: 10 }}>
        {shown.map((m) => (
          <Panel key={m.id} pad="tight">
            <div onClick={() => setOpen(open === m.id ? null : m.id)}
              style={{ display: "flex", gap: 10, alignItems: "center", cursor: "pointer", flexWrap: "wrap" }}>
              <span style={{ ...S.pill, color: MODEL_STATUS_TONE[m.status], borderColor: MODEL_STATUS_TONE[m.status] }}>
                {m.status}</span>
              <b style={{ fontSize: 13 }}>{m.name}</b>
              <span style={{ color: T.faint, fontSize: 11 }}>{m.category}</span>
              <span style={{ color: T.faint, fontSize: 11, fontFamily: MONO }}>{m.engine}</span>
              <ChevronRight size={14} color={T.dim}
                style={{ marginLeft: "auto", transform: open === m.id ? "rotate(90deg)" : "none" }} />
            </div>
            <div style={{ fontSize: 12.5, color: T.dim, marginTop: 6 }}>{m.purpose}</div>
            {open === m.id && (
              <div style={{ marginTop: 12, display: "grid", gap: 10, fontSize: 12 }}>
                <div><span style={{ color: T.faint }}>ASSUMPTIONS</span>
                  <div style={{ color: T.text, marginTop: 2 }}>{m.assumptions}</div></div>
                <div><span style={{ color: T.amber }}>LIMITATIONS</span>
                  <div style={{ color: T.text, marginTop: 2 }}>{m.limitations}</div></div>
                <div><span style={{ color: MODEL_STATUS_TONE[m.status] }}>VALIDATION EVIDENCE</span>
                  <ul style={{ margin: "4px 0 0 16px", color: T.text, lineHeight: 1.7 }}>
                    {m.validation.map((v, i) => <li key={i}>{v}</li>)}</ul></div>
              </div>)}
          </Panel>))}
      </div>
    </div>
  );
}

/* ---------- AMM / LIQUIDITY (digital assets) ---------- */
// This panel used to claim it called `xrpl_feed.amm_state()` against a public
// rippled node and "streams real reserves", with a note saying the numbers
// were illustrative "for the static preview". That was false in the worst
// direction: this IS the deployed app, it called nothing, and invented
// reserves were presented under a live-data claim. It is now what it actually
// is -- a constant-product calculator -- with the maths executed by the
// backend engines and a genuinely live crypto spot for context.
function LiveAMM() {
  const [reserveBase, setRB] = useState(50000), [reserveQuote, setRQ] = useState(105000);
  const [priceMove, setMove] = useState(1.5);
  const [amountIn, setAmountIn] = useState(1000);
  const [il, setIl] = useState(null);
  const [swap, setSwap] = useState(null);
  const [spotLive, setSpotLive] = useState(null);
  const [err, setErr] = useState(null);

  const poolSpot = reserveQuote / reserveBase;

  // Live crypto marks, for context on what a real pool would be quoting.
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch("/api/marketdata/board?days=30");
        if (!r.ok) return;
        const d = await r.json();
        setSpotLive((d.rows || []).filter((x) => x.asset_class === "CRYPTO" && x.status === "ok"));
      } catch { /* context only */ }
    })();
  }, []);

  // Impermanent loss and swap output are computed by the BACKEND, through the
  // same timed engine seam every other pricing path uses — so these calls show
  // up in the profiler and Grafana rather than being invisible browser maths.
  const compute = async () => {
    setErr(null);
    try {
      const [ilR, swR] = await Promise.all([
        fetch(`/api/crypto/amm/impermanent-loss?price_ratio=${priceMove}`).then((r) => r.ok ? r.json() : null),
        fetch("/api/crypto/amm/swap", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reserve_in: reserveBase, reserve_out: reserveQuote,
                                 amount_in: Number(amountIn), fee: 0.003 }) })
          .then((r) => r.ok ? r.json() : null),
      ]);
      if (!ilR || !swR) throw new Error("backend unavailable");
      setIl(ilR); setSwap(swR);
    } catch (e) { setErr(String(e.message || e)); }
  };
  useEffect(() => { compute(); /* eslint-disable-next-line */ }, [priceMove, reserveBase, reserveQuote, amountIn]);

  const ilPct = il?.impermanent_loss != null ? il.impermanent_loss * 100 : null;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Panel title="AMM / Liquidity — constant-product calculator"
        sub="x·y=k · impermanent loss and swap execution priced by the backend engine">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
          <Big l="Reserve (base)" v={reserveBase} /><Big l="Reserve (quote)" v={reserveQuote} />
          <Big l="Pool spot (quote/base)" v={poolSpot} accent={T.teal} />
          <Big l="Pool value (quote terms)" v={reserveQuote * 2} prefix="$" />
        </div>
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
          Reserves are inputs — this is a pool calculator, not a connected pool. Every
          number below is computed server-side by the same engines the pricing screens
          use. Live venue prices for reference:{" "}
          {spotLive?.length
            ? spotLive.map((c) => `${c.symbol} ${fmt(c.last, 2)}`).join(" · ")
            : "loading…"}
        </div>
        {err && <div style={{ color: T.red, fontSize: 12, marginTop: 8 }}>{err}</div>}
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Panel title="Impermanent loss" sub="exact closed form · symmetric in log-price">
          <label style={{ display: "grid", gap: 5, marginBottom: 12 }}>
            <span style={{ color: T.dim, fontSize: 12 }}>Price move since deposit:{" "}
              <b style={{ color: T.text, fontFamily: MONO }}>{priceMove.toFixed(2)}×</b></span>
            <input type="range" min="0.25" max="4" step="0.05" value={priceMove}
              onChange={(e) => setMove(+e.target.value)} /></label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <Big l="Impermanent loss" v={ilPct ?? 0} suffix="%" accent={T.red} />
            <Big l="Fee APR to offset" v={Math.abs(ilPct ?? 0) * 2} suffix="%" /></div>
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
            IL is symmetric in log-price: a 2× move and a 0.5× move cost the LP the
            same. It is a real opportunity cost versus holding, not an accounting
            artefact — it only becomes permanent on withdrawal.
          </div>
        </Panel>

        <Panel title="Swap execution" sub="constant-product output, 30bp fee · priced by /api/crypto/amm/swap">
          <div style={{ display: "grid", gap: 10 }}>
            {[["Base reserve", reserveBase, setRB, 1000],
              ["Quote reserve", reserveQuote, setRQ, 1000],
              ["Amount in", amountIn, setAmountIn, 100]].map(([l, v, set, st]) => (
              <label key={l} style={{ display: "grid", gridTemplateColumns: "104px 1fr",
                alignItems: "center", gap: 8 }}>
                <span style={{ color: T.dim, fontSize: 12 }}>{l}</span>
                <input type="number" value={v} step={st}
                  onChange={(e) => set(+e.target.value || 0)} style={S.num} /></label>))}
          </div>
          {swap && (
            <div style={{ display: "grid", gap: 7, marginTop: 12 }}>
              <KV k="Amount out" v={fmt(swap.amount_out, 4)} />
              <KV k="Effective price" v={fmt(swap.effective_price ?? (swap.amount_out / amountIn), 6)} />
              <KV k="Price impact" v={swap.price_impact != null
                ? `${fmt(swap.price_impact * 100, 3)}%` : "—"} tone={T.amber} />
              <KV k="Fee paid" v={fmt(swap.fee_paid ?? amountIn * 0.003, 4)} />
            </div>)}
          <div style={{ fontSize: 11.5, color: T.dim, marginTop: 10 }}>
            k = {(reserveBase * reserveQuote).toExponential(2)}. Price impact grows with
            trade size relative to reserves — the reason a large order is split across
            venues rather than sent to one pool.
          </div>
        </Panel>
      </div>
    </div>
  );
}

/* ---------- shared ---------- */

/* ================= CRYPTO DESK — one umbrella for digital assets =========
   Every crypto screen lives here: the trade lifecycle, perps and funding,
   AMM liquidity, and liquidation risk. They used to be four sibling nav
   entries next to FX and rates, which put BTCUSDT three clicks from a
   Treasury curve and implied the two were comparable books.

   The boundary is enforced on the API as well, not just visually:
   /api/oms/blotter and /api/marketdata/board default to desk=traditional and
   exclude digital assets. Crypto marks are live and 24/7; the rest of the
   console is end-of-day. Blending them produces a firm-wide P&L nobody can
   act on.

   Nothing here reaches an exchange. The API has no submit endpoint. ====== */

const CD = {                     // digital-asset palette, distinct on purpose
  amber: "#F2A93B", chain: "#4DD4AC", violet: "#8B7BF0",
};

const STAGES = [
  ["market_data", "Market data"], ["pre_trade_risk", "Pre-trade risk"],
  ["schedule", "Parent → children"], ["execution", "Ack & fill"],
  ["position", "Position"], ["tca", "TCA"],
  ["reconciliation", "Reconciliation"],
];

function CryptoDeskConsole() {
  const [tab, setTab] = useState("lifecycle");
  const TABS = [
    ["lifecycle", "Trade lifecycle"], ["ticket", "Order ticket"],
    ["perps", "Perps & funding"], ["amm", "AMM / liquidity"],
    ["liq", "Liquidation risk"],
  ];
  return <div style={{ display: "grid", gap: 14 }}>
    <DeskHeader />
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {TABS.map(([id, label]) =>
        <button key={id} onClick={() => setTab(id)} style={{
          width: "auto", margin: 0, padding: "6px 13px", borderRadius: 7,
          fontSize: 12.5, fontWeight: 600, cursor: "pointer",
          border: `1px solid ${tab === id ? CD.amber : T.lineSoft}`,
          background: tab === id ? `${CD.amber}1A` : "transparent",
          color: tab === id ? CD.amber : T.dim,
        }}>{label}</button>)}
    </div>
    {tab === "lifecycle" && <Lifecycle />}
    {tab === "ticket" && <OrderTicket />}
    {tab === "perps" && <CryptoDesk tab="crypto" />}
    {tab === "amm" && <LiveAMM />}
    {tab === "liq" && <CryptoDesk tab="liq" />}
  </div>;
}

function DeskHeader() {
  const [h, setH] = useState(null);
  useEffect(() => {
    fetch("/api/cryptodesk/health").then(r => r.json()).then(setH).catch(() => {});
  }, []);
  const live = h?.available;
  return <section style={{
    ...S.panel, padding: 14, borderLeft: `3px solid ${CD.amber}`,
    display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap",
  }}>
    <div>
      <div style={{ fontSize: 13.5, fontWeight: 600 }}>Digital Assets Desk</div>
      <div style={{ color: T.dim, fontSize: 11, marginTop: 2 }}>
        Separate venues, separate custody, 24/7 hours — and separate from the
        FX, rates and credit books by design.
      </div>
    </div>
    <div style={{ marginLeft: "auto", display: "flex", gap: 16, flexWrap: "wrap" }}>
      <Tag label="network" value={h?.network || "…"}
           tone={h?.mainnet_unlocked ? T.red : CD.chain} />
      <Tag label="order submission" value={h?.order_submission ? "EXPOSED" : "not exposed"}
           tone={h?.order_submission ? T.red : CD.chain} />
      <Tag label="engine" value={live ? `v${h.version}` : "unavailable"}
           tone={live ? CD.chain : T.red} />
    </div>
  </section>;
}

const Tag = ({ label, value, tone }) => <div>
  <div style={{ color: T.dim, fontSize: 10, textTransform: "uppercase",
                letterSpacing: ".06em" }}>{label}</div>
  <div style={{ fontFamily: MONO, fontSize: 12.5, color: tone || T.text,
                marginTop: 2 }}>{value}</div></div>;

/* ---- the whole lifecycle, one parent order, seven stages ---- */
function Lifecycle() {
  const [form, setForm] = useState({ market: "BTC-USD-PERP", side: "BUY",
                                     size: "3.0", algo: "TWAP", slices: 6 });
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const run = () => {
    setBusy(true); setErr(null);
    fetch("/api/cryptodesk/lifecycle", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...form, slices: Number(form.slices) || 6 }),
    }).then(r => r.json().then(j => ({ ok: r.ok, j })))
      .then(({ ok, j }) => {
        // Clear the previous result on failure. Leaving it rendered beside a
        // blank verdict reads as a broken screen - which is exactly how this
        // looked the first time it shipped.
        if (!ok) { setData(null); setErr(j.detail || j.error || "request failed"); }
        else { setData(j); }
      })
      .catch(e => { setData(null); setErr(String(e)); })
      .finally(() => setBusy(false));
  };
  useEffect(run, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const st = data?.stages;
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return <div style={{ display: "grid", gap: 14 }}>
    <Panel title="Parent order"
           sub="Worked as child orders against a live-seeded book. Simulated end to end — nothing is sent.">
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "end" }}>
        <Fld label="Market"><select style={{ ...S.num, width: 160 }} value={form.market}
          onChange={set("market")}>
          <option>BTC-USD-PERP</option><option>ETH-USD-PERP</option></select></Fld>
        <Fld label="Side"><select style={{ ...S.num, width: 90 }} value={form.side}
          onChange={set("side")}><option>BUY</option><option>SELL</option></select></Fld>
        <Fld label="Size"><input style={{ ...S.num, width: 100 }} value={form.size}
          onChange={set("size")} /></Fld>
        <Fld label="Algo"><select style={{ ...S.num, width: 100 }} value={form.algo}
          onChange={set("algo")}><option>TWAP</option><option>VWAP</option></select></Fld>
        <Fld label="Slices"><input style={{ ...S.num, width: 70 }} value={form.slices}
          onChange={set("slices")} /></Fld>
        <button onClick={run} disabled={busy} style={{
          width: "auto", margin: 0, padding: "8px 18px", border: `1px solid ${CD.amber}`,
          background: CD.amber, color: "#151A23", borderRadius: 7,
          fontWeight: 700, fontSize: 12.5, cursor: busy ? "wait" : "pointer" }}>
          {busy ? "working…" : "Run lifecycle"}</button>
      </div>
      {err && <div style={{ marginTop: 12, borderLeft: `3px solid ${T.red}`,
                            padding: "8px 11px", fontFamily: MONO, fontSize: 12,
                            color: T.red }}>{err}</div>}
      {data && <div style={{ color: T.dim, fontSize: 11.5, marginTop: 11 }}>
        arrival mid <span style={{ fontFamily: MONO, color: T.text }}>
        {data.arrival_price}</span> · sent to exchange:{" "}
        <span style={{ fontFamily: MONO, color: CD.chain }}>
        {String(data.sent_to_exchange)}</span></div>}
    </Panel>

    {st && <div style={{ display: "grid",
                         gridTemplateColumns: "repeat(auto-fit,minmax(255px,1fr))",
                         gap: 12 }}>
      {STAGES.map(([key, label], i) =>
        <StageCard key={key} n={i + 1} label={label} data={st[key]} k={key} />)}
    </div>}
  </div>;
}

const Fld = ({ label, children }) => <div>
  <div style={{ color: T.dim, fontSize: 10.5, marginBottom: 4,
                textTransform: "uppercase", letterSpacing: ".05em" }}>{label}</div>
  {children}</div>;

function StageCard({ n, label, data, k }) {
  const rows = stageRows(k, data);
  const bad = k === "pre_trade_risk" ? data && !data.cleared
            : k === "reconciliation" ? data && !data.clean : false;
  return <section style={{ ...S.panel, padding: 13,
                           borderTop: `2px solid ${bad ? T.red : CD.chain}` }}>
    <div style={{ display: "flex", gap: 8, alignItems: "baseline", marginBottom: 9 }}>
      <span style={{ fontFamily: MONO, fontSize: 10.5, color: CD.amber }}>
        {String(n).padStart(2, "0")}</span>
      <span style={{ fontSize: 12, fontWeight: 650 }}>{label}</span>
    </div>
    {rows.length === 0
      ? <div style={{ color: T.dim, fontSize: 11.5 }}>—</div>
      : rows.map(([kk, vv]) =>
        <div key={kk} style={{ display: "flex", justifyContent: "space-between",
                               gap: 10, padding: "2px 0" }}>
          <span style={{ color: T.dim, fontSize: 11 }}>{kk}</span>
          <span style={{ fontFamily: MONO, fontSize: 11.5, textAlign: "right",
                         wordBreak: "break-all" }}>{vv}</span></div>)}
  </section>;
}

function stageRows(k, d) {
  if (!d) return [];
  const f = (v) => v === null || v === undefined ? "—" : String(v);
  switch (k) {
    case "market_data": return [["source", f(d.source)], ["bid", f(d.bid)],
      ["ask", f(d.ask)], ["spread", `${f(d.spread_bps)} bps`],
      ["depth levels", f(d.depth_levels)]];
    case "pre_trade_risk": return [["cleared", d.cleared ? "yes" : "NO"],
      ["notional", f(d.notional)],
      ...(d.checks || []).map(c => [c.code, c.message.slice(0, 40)])];
    case "schedule": return [["algo", f(d.algo)], ["slices", (d.slices || []).length],
      ["sums to parent", d.sums_to_parent ? "exact" : "DRIFT"],
      ["sizes", (d.slices || []).map(s => s.size).join(", ").slice(0, 46)]];
    case "execution": return [["status", f(d.status)], ["filled", f(d.filled_size)],
      ["avg price", f(d.avg_price)], ["children", (d.children || []).length],
      ["fills", (d.fills || []).length]];
    case "position": return [["size", f(d.size)], ["entry", f(d.entry_price)],
      ["unrealised", f(d.unrealised_pnl)], ["margin ratio", f(d.margin_ratio)],
      ["liquidation", f(d.liquidation_price)]];
    case "tca": return [["shortfall", `${f(d.implementation_shortfall_bps)} bps`],
      ["vs VWAP", `${f(d.vwap_slippage_bps)} bps`],
      ["fees", `${f(d.fee_bps)} bps`],
      ["all-in", `${f(d.total_cost_bps)} bps`],
      ["participation", `${f(d.participation_rate_pct)} %`]];
    case "reconciliation": return [["clean", d.clean ? "yes" : "NO"],
      ["blotter rows", f(d.blotter_rows)],
      ...Object.entries(d).filter(([kk]) =>
        !["clean", "blotter_rows"].includes(kk)).map(([kk, vv]) =>
        [kk, Array.isArray(vv) ? vv.length : f(vv)])];
    default: return Object.entries(d).slice(0, 6).map(([kk, vv]) => [kk, f(vv)]);
  }
}

/* ---- order ticket: the same engine, two levels of disclosure ---- */
function OrderTicket() {
  const [t, setT] = useState({ venue: "paper", market: "BTC-USD-PERP",
                               side: "BUY", type: "LIMIT", size: "0.01", price: "" });
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const [book, setBook] = useState(null);

  useEffect(() => {
    let live = true;
    const load = () => fetch(`/api/cryptodesk/book?venue=${t.venue}&market=${t.market}`)
      .then(r => r.ok ? r.json() : null).then(d => { if (live) setBook(d); })
      .catch(() => {});
    load(); const id = setInterval(load, 8000);
    return () => { live = false; clearInterval(id); };
  }, [t.venue, t.market]);

  useEffect(() => {
    const id = setTimeout(() => {
      fetch("/api/cryptodesk/validate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(t),
      }).then(r => r.json().then(j => ({ ok: r.ok, j })))
        .then(({ ok, j }) => {
          // Never leave a stale pass rendered under a fresh failure.
          if (!ok) { setRes(null); setErr(j.detail || j.error || "invalid order"); }
          else { setRes(j); setErr(null); }
        })
        .catch(e => { setRes(null); setErr(String(e)); });
    }, 250);
    return () => clearTimeout(id);
  }, [t]);

  const set = (k) => (e) => setT({ ...t, [k]: e.target.value });
  const rej = res?.desk?.rejections || [];

  return <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 14 }}>
    <div style={{ display: "grid", gap: 14, alignContent: "start" }}>
      <Panel title="Ticket" pad="tight">
        <Fld label="Venue"><select style={{ ...S.num, width: "100%" }} value={t.venue}
          onChange={set("venue")}><option value="paper">paper — real matching</option>
          <option value="binance">binance — real matching</option>
          <option value="paradex">paradex — signed orders</option></select></Fld>
        <Fld label="Market"><input style={{ ...S.num, width: "100%" }} value={t.market}
          onChange={set("market")} /></Fld>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
          <Fld label="Side"><select style={{ ...S.num, width: "100%" }} value={t.side}
            onChange={set("side")}><option>BUY</option><option>SELL</option></select></Fld>
          <Fld label="Type"><select style={{ ...S.num, width: "100%" }} value={t.type}
            onChange={set("type")}><option>LIMIT</option><option>MARKET</option></select></Fld>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
          <Fld label="Size"><input style={{ ...S.num, width: "100%" }} value={t.size}
            onChange={set("size")} /></Fld>
          <Fld label="Price"><input style={{ ...S.num, width: "100%" }} value={t.price}
            onChange={set("price")}
            placeholder={book?.mid ? Number(book.mid).toFixed(2) : "—"} /></Fld>
        </div>
      </Panel>
      <Panel title="Order book" sub={book ? `${book.source} · ${book.spread_bps ?? "—"} bps` : "—"} pad="tight">
        <table style={{ width: "100%", borderCollapse: "collapse",
                        fontFamily: MONO, fontSize: 11.5 }}>
          <tbody>{Array.from({ length: Math.min(6, book?.bids?.length || 0) }).map((_, i) => {
            const b = book.bids[i], a = book.asks?.[i];
            return <tr key={i}>
              <td style={{ textAlign: "right", color: T.dim }}>{b?.size}</td>
              <td style={{ textAlign: "right", color: T.green }}>{b?.price}</td>
              <td style={{ textAlign: "right", color: T.red }}>{a?.price}</td>
              <td style={{ textAlign: "right", color: T.dim }}>{a?.size}</td></tr>;
          })}</tbody></table>
      </Panel>
    </div>

    <div style={{ display: "grid", gap: 14, alignContent: "start" }}>
      <Panel title="What a retail app shows you"
             sub="One line. An exchange app tells you it failed, not which rule you broke.">
        <div style={{ fontSize: 16, fontWeight: 600, padding: "8px 0",
          color: err ? T.red : res ? (res.retail.accepted ? T.green : T.red) : T.dim }}>
          {err ? "Order failed. Please check your order details."
               : res ? res.retail.message : "—"}</div>
      </Panel>
      <Panel title="What the desk sees" sub={res?.desk?.stages?.join(" → ") || "—"}>
        {err && <div style={{ borderLeft: `3px solid ${T.red}`, padding: "8px 11px",
                              fontFamily: MONO, fontSize: 12, color: T.red }}>{err}</div>}
        {!err && res && (res.desk.repairs || []).map((c, i) =>
          <div key={i} style={{ borderLeft: `3px solid ${T.amber}`, padding: "6px 10px",
            margin: "6px 0", fontFamily: MONO, fontSize: 11.5, color: T.amber }}>
            auto-repaired: {c}</div>)}
        {!err && res && rej.length === 0 &&
          <div style={{ color: T.green, fontFamily: MONO, fontSize: 12 }}>
            passed all {res.desk.rules_evaluated} rules</div>}
        {!err && rej.map((r, i) =>
          <div key={i} style={{ border: `1px solid ${T.lineSoft}`,
            borderLeft: `3px solid ${T.red}`, borderRadius: 6,
            padding: "8px 10px", margin: "6px 0" }}>
            <div style={{ fontFamily: MONO, fontSize: 11.5, color: T.red,
                          fontWeight: 600 }}>{r.code}
              <span style={{ color: T.dim, fontWeight: 400 }}> · {r.stage}</span></div>
            <div style={{ fontSize: 12.5, margin: "3px 0" }}>{r.message}</div>
          </div>)}
        <div style={{ color: T.dim, fontSize: 11.5, marginTop: 12, paddingTop: 10,
                      borderTop: `1px solid ${T.lineSoft}` }}>
          Same order, same engine. The rejection <em>codes</em> are the payload —
          an execution system branches on them; free text cannot be.
        </div>
      </Panel>
    </div>
  </div>;
}

function Panel({ title, sub, children, pad }) {
  return <section style={{ ...S.panel, padding: pad === "tight" ? 14 : 16 }}>
    {title && <div style={{ marginBottom: 12 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>{title}</div>
      {sub && <div style={{ color: T.dim, fontSize: 11, marginTop: 2 }}>{sub}</div>}</div>}{children}</section>;
}
const KV = ({ k, v, d, tone }) => <div><div style={{ color: T.dim, fontSize: 11 }}>{k}</div>
  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
    <span style={{ fontFamily: MONO, fontSize: 15, color: tone || T.text }}>{v}</span>{d !== undefined && <Delta v={d} />}</div></div>;
const Delta = ({ v }) => v === 0 ? <span style={{ color: T.dim, fontSize: 11.5 }}>flat</span> :
  <span style={{ color: v > 0 ? T.green : T.red, fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 2 }}>
    {v > 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}{Math.abs(v)}%</span>;
const Big = ({ l, v, accent, prefix = "", suffix = "" }) => <div style={{ background: T.panel2, border: `1px solid ${T.lineSoft}`, borderRadius: 8, padding: 13 }}>
  <div style={{ color: T.dim, fontSize: 11 }}>{l}</div>
  <div style={{ fontFamily: MONO, fontSize: 23, fontWeight: 600, marginTop: 5, color: accent || T.text }}>{prefix}{fmt(v)}{suffix}</div></div>;
const Bar = ({ pct, c }) => <div style={{ height: 6, background: T.lineSoft, borderRadius: 4, overflow: "hidden" }}>
  <div style={{ height: "100%", width: `${pct}%`, background: c, borderRadius: 4 }} /></div>;
function DistBar({ p50, p95, p99, max, c }) {
  const w = (v) => `${Math.min(100, (v / max) * 100)}%`;
  return <div style={{ position: "relative", height: 20, background: T.lineSoft, borderRadius: 4 }}>
    <div style={{ position: "absolute", inset: 0, width: w(p99), background: `${c}22`, borderRadius: 4 }} />
    <div style={{ position: "absolute", inset: 0, width: w(p95), background: `${c}55`, borderRadius: 4 }} />
    <div style={{ position: "absolute", inset: 0, width: w(p50), background: c, borderRadius: 4 }} /></div>;
}
function Spark({ c }) {
  const d = [3, 5, 4, 6, 7, 6, 8], mn = Math.min(...d), mx = Math.max(...d);
  return <svg width="110" height="34"><polyline fill="none" stroke={c} strokeWidth="1.6"
    points={d.map((v, i) => `${i / (d.length - 1) * 110},${34 - (v - mn) / (mx - mn) * 34}`).join(" ")} /></svg>;
}
function Arc({ score }) {
  const b = band(score), R = 74, cx = 90, cy = 90, a = Math.PI + score / 100 * Math.PI;
  const pt = (x) => `${cx + R * Math.cos(x)},${cy + R * Math.sin(x)}`;
  return <svg width="180" height="112" viewBox="0 0 180 112">
    <path d={`M ${pt(Math.PI)} A ${R} ${R} 0 1 1 ${pt(2 * Math.PI)}`} fill="none" stroke={T.line} strokeWidth="12" strokeLinecap="round" />
    <path d={`M ${pt(Math.PI)} A ${R} ${R} 0 ${a - Math.PI > Math.PI ? 1 : 0} 1 ${pt(a)}`} fill="none" stroke={b.c} strokeWidth="12" strokeLinecap="round" />
    <text x={cx} y={cy - 4} textAnchor="middle" fontFamily={MONO} fontSize="36" fontWeight="600" fill={T.text}>{score}</text>
    <text x={cx} y={cy + 16} textAnchor="middle" fontSize="10.5" letterSpacing="1.5" fill={b.c}>{b.k}</text></svg>;
}

/* ---------- styles ---------- */
const S = {
  root: { display: "flex", height: "100vh", background: T.bg, color: T.text, fontFamily: "'IBM Plex Sans',system-ui,sans-serif", fontSize: 13 },
  rail: { width: 230, borderRight: `1px solid ${T.line}`, background: T.panel2, display: "flex", flexDirection: "column", flexShrink: 0 },
  brand: { display: "flex", gap: 10, alignItems: "center", padding: "15px 15px 13px", borderBottom: `1px solid ${T.line}` },
  mark: { width: 28, height: 28, display: "grid", placeItems: "center", background: T.teal, color: T.bg, borderRadius: 7, fontSize: 15 },
  groupLbl: { color: T.faint, fontSize: 9.5, letterSpacing: 1.3, padding: "0 8px 5px" },
  nav: { display: "flex", alignItems: "center", gap: 10, width: "100%", background: "none", border: "none", padding: "7px 10px", borderRadius: 7, cursor: "pointer", textAlign: "left" },
  navOn: { background: T.panel },
  footer: {
    borderTop: `1px solid ${T.line}`, background: T.panel2,
    padding: "10px 18px", fontSize: 11, color: T.dim,
    textAlign: "center", letterSpacing: 0.2, flexShrink: 0,
  },
  railFoot: { padding: 13, borderTop: `1px solid ${T.line}`, display: "flex", alignItems: "center", gap: 8 },
  topbar: { height: 52, borderBottom: `1px solid ${T.line}`, background: T.panel2, display: "flex", alignItems: "center", gap: 16, padding: "0 18px", flexShrink: 0 },
  searchWrap: { flex: 1, maxWidth: 440, display: "flex", alignItems: "center", gap: 8, background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "0 10px", height: 33 },
  search: { flex: 1, background: "none", border: "none", color: T.text, outline: "none", fontSize: 12.5 },
  kbd: { display: "inline-flex", alignItems: "center", gap: 3, color: T.dim, fontSize: 10.5, border: `1px solid ${T.line}`, borderRadius: 5, padding: "2px 5px" },
  ctx: { display: "flex", flexDirection: "column", lineHeight: 1.25, paddingRight: 12, borderRight: `1px solid ${T.lineSoft}` },
  sim: { fontSize: 9.5, letterSpacing: 0.6, color: T.amber, border: `1px solid ${T.amber}`, borderRadius: 5, padding: "4px 7px", whiteSpace: "nowrap" },
  canvas: { padding: 18, overflowY: "auto", overflowX: "hidden", flex: 1 },
  iconBtn: { background: "none", border: `1px solid ${T.line}`, color: T.text,
             borderRadius: 7, width: 32, height: 30, cursor: "pointer",
             alignItems: "center", justifyContent: "center", fontSize: 14, flexShrink: 0 },
  panel: { background: T.panel, border: `1px solid ${T.line}`, borderRadius: 10 },
  alert: { display: "flex", alignItems: "center", gap: 10, padding: "8px 10px", background: T.panel2, border: `1px solid ${T.lineSoft}`, borderRadius: 7 },
  pill: { fontSize: 9.5, letterSpacing: 0.5, border: "1px solid", borderRadius: 4, padding: "2px 5px" },
  table: { width: "100%", borderCollapse: "collapse" },
  th: { color: T.dim, fontSize: 11, fontWeight: 500, padding: "8px 10px", borderBottom: `1px solid ${T.line}` },
  td: { padding: "9px 10px", borderBottom: `1px solid ${T.lineSoft}`, fontSize: 13 },
  tdN: { padding: "9px 10px", borderBottom: `1px solid ${T.lineSoft}`, fontSize: 13, fontFamily: MONO, textAlign: "right" },
  seg: { flex: 1, padding: "8px", background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 7, color: T.dim, cursor: "pointer", fontSize: 12.5 },
  segOn: { background: T.teal, color: T.bg, borderColor: T.teal, fontWeight: 600 },
  tab: { padding: "8px 14px", background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 7, color: T.dim, cursor: "pointer", fontSize: 12.5 },
  tabOn: { background: T.panel, color: T.text, borderColor: T.teal },
  num: { background: T.bg, border: `1px solid ${T.line}`, borderRadius: 7, color: T.text, padding: "8px 10px", fontFamily: MONO, fontSize: 13, outline: "none", width: "100%", boxSizing: "border-box" },
  jump: { display: "inline-flex", alignItems: "center", gap: 4, background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 7, padding: "8px 12px", color: T.blue, fontSize: 12.5, cursor: "pointer" },
  chip: { textAlign: "left", background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, padding: "9px 11px", color: T.blue, fontSize: 12.5, cursor: "pointer" },
  bubbleB: { maxWidth: "86%", background: T.panel2, border: `1px solid ${T.lineSoft}`, borderRadius: "10px 10px 10px 3px", padding: "10px 13px", fontSize: 13, lineHeight: 1.55 },
  bubbleU: { maxWidth: "86%", background: T.teal, color: T.bg, borderRadius: "10px 10px 3px 10px", padding: "10px 13px", fontSize: 13, lineHeight: 1.5 },
  sendBtn: { background: T.teal, color: T.bg, border: "none", borderRadius: 8, padding: "0 16px", fontWeight: 600, cursor: "pointer", fontSize: 13 },
};
const CSS = `
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
*{box-sizing:border-box}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:${T.line};border-radius:6px}::-webkit-scrollbar-track{background:transparent}
.nav:hover{background:${T.panel}}.tr:hover td{background:${T.panel2}}
input[type=number]::-webkit-inner-spin-button{opacity:.3}
input[type=range]{accent-color:${T.teal}}

/* The rail is permanent on desktop, so its toggle and close button are not. */
.navtoggle,.navclose{display:none}
.scrim{display:none}

/* Wide tables (blotter, board, VaR suite) scroll inside their own panel
   instead of forcing the whole page to scroll sideways. */
.panelbody{overflow-x:auto;-webkit-overflow-scrolling:touch}

@media (max-width: 900px){
  /* Drawer: off-canvas by default, slides in over the content. Transform
     rather than width so it animates on the compositor and never reflows
     the canvas underneath. */
  .rail{position:fixed;top:0;left:0;bottom:0;z-index:40;transform:translateX(-100%);
        transition:transform .18s ease-out;box-shadow:0 0 40px rgba(0,0,0,.5)}
  .rail.open{transform:translateX(0)}
  .scrim{display:block;position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.55)}
  .navtoggle,.navclose{display:inline-flex}
  .navclose{margin-left:auto}
  /* Context chips and the search hint are the first things to go: they are
     labels, not controls, and the toggle plus title must stay reachable. */
  .ctxwrap{display:none}
  .kbd{display:none}
  .searchwrap{max-width:none}
  /* Every fixed multi-column grid collapses to one column. Declared once
     here rather than per-component so no screen can be missed. */
  .shell [style*="grid-template-columns"],.shell [style*="gridTemplateColumns"]{
    grid-template-columns:1fr !important}
  /* Each table becomes its own horizontal scroller. Done globally so a wide
     table can never force the whole page to scroll sideways -- the failure
     that makes a desktop dashboard unusable on a phone. */
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;
        white-space:nowrap;font-size:11.5px;max-width:100%}
  main{padding:12px !important}
  header{padding:0 10px !important;gap:10px !important}
}
@media (max-width: 560px){
  .shell{font-size:12px}
  h1,h2{font-size:16px}
}
`;
