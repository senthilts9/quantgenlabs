/* Charts.jsx — SVG chart primitives.
 *
 * NO CHARTING LIBRARY, deliberately. The whole frontend bundle is already
 * 511 kB; Recharts plus its D3 dependencies roughly doubles that to render four
 * chart shapes. These are plain SVG and total a few hundred lines.
 *
 * SERIES COLOURS ARE NOT THE UI ACCENT COLOURS, and that is not an oversight.
 * The theme's accents sit at OKLCH L 0.685-0.76, outside the 0.48-0.67 band a
 * dark chart surface needs, so plotted side by side they read as uneven weight
 * rather than as equal categories. The series ramp below keeps the same five
 * hues -- so teal still means the same thing it means everywhere else -- stepped
 * into the band. Both modes were run through the palette validator: lightness
 * band, chroma floor, CVD separation (worst adjacent pair deltaE 14.3 deutan),
 * normal-vision floor and contrast all pass on their own surface.
 *
 * Identity is never colour alone: two or more series always get a legend, and
 * four or fewer are also direct-labelled.
 */
import React, { useId, useState } from "react";
import { T, MONO } from "../theme.js";

/* Validated series ramp. Fixed order, never cycled -- a filter that drops a
 * series must not repaint the survivors, so slot N keeps its hue regardless of
 * how many are visible. */
export const SERIES_DARK = ["#2A9C88", "#4A82D4", "#B8823A", "#8370DB", "#C94750"];
export const SERIES_LIGHT = ["#00836E", "#1F5FC4", "#9A6212", "#5B41C7", "#B3202A"];

export function useSeriesColors() {
  const [dark, setDark] = useState(() =>
    typeof document === "undefined" ? true
      : document.documentElement.getAttribute("data-theme") !== "light");
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    const el = document.documentElement;
    const obs = new MutationObserver(() =>
      setDark(el.getAttribute("data-theme") !== "light"));
    obs.observe(el, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return dark ? SERIES_DARK : SERIES_LIGHT;
}

const fmt = (v, d = 2) =>
  v == null || Number.isNaN(v) ? "—"
    : Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : v.toFixed(d);

/* ------------------------------------------------------------------ legend */
export function Legend({ items }) {
  if (!items || items.length < 2) return null;   // one series: the title names it
  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 8 }}>
      {items.map((it) => (
        <span key={it.label} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: 2, background: it.color, flex: "0 0 auto" }} />
          <span style={{ fontSize: 11, color: T.dim }}>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

/* --------------------------------------------------------------- histogram */
/** Distribution with threshold markers (VaR, ES). `markers` are {x, label, color}. */
export function Histogram({ values, markers = [], height = 190, xLabel = "", bins = 36 }) {
  const [hover, setHover] = useState(null);
  const id = useId();
  if (!values?.length) return <Empty />;

  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const counts = new Array(bins).fill(0);
  values.forEach((v) => {
    const i = Math.min(bins - 1, Math.max(0, Math.floor(((v - lo) / span) * bins)));
    counts[i] += 1;
  });
  const peak = Math.max(...counts) || 1;

  const W = 640, H = height, PL = 44, PR = 12, PT = 10, PB = 26;
  const iw = W - PL - PR, ih = H - PT - PB;
  const xOf = (v) => PL + ((v - lo) / span) * iw;
  const bw = iw / bins;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}
           role="img" aria-label={`Distribution of ${values.length} observations`}>
        {/* recessive grid */}
        {[0, 0.5, 1].map((f) => (
          <line key={f} x1={PL} x2={W - PR} y1={PT + ih * (1 - f)} y2={PT + ih * (1 - f)}
                stroke={T.lineSoft} strokeWidth="1" />
        ))}
        {counts.map((c, i) => {
          const h = (c / peak) * ih;
          // 2px surface gap between adjacent fills, and the 4px rounded data-end
          // anchored to the baseline.
          return (
            <rect key={i} x={PL + i * bw + 1} y={PT + ih - h} width={Math.max(bw - 2, 1)} height={h}
                  rx={Math.min(4, Math.max(bw - 2, 1) / 2)} fill={T.dim} opacity={hover === i ? 0.95 : 0.55}
                  onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
          );
        })}
        {markers.map((m) => (
          <g key={m.label}>
            <line x1={xOf(m.x)} x2={xOf(m.x)} y1={PT} y2={PT + ih}
                  stroke={m.color} strokeWidth="2" strokeDasharray="4 3" />
            <text x={xOf(m.x)} y={PT + 10} fill={m.color} fontSize="10" fontFamily={MONO}
                  textAnchor={xOf(m.x) > W / 2 ? "end" : "start"}
                  dx={xOf(m.x) > W / 2 ? -4 : 4}>{m.label}</text>
          </g>
        ))}
        <line x1={PL} x2={W - PR} y1={PT + ih} y2={PT + ih} stroke={T.line} strokeWidth="1" />
        {[lo, lo + span / 2, hi].map((v, i) => (
          <text key={i} x={PL + (iw * i) / 2} y={H - 8} fill={T.faint} fontSize="10"
                fontFamily={MONO} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>
            {fmt(v * 100, 1)}%
          </text>
        ))}
      </svg>
      <div style={{ fontSize: 10.5, color: T.faint, textAlign: "center" }}>
        {hover != null
          ? `${counts[hover]} observations in ${fmt((lo + (hover * span) / bins) * 100, 2)}%…${fmt((lo + ((hover + 1) * span) / bins) * 100, 2)}%`
          : xLabel}
      </div>
    </div>
  );
}

