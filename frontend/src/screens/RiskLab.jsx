/* RiskLab.jsx — VaR/ES on real returns, and the pricing engines plotted.
 *
 * EVERY NUMBER ON THIS SCREEN IS COMPUTED, NOT ILLUSTRATED. The return series
 * is a real FRED index history pulled through /api/marketdata/series; the VaR
 * and ES figures come from /api/risk/var over those returns; the option prices
 * and Greeks come from the C++ engines through /api/pricing. Nothing is drawn
 * from a shape that looks about right -- if an endpoint fails, the panel says
 * so and plots nothing.
 *
 * WHY THE DISTRIBUTION IS THE MAIN CHART. VaR as a single number tells you a
 * threshold and hides the shape that produced it. Two books with identical 99%
 * VaR can have completely different tails, and the one that matters is the
 * difference between them -- which is exactly what ES measures and what the
 * histogram shows. So the distribution is the chart and the numbers sit on it,
 * rather than four numbers in boxes.
 *
 * WHY FOUR METHODS SIDE BY SIDE. Historical, parametric, Monte Carlo and
 * Cornish-Fisher disagree, and the size of the disagreement is the useful
 * signal: parametric assumes normality, so where it sits well below historical
 * the returns have a fat left tail the normal assumption is not seeing.
 */
import React, { useEffect, useState } from "react";
import { T, MONO } from "../theme.js";
import { Histogram, GroupedBars, LineChart, useSeriesColors } from "../components/Charts.jsx";

const getJSON = (u) => fetch(u).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${u}`))));
const postJSON = (u, b) =>
  fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${u}`))));

function Box({ title, sub, children, right }) {
  return (
    <section style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 9, marginBottom: 14, overflow: "hidden" }}>
      <header style={{ padding: "11px 14px", borderBottom: `1px solid ${T.lineSoft}`, display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>{title}</div>
          {sub && <div style={{ fontSize: 11, color: T.dim, marginTop: 3, lineHeight: 1.55, maxWidth: 760 }}>{sub}</div>}
        </div>
        {right}
      </header>
      <div style={{ padding: 14 }}>{children}</div>
    </section>
  );
}

const Pill = ({ children, tone = T.dim }) => (
  <span style={{ padding: "2px 8px", borderRadius: 20, fontSize: 9.5, letterSpacing: 0.7, color: tone,
                 border: `1px solid ${tone}`, fontFamily: MONO, textTransform: "uppercase", whiteSpace: "nowrap" }}>{children}</span>
);

const Err = ({ msg }) => (
  <div style={{ border: `1px solid ${T.amber}`, borderRadius: 7, padding: 11, fontSize: 11.5, color: T.dim, lineHeight: 1.6 }}>
    <Pill tone={T.amber}>no data</Pill>
    <div style={{ marginTop: 7 }}>{msg}</div>
    <div style={{ marginTop: 6, fontStyle: "italic", color: T.faint }}>
      Nothing is plotted rather than plotting a placeholder: a chart that renders
      without its data is indistinguishable from one that worked.
    </div>
  </div>
);

