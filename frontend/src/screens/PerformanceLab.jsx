/* PerformanceLab.jsx — measured C++ performance, never asserted.
 *
 * Every figure on this screen comes from /api/connectivity/performance, which
 * serves the JSON that ./benchmarks/run.sh wrote. Nothing here is a constant
 * typed into a component, and the screen is built so that it CANNOT show a
 * number the harness did not produce:
 *
 *   * If no results exist the API 404s and this renders an instruction to run
 *     the harness, not a placeholder table.
 *   * The 8-core row shows "NOT MEASURED" when the host had fewer cores. It is
 *     not extrapolated, and the reason is displayed rather than hidden.
 *   * Rows that oversubscribe the physical core count are tagged SMT, because
 *     "4 threads: 2.6x" read without that invites inferring 8 threads gives 5x.
 *   * Each figure shows its run-to-run spread. A measurement with a 79% spread
 *     is shown greyed with a do-not-quote marker rather than dropped -- quietly
 *     removing the inconvenient row is how benchmark tables become dishonest.
 */
import React, { useEffect, useState } from "react";
import { T, MONO } from "../theme.js";

const fmtM = (n) => (n == null ? "—" : `${(n / 1e6).toFixed(1)}M`);
const pct = (n) => (n == null ? "—" : `${n.toFixed(1)}%`);

/* Spread is the honesty signal on this hardware, so it drives the colour. */
function spreadTone(s) {
  if (s == null) return T.dim;
  if (s > 50) return T.red;
  if (s > 20) return T.amber;
  return T.dim;
}

function Cell({ children, mono = true, align = "right", color, dim, title }) {
  return (
    <td title={title} style={{
      padding: "7px 10px", textAlign: align, color: color || (dim ? T.dim : T.text),
      fontFamily: mono ? MONO : "inherit", fontSize: 12.5,
      borderBottom: `1px solid ${T.lineSoft}`, whiteSpace: "nowrap",
    }}>{children}</td>
  );
}

function Th({ children, align = "right", title }) {
  return (
    <th title={title} style={{
      padding: "7px 10px", textAlign: align, color: T.faint, fontSize: 10,
      letterSpacing: 0.9, textTransform: "uppercase", fontWeight: 600,
      borderBottom: `1px solid ${T.line}`, whiteSpace: "nowrap",
    }}>{children}</th>
  );
}