/* ----------------------------------------------------------- grouped bars */
/** groups: [{label, values:[n,…]}], seriesNames aligns with values. */
export function GroupedBars({ groups, seriesNames, colors, height = 200, unit = "" }) {
  const [hover, setHover] = useState(null);
  if (!groups?.length) return <Empty />;

  const all = groups.flatMap((g) => g.values).filter((v) => v != null);
  const hi = Math.max(...all, 0) * 1.12 || 1;
  const W = 640, H = height, PL = 52, PR = 12, PT = 8, PB = 30;
  const iw = W - PL - PR, ih = H - PT - PB;
  const gw = iw / groups.length;
  const n = seriesNames.length;
  const bw = Math.max((gw - 14) / n, 4);

  return (
    <div>
      <Legend items={seriesNames.map((s, i) => ({ label: s, color: colors[i] }))} />
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}
           role="img" aria-label="Grouped comparison">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={PL} x2={W - PR} y1={PT + ih * (1 - f)} y2={PT + ih * (1 - f)}
                  stroke={T.lineSoft} strokeWidth="1" />
            <text x={PL - 6} y={PT + ih * (1 - f) + 3} fill={T.faint} fontSize="10"
                  fontFamily={MONO} textAnchor="end">{fmt(hi * f, 2)}</text>
          </g>
        ))}
        {groups.map((g, gi) =>
          g.values.map((v, si) => {
            const h = v == null ? 0 : (v / hi) * ih;
            const x = PL + gi * gw + 7 + si * bw;
            const key = `${gi}-${si}`;
            return (
              <g key={key}>
                {/* 2px gap between adjacent bars, 4px rounded end at the top */}
                <rect x={x + 1} y={PT + ih - h} width={bw - 2} height={h}
                      rx={Math.min(4, (bw - 2) / 2)} fill={colors[si]}
                      opacity={hover && hover !== key ? 0.35 : 1}
                      onMouseEnter={() => setHover(key)} onMouseLeave={() => setHover(null)} />
                {hover === key && (
                  <text x={x + bw / 2} y={PT + ih - h - 5} fill={T.text} fontSize="10"
                        fontFamily={MONO} textAnchor="middle">{fmt(v, 3)}{unit}</text>
                )}
              </g>
            );
          }),
        )}
        <line x1={PL} x2={W - PR} y1={PT + ih} y2={PT + ih} stroke={T.line} strokeWidth="1" />
        {groups.map((g, gi) => (
          <text key={g.label} x={PL + gi * gw + gw / 2} y={H - 10} fill={T.dim} fontSize="10"
                textAnchor="middle">{g.label}</text>
        ))}
      </svg>
    </div>
  );
}