const lbl = { color: T.faint, fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase", display: "block", marginBottom: 4 };
const inp = { background: T.panel2, border: `1px solid ${T.line}`, color: T.text, borderRadius: 6, padding: "6px 9px", fontFamily: MONO, fontSize: 12, width: "100%" };
const Field = ({ label, w = 92, ...r }) => (
  <label style={{ display: "block", width: w }}><span style={lbl}>{label}</span><input style={inp} {...r} /></label>
);

/* ------------------------------------------------- VaR / ES on real returns */
function VarPanel({ colors }) {
  const [symbol, setSymbol] = useState("SP500");
  const [conf, setConf] = useState(0.99);
  const [state, setState] = useState({ loading: true });

  const run = async (sym = symbol, c = conf) => {
    setState({ loading: true });
    try {
      const s = await getJSON(`/api/marketdata/series/${encodeURIComponent(sym)}`);
      const px = (s.points || []).map((p) => p.value).filter((v) => typeof v === "number" && v > 0);
      if (px.length < 60) throw new Error(`${sym} returned ${px.length} usable points; VaR needs a meaningful history.`);
      const rets = [];
      for (let i = 1; i < px.length; i++) rets.push(px[i] / px[i - 1] - 1);
      const res = await postJSON("/api/risk/var", { method: "historical", confidence: c, returns: rets });
      setState({ loading: false, rets, res, label: s.label || sym, n: rets.length });
    } catch (e) {
      setState({ loading: false, error: e.message });
    }
  };
  useEffect(() => { run(); }, []);

  const { loading, rets, res, error, label, n } = state;
  const hist = res?.find((r) => r.method === "historical");

  return (
    <Box title="Value at Risk and Expected Shortfall — on a real return series"
         sub="VaR is a threshold; it says nothing about what lies beyond it. Two books with the same 99% VaR can have very different tails, and the difference is what ES measures. So the distribution is the chart and the thresholds sit on it."
         right={<Pill tone={T.teal}>{n ? `${n} daily returns` : "loading"}</Pill>}>
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", marginBottom: 12, flexWrap: "wrap" }}>
        <Field label="Series" w={130} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
        <label style={{ display: "block", width: 110 }}>
          <span style={lbl}>Confidence</span>
          <select style={inp} value={conf} onChange={(e) => { setConf(+e.target.value); run(symbol, +e.target.value); }}>
            <option value={0.95}>95%</option><option value={0.99}>99%</option><option value={0.999}>99.9%</option>
          </select>
        </label>
        <button style={{ background: T.teal, color: T.bg, border: "none", borderRadius: 6, padding: "7px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
                onClick={() => run()}>Recompute</button>
      </div>

      {loading && <div style={{ fontSize: 11.5, color: T.faint }}>Fetching the series and computing…</div>}
      {error && <Err msg={error} />}

      {!loading && !error && hist && (
        <>
          <Histogram
            values={rets}
            bins={40}
            xLabel={`${label} daily returns — the two dashed lines are the ${(conf * 100).toFixed(conf === 0.999 ? 1 : 0)}% VaR and ES`}
            markers={[
              { x: -hist.var, label: `VaR ${(hist.var * 100).toFixed(2)}%`, color: colors[2] },
              { x: -hist.es, label: `ES ${(hist.es * 100).toFixed(2)}%`, color: colors[4] },
            ]}
          />
          <div style={{ marginTop: 14 }}>
            <GroupedBars
              groups={res.map((r) => ({ label: r.method.replace("_", " "), values: [r.var * 100, r.es * 100] }))}
              seriesNames={["VaR %", "ES %"]}
              colors={[colors[1], colors[4]]}
              unit="%"
            />
          </div>
          <div style={{ fontSize: 11, color: T.dim, lineHeight: 1.65, marginTop: 10, borderTop: `1px solid ${T.lineSoft}`, paddingTop: 10 }}>
            ES exceeds VaR for every method, which it must — it is the mean of the
            losses beyond the threshold, so a method reporting ES below its own VaR
            would be arithmetically impossible rather than merely wrong. Where
            <strong style={{ color: T.text }}> parametric</strong> sits below
            <strong style={{ color: T.text }}> historical</strong>, the normal
            assumption is understating a tail the actual returns contain.
          </div>
          <details style={{ marginTop: 10 }}>
            <summary style={{ fontSize: 11, color: T.faint, cursor: "pointer" }}>Table view</summary>
            <table style={{ width: "100%", marginTop: 8, fontFamily: MONO, fontSize: 11, borderCollapse: "collapse" }}>
              <thead><tr>{["method", "VaR", "ES", "ES/VaR", "µs"].map((h) => (
                <th key={h} style={{ textAlign: "right", color: T.faint, fontWeight: 400, padding: "3px 8px", borderBottom: `1px solid ${T.lineSoft}` }}>{h}</th>))}</tr></thead>
              <tbody>{res.map((r) => (
                <tr key={r.method}>
                  <td style={{ textAlign: "left", padding: "3px 8px", color: T.dim }}>{r.method}</td>
                  <td style={{ textAlign: "right", padding: "3px 8px" }}>{(r.var * 100).toFixed(3)}%</td>
                  <td style={{ textAlign: "right", padding: "3px 8px" }}>{(r.es * 100).toFixed(3)}%</td>
                  <td style={{ textAlign: "right", padding: "3px 8px", color: T.dim }}>{(r.es / r.var).toFixed(2)}×</td>
                  <td style={{ textAlign: "right", padding: "3px 8px", color: T.faint }}>{(r.latency_ms * 1000).toFixed(0)}</td>
                </tr>))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </Box>
  );
}

/* ----------------------------------------------------------- Greeks vs spot */
function GreeksPanel({ colors }) {
  const [p, setP] = useState({ strike: 100, rate: 0.05, vol: 0.2, maturity: 1.0, is_call: true });
  const [state, setState] = useState({ loading: true });

  const run = async (q = p) => {
    setState({ loading: true });
    try {
      const spots = [];
      for (let s = q.strike * 0.6; s <= q.strike * 1.4001; s += q.strike * 0.02) spots.push(+s.toFixed(4));
      const out = await Promise.all(spots.map((spot) =>
        postJSON("/api/pricing/black-scholes", { ...q, spot }).then((r) => ({ spot, ...r }))));
      setState({ loading: false, out });
    } catch (e) { setState({ loading: false, error: e.message }); }
  };
  useEffect(() => { run(); }, []);

  const { loading, out, error } = state;
  const line = (key, name, ci) => ({
    name, color: colors[ci],
    points: (out || []).map((o) => [o.spot, o.greeks?.[key]]).filter((pt) => pt[1] != null),
  });

  return (
    <Box title="Greeks across spot — Black-Scholes, computed in C++"
         sub="Each point is a separate call to the pricing engine, not an interpolation. Delta and gamma are plotted apart from vega and theta because they differ by two orders of magnitude, and forcing them onto one axis would flatten gamma into the zero line."
         right={<Pill tone={T.teal}>{out ? `${out.length} engine calls` : "…"}</Pill>}>
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", marginBottom: 12, flexWrap: "wrap" }}>
        {[["strike", "Strike"], ["vol", "Vol"], ["rate", "Rate"], ["maturity", "Maturity (y)"]].map(([k, L]) => (
          <Field key={k} label={L} value={p[k]} onChange={(e) => setP({ ...p, [k]: +e.target.value || 0 })} />
        ))}
        <label style={{ display: "block", width: 92 }}>
          <span style={lbl}>Right</span>
          <select style={inp} value={p.is_call ? "C" : "P"} onChange={(e) => setP({ ...p, is_call: e.target.value === "C" })}>
            <option value="C">Call</option><option value="P">Put</option>
          </select>
        </label>
        <button style={{ background: T.teal, color: T.bg, border: "none", borderRadius: 6, padding: "7px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
                onClick={() => run()}>Recompute</button>
      </div>
      {loading && <div style={{ fontSize: 11.5, color: T.faint }}>Calling the engine across the spot grid…</div>}
      {error && <Err msg={error} />}
      {!loading && !error && out && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 16 }}>
          <div>
            <div style={{ ...lbl, marginBottom: 6 }}>Delta &amp; gamma</div>
            <LineChart series={[line("delta", "Delta", 0), line("gamma", "Gamma", 1)]}
                       xLabel="spot" yFmt={(v) => v.toFixed(3)} />
          </div>
          <div>
            <div style={{ ...lbl, marginBottom: 6 }}>Vega &amp; theta</div>
            <LineChart series={[line("vega", "Vega", 2), line("theta", "Theta", 4)]}
                       xLabel="spot" yFmt={(v) => v.toFixed(3)} />
          </div>
        </div>
      )}
    </Box>
  );
}

/* ---------------------------------------------------------- convergence */
function ConvergencePanel({ colors }) {
  const [state, setState] = useState({ loading: true });

  useEffect(() => {
    (async () => {
      const q = { spot: 100, strike: 100, rate: 0.05, vol: 0.2, maturity: 1.0, is_call: true };
      try {
        const bs = await postJSON("/api/pricing/black-scholes", q);
        const stepList = [8, 16, 32, 64, 128, 256, 512, 1024];
        const pathList = [1000, 5000, 20000, 50000, 200000, 500000];
        const [bin, mc] = await Promise.all([
          Promise.all(stepList.map((steps) => postJSON("/api/pricing/binomial", { ...q, steps })
            .then((r) => ({ steps: r.meta?.steps ?? steps, price: r.price })))),
          Promise.all(pathList.map((paths) => postJSON("/api/pricing/monte-carlo", { ...q, paths })
            .then((r) => ({ paths: r.meta?.paths ?? paths, price: r.price, stderr: r.stderr })))),
        ]);
        setState({ loading: false, bs, bin, mc });
      } catch (e) { setState({ loading: false, error: e.message }); }
    })();
  }, []);

  const { loading, bs, bin, mc, error } = state;

  return (
    <Box title="Convergence — binomial and Monte Carlo against the analytic price"
         sub="The same contract priced three ways. Binomial converges by refining the lattice and Monte Carlo by sampling, and watching both approach the closed form is the cross-check that says the engines agree. A method that converged to a different number would be the finding."
         right={bs && <Pill tone={T.teal}>BS {bs.price.toFixed(4)}</Pill>}>
      {loading && <div style={{ fontSize: 11.5, color: T.faint }}>Pricing across lattice sizes and path counts…</div>}
      {error && <Err msg={error} />}
      {!loading && !error && bs && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 16 }}>
          <div>
            <div style={{ ...lbl, marginBottom: 6 }}>Binomial — error vs lattice steps</div>
            <LineChart
              series={[{ name: "Binomial − BS", color: colors[0],
                         points: bin.map((b) => [b.steps, (b.price - bs.price) * 1e4] ) },
                       { name: "Analytic", color: colors[3], dashed: true,
                         points: bin.map((b) => [b.steps, 0]) }]}
              xLabel="lattice steps" yFmt={(v) => `${v.toFixed(1)}bp`} />
          </div>
          <div>
            <div style={{ ...lbl, marginBottom: 6 }}>Monte Carlo — error vs paths</div>
            <LineChart
              series={[{ name: "MC − BS", color: colors[1],
                         points: mc.map((m) => [m.paths, (m.price - bs.price) * 1e4]) },
                       { name: "±1 s.e.", color: colors[2], dashed: true,
                         points: mc.map((m) => [m.paths, (m.stderr || 0) * 1e4]) },
                       { name: "Analytic", color: colors[3], dashed: true,
                         points: mc.map((m) => [m.paths, 0]) }]}
              xLabel="paths" yFmt={(v) => `${v.toFixed(1)}bp`} />
          </div>
          <div style={{ gridColumn: "1 / -1", fontSize: 11, color: T.dim, lineHeight: 1.65, borderTop: `1px solid ${T.lineSoft}`, paddingTop: 10 }}>
            Error is in basis points of the analytic price, so zero is agreement and
            the sign says which way a method leans. Monte Carlo error should fall
            roughly as 1/√paths — a run that sits outside its own standard error band
            is not converging, it is biased, and that is a different problem from
            being imprecise.
          </div>
        </div>
      )}
    </Box>
  );
}

export default function RiskLab() {
  const colors = useSeriesColors();
  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 5 }}>Risk &amp; Pricing Lab</div>
        <div style={{ fontSize: 11.5, color: T.dim, lineHeight: 1.65, maxWidth: 780 }}>
          VaR and Expected Shortfall computed over a real index return history, and
          the Black-Scholes, binomial and Monte Carlo engines plotted against each
          other. Every figure here is the result of a call to the engine or the
          market-data service; nothing is illustrative.
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
          <Pill tone={T.teal}>c++ engines</Pill>
          <Pill tone={T.dim}>real FRED return history</Pill>
          <Pill tone={T.amber}>end-of-day, not intraday</Pill>
        </div>
      </div>
      <VarPanel colors={colors} />
      <GreeksPanel colors={colors} />
      <ConvergencePanel colors={colors} />
    </div>
  );
}