function Box({ title, sub, children, tone }) {
  return (
    <section style={{
      background: T.panel, border: `1px solid ${tone || T.line}`,
      borderRadius: 9, overflow: "hidden", marginBottom: 14,
    }}>
      <header style={{ padding: "11px 14px", borderBottom: `1px solid ${T.lineSoft}` }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>{title}</div>
        {sub && <div style={{ fontSize: 11, color: T.dim, marginTop: 3, lineHeight: 1.5 }}>{sub}</div>}
      </header>
      <div style={{ padding: 0 }}>{children}</div>
    </section>
  );
}

function Pill({ children, tone = T.dim }) {
  return (
    <span style={{
      display: "inline-block", padding: "1.5px 7px", borderRadius: 20, fontSize: 9.5,
      letterSpacing: 0.6, color: tone, border: `1px solid ${tone}`,
      fontFamily: MONO, textTransform: "uppercase", marginLeft: 6,
    }}>{children}</span>
  );
}

export default function PerformanceLab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    fetch("/api/connectivity/performance")
      .then(async (r) => {
        if (r.status === 404) throw new Error((await r.json()).detail);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setErr(e.message));
  }, []);

  if (err) {
    return (
      <Box title="Performance Lab" tone={T.amber}
           sub="No measured results are available, and this screen will not invent any.">
        <div style={{ padding: 16 }}>
          <div style={{ color: T.amber, fontSize: 12.5, marginBottom: 10 }}>{err}</div>
          <code style={{
            display: "block", background: T.panel2, border: `1px solid ${T.line}`,
            borderRadius: 6, padding: "9px 12px", fontFamily: MONO, fontSize: 12, color: T.teal,
          }}>./benchmarks/run.sh</code>
          <p style={{ color: T.dim, fontSize: 11.5, marginTop: 10, lineHeight: 1.6, maxWidth: 620 }}>
            Run it on an idle machine. Under concurrent load this harness has produced
            a 4-thread figure below its own 2-thread figure, which is what contention
            looks like rather than a property of the code.
          </p>
        </div>
      </Box>
    );
  }

  if (!data) return <div style={{ color: T.dim, padding: 16, fontSize: 12.5 }}>Loading measured results…</div>;

  const base = data.runs?.baseline;
  const avx = data.runs?.avx2;
  const env = (base || avx)?.environment || {};
  const mc = (base || avx)?.monte_carlo || {};
  const conn = (base || avx)?.connectivity || {};
  const eightMeasured = mc.eight_physical_core_measured;

  return (
    <div>
      {/* ---------------------------------------------------------- environment */}
      <Box
        title="Measurement environment"
        sub="A throughput figure is a fact about code on a CPU with a compiler at an optimisation level. Quoting it without these makes it unfalsifiable, so they are recorded with every run."
      >
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))",
          gap: 1, background: T.lineSoft,
        }}>
          {[
            ["CPU", env.cpu],
            ["Cores", `${env.physical_cores} physical / ${env.logical_cores} logical`],
            ["Compiler", env.compiler],
            ["Flags", env.flags],
            ["SIMD compiled", env.simd_compiled],
            ["SIMD on CPU", env.simd_available_on_cpu],
            ["Paths", mc.paths?.toLocaleString()],
            ["Repetitions", mc.repetitions],
          ].map(([k, v]) => (
            <div key={k} style={{ background: T.panel, padding: "9px 13px" }}>
              <div style={{ color: T.faint, fontSize: 9.5, letterSpacing: 0.9, textTransform: "uppercase" }}>{k}</div>
              <div style={{ fontFamily: MONO, fontSize: 12, marginTop: 3, color: T.text, wordBreak: "break-word" }}>
                {v ?? "—"}
              </div>
            </div>
          ))}
        </div>
        {env.simd_compiled !== env.simd_available_on_cpu && (
          <div style={{
            padding: "9px 14px", borderTop: `1px solid ${T.lineSoft}`,
            color: T.amber, fontSize: 11.5, lineHeight: 1.55,
          }}>
            This CPU supports <b>{env.simd_available_on_cpu}</b> but the default build targets{" "}
            <b>{env.simd_compiled}</b>. The AVX2 column below is the measured cost of that gap.
          </div>
        )}
      </Box>

      {/* ------------------------------------------------------- the 8-core note */}
      <Box
        title="8-core objective"
        tone={eightMeasured ? T.teal : T.amber}
        sub={eightMeasured
          ? "Measured on this host."
          : "Not measured, and deliberately not extrapolated."}
      >
        <div style={{ padding: "12px 14px" }}>
          {eightMeasured ? (
            <div style={{ color: T.teal, fontSize: 12.5 }}>
              8 physical cores present — the scaling table below includes a measured 8-core row.
            </div>
          ) : (
            <>
              <div style={{ fontFamily: MONO, fontSize: 13, color: T.amber, marginBottom: 7 }}>
                NOT MEASURED
              </div>
              <p style={{ color: T.dim, fontSize: 11.5, lineHeight: 1.6, margin: 0, maxWidth: 680 }}>
                {mc.eight_physical_core_note}
              </p>
              <p style={{ color: T.faint, fontSize: 11, lineHeight: 1.6, marginTop: 8, marginBottom: 0, maxWidth: 680 }}>
                Multiplying a measured per-core figure by eight would produce something
                indistinguishable from a measurement. The harness fills this in
                automatically when run on suitable hardware.
              </p>
            </>
          )}
        </div>
      </Box>

      {/* ------------------------------------------------------------- MC scaling */}
      <Box
        title="Monte Carlo scaling"
        sub={<>
          {mc.model} · <b>{mc.timesteps_per_path} timestep per path</b> · {mc.variance_reduction} ·{" "}
          {mc.bitwise_identical_across_thread_counts
            ? <span style={{ color: T.teal }}>bitwise-identical price at every thread count</span>
            : <span style={{ color: T.red }}>DETERMINISM CHECK FAILED</span>}
          {data.price_identical_across_builds &&
            <span style={{ color: T.teal }}> · and identical between the scalar and AVX2 builds</span>}
        </>}
      >
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 720 }}>
            <thead>
              <tr>
                <Th align="left">Threads</Th>
                <Th title="Median of all repetitions">Baseline</Th>
                {avx && <Th title="Same source, built with -mavx2 -mfma">AVX2+FMA</Th>}
                {avx && <Th>Gain</Th>}
                <Th title="Fastest and slowest repetition">Best / worst</Th>
                <Th title="(best - worst) / median">Spread</Th>
                <Th>Speedup</Th>
                <Th title="speedup / threads">Efficiency</Th>
                <Th align="left"> </Th>
              </tr>
            </thead>
            <tbody>
              {(mc.scaling || []).map((row, i) => {
                const a = avx?.monte_carlo?.scaling?.[i];
                const gain = a ? a.paths_per_sec / row.paths_per_sec : null;
                return (
                  <tr key={row.threads}>
                    <Cell align="left">{row.threads}</Cell>
                    <Cell>{fmtM(row.paths_per_sec)}/s</Cell>
                    {avx && <Cell color={T.teal}>{fmtM(a?.paths_per_sec)}/s</Cell>}
                    {avx && <Cell color={T.teal}>{gain ? `${gain.toFixed(2)}×` : "—"}</Cell>}
                    <Cell dim>{fmtM(row.best_paths_per_sec)} / {fmtM(row.worst_paths_per_sec)}</Cell>
                    <Cell color={spreadTone(row.spread_pct)}>{pct(row.spread_pct)}</Cell>
                    <Cell>{row.speedup?.toFixed(2)}×</Cell>
                    <Cell color={row.efficiency >= 0.9 ? T.teal : row.efficiency >= 0.7 ? T.text : T.amber}>
                      {row.efficiency?.toFixed(2)}
                    </Cell>
                    <Cell align="left" mono={false}>
                      {row.oversubscribed_smt
                        ? <Pill tone={T.amber}>SMT</Pill>
                        : <Pill tone={T.teal}>real cores</Pill>}
                    </Cell>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div style={{ padding: "10px 14px", borderTop: `1px solid ${T.lineSoft}`, color: T.dim, fontSize: 11, lineHeight: 1.6 }}>
          <b style={{ color: T.amber }}>SMT rows are not per-core scaling.</b> {mc.smt_note}
          <br />
          <b style={{ color: T.text }}>Timesteps matter.</b> {mc.timestep_note}
        </div>
      </Box>

      {/* -------------------------------------------------------- connectivity */}
      <Box
        title="Connectivity core throughput"
        sub="Single thread, in-process. Excludes network, kernel and venue time — which dominate real wire-to-wire latency."
      >
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 560 }}>
            <thead>
              <tr>
                <Th align="left">Operation</Th>
                <Th>Median</Th>
                <Th>Best / worst</Th>
                <Th>Spread</Th>
                <Th align="left"> </Th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(conn)
                .filter(([, v]) => v && typeof v === "object" && "median_per_sec" in v)
                .sort((a, b) => b[1].median_per_sec - a[1].median_per_sec)
                .map(([k, v]) => {
                  const unreliable = v.spread_pct > 50;
                  return (
                    <tr key={k} style={unreliable ? { opacity: 0.55 } : undefined}>
                      <Cell align="left" mono={false}>{k.replace(/_/g, " ")}</Cell>
                      <Cell>{fmtM(v.median_per_sec)}/s</Cell>
                      <Cell dim>{fmtM(v.best_per_sec)} / {fmtM(v.worst_per_sec)}</Cell>
                      <Cell color={spreadTone(v.spread_pct)}>{pct(v.spread_pct)}</Cell>
                      <Cell align="left" mono={false}>
                        {unreliable && <Pill tone={T.red}>do not quote</Pill>}
                      </Cell>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
        {conn.end_to_end_note && (
          <div style={{ padding: "10px 14px", borderTop: `1px solid ${T.lineSoft}`, color: T.dim, fontSize: 11, lineHeight: 1.6 }}>
            <b style={{ color: T.text }}>End-to-end order path.</b> {conn.end_to_end_note}
          </div>
        )}
        <div style={{ padding: "10px 14px", borderTop: `1px solid ${T.lineSoft}`, color: T.faint, fontSize: 11, lineHeight: 1.6 }}>
          Rows greyed with <span style={{ color: T.red }}>do not quote</span> have a
          run-to-run spread above 50%: the measurement is dominated by scheduling noise
          rather than the code under test. They are shown rather than deleted, because
          quietly dropping the inconvenient row is how a benchmark table becomes dishonest.
        </div>
      </Box>

      <div style={{ color: T.faint, fontSize: 11, lineHeight: 1.6, padding: "0 2px 8px" }}>
        <b style={{ color: T.dim }}>Methodology.</b> {(base || avx)?.methodology}
      </div>
    </div>
  );
}