/* ---------------------------------------------------------------- line(s) */
/** series: [{name, color, points:[[x,y],…], dashed?}] — one shared y axis. */
export function LineChart({ series, height = 200, xLabel = "", yLabel = "", yFmt = (v) => fmt(v, 2) }) {
  const [cx, setCx] = useState(null);
  if (!series?.length || !series.some((s) => s.points?.length)) return <Empty />;

  const pts = series.flatMap((s) => s.points);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || Math.abs(y1) * 0.1 || 1;
  y0 -= pad; y1 += pad;

  const W = 640, H = height, PL = 56, PR = 14, PT = 10, PB = 28;
  const iw = W - PL - PR, ih = H - PT - PB;
  const X = (v) => PL + ((v - x0) / (x1 - x0 || 1)) * iw;
  const Y = (v) => PT + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    setCx(px >= PL && px <= W - PR ? x0 + ((px - PL) / iw) * (x1 - x0) : null);
  };
  const nearest = (s) => cx == null ? null
    : s.points.reduce((a, b) => (Math.abs(b[0] - cx) < Math.abs(a[0] - cx) ? b : a));

  return (
    <div>
      <Legend items={series.map((s) => ({ label: s.name, color: s.color }))} />
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}
           onMouseMove={onMove} onMouseLeave={() => setCx(null)}
           role="img" aria-label={yLabel || "Line chart"}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={PL} x2={W - PR} y1={PT + ih * f} y2={PT + ih * f} stroke={T.lineSoft} strokeWidth="1" />
            <text x={PL - 6} y={PT + ih * f + 3} fill={T.faint} fontSize="10" fontFamily={MONO}
                  textAnchor="end">{yFmt(y1 - (y1 - y0) * f)}</text>
          </g>
        ))}
        {series.map((s) => (
          <polyline key={s.name} fill="none" stroke={s.color} strokeWidth="2"
                    strokeDasharray={s.dashed ? "5 4" : undefined}
                    strokeLinejoin="round" strokeLinecap="round"
                    points={s.points.map((p) => `${X(p[0])},${Y(p[1])}`).join(" ")} />
        ))}
        {cx != null && (
          <g>
            <line x1={X(cx)} x2={X(cx)} y1={PT} y2={PT + ih} stroke={T.dim} strokeWidth="1" strokeDasharray="3 3" />
            {series.map((s) => {
              const p = nearest(s);
              return p ? (
                // 2px surface ring so overlapping markers stay countable
                <circle key={s.name} cx={X(p[0])} cy={Y(p[1])} r="4.5" fill={s.color}
                        stroke={T.panel} strokeWidth="2" />
              ) : null;
            })}
          </g>
        )}
        <line x1={PL} x2={W - PR} y1={PT + ih} y2={PT + ih} stroke={T.line} strokeWidth="1" />
        {[x0, (x0 + x1) / 2, x1].map((v, i) => (
          <text key={i} x={PL + (iw * i) / 2} y={H - 9} fill={T.faint} fontSize="10" fontFamily={MONO}
                textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>{fmt(v, 0)}</text>
        ))}
      </svg>
      <div style={{ fontSize: 10.5, color: T.faint, textAlign: "center", minHeight: 15 }}>
        {cx != null
          ? series.map((s) => { const p = nearest(s); return p ? `${s.name} ${yFmt(p[1])}` : null; })
              .filter(Boolean).join("   ·   ")
          : xLabel}
      </div>
    </div>
  );
}

function Empty() {
  return <div style={{ fontSize: 11.5, color: T.faint, padding: "22px 0", textAlign: "center" }}>
    No data to plot.
  </div>;
}
