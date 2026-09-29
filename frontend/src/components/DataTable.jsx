/* DataTable.jsx — the table primitive the console was missing.
 *
 * Every blotter in the app was a plain <table> rendering every row. That is fine
 * at twenty rows and unusable at twenty thousand: the browser lays out every cell
 * whether or not it is on screen, and a streaming blotter turns into a frozen tab.
 *
 * VIRTUALISATION, hand-rolled rather than pulled in. A windowing library is the
 * usual answer, but this needs one thing — render the slice of rows that overlaps
 * the viewport, pad above and below with spacer rows so the scrollbar stays
 * honest — and that is ~15 lines. The dependency would cost more to audit than
 * the code costs to own.
 *
 * SORTING IS STABLE and type-aware. A blotter sorted by quantity must order
 * numerically (100 before 20), and JavaScript's default sort is lexicographic, so
 * "100" < "20" is the bug you get for free. Ties preserve arrival order, which is
 * what makes a sorted execution list still readable as a sequence.
 *
 * EXPORT WRITES REAL CSV. Values are quoted and internal quotes doubled, because
 * an instrument name with a comma in it otherwise shifts every subsequent column
 * — silently, in a file someone reconciles against.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { T, MONO } from "../theme.js";

const ROW_H = 28;
const OVERSCAN = 8;      // rows rendered beyond the viewport, so a fast scroll
                         // does not expose blank space before React catches up

function compare(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;                    // nulls sort last, either direction
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  const na = Number(a), nb = Number(b);
  // Numeric strings compare numerically: lexicographic order puts "100" before
  // "20", which is wrong in every column a trader looks at.
  if (!Number.isNaN(na) && !Number.isNaN(nb) && a !== "" && b !== "") return na - nb;
  return String(a).localeCompare(String(b));
}

function toCsv(columns, rows) {
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return `"${s.replace(/"/g, '""')}"`;       // always quote, double internal quotes
  };
  const head = columns.map((c) => esc(c.header ?? c.key)).join(",");
  const body = rows.map((r) => columns.map((c) => esc(c.raw ? c.raw(r) : r[c.key])).join(","));
  return [head, ...body].join("\r\n");         // CRLF: what spreadsheets expect
}

export default function DataTable({
  columns,
  rows,
  height = 340,
  rowKey = (r, i) => r.id ?? i,
  onRowClick,
  selectedKey,
  filterable = true,
  exportable = true,
  exportName = "export",
  empty = "No rows.",
  dense = false,
}) {
  const [sort, setSort] = useState({ key: null, dir: 1 });
  const [query, setQuery] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const boxRef = useRef(null);
  const rowH = dense ? 24 : ROW_H;

  const filtered = useMemo(() => {
    if (!query.trim()) return rows;
    const q = query.toLowerCase();
    return rows.filter((r) =>
      columns.some((c) => {
        const v = c.raw ? c.raw(r) : r[c.key];
        return v != null && String(v).toLowerCase().includes(q);
      }));
  }, [rows, columns, query]);

  const sorted = useMemo(() => {
    if (!sort.key) return filtered;
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return filtered;
    // Decorate with the original index so ties keep arrival order — a stable
    // sort is what lets a sorted execution list still read as a sequence.
    return filtered
      .map((r, i) => [r, i])
      .sort(([a, ai], [b, bi]) => {
        const va = col.raw ? col.raw(a) : a[col.key];
        const vb = col.raw ? col.raw(b) : b[col.key];
        const c = compare(va, vb);
        return c !== 0 ? c * sort.dir : ai - bi;
      })
      .map(([r]) => r);
  }, [filtered, columns, sort]);

  // Scrolling past the end of a shrinking list leaves a blank viewport.
  useEffect(() => {
    const max = Math.max(0, sorted.length * rowH - height);
    if (scrollTop > max && boxRef.current) boxRef.current.scrollTop = max;
  }, [sorted.length, rowH, height, scrollTop]);

  const total = sorted.length;
  const first = Math.max(0, Math.floor(scrollTop / rowH) - OVERSCAN);
  const visible = Math.ceil(height / rowH) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);
  const slice = sorted.slice(first, last);

  const toggleSort = (key) =>
    setSort((s) => (s.key === key ? { key, dir: -s.dir } : { key, dir: 1 }));

  const download = () => {
    const csv = toCsv(columns, sorted);
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${exportName}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const th = {
    padding: dense ? "4px 8px" : "6px 9px", color: T.faint, fontSize: 9.5,
    letterSpacing: 0.8, textTransform: "uppercase", fontWeight: 600,
    borderBottom: `1px solid ${T.line}`, whiteSpace: "nowrap",
    position: "sticky", top: 0, background: T.panel, zIndex: 1, cursor: "pointer",
    userSelect: "none",
  };

  return (
    <div>
      {(filterable || exportable) && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
          {filterable && (
            <input
              value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter…" aria-label="Filter rows"
              style={{ background: T.panel2, border: `1px solid ${T.line}`, color: T.text,
                       borderRadius: 6, padding: "5px 9px", fontSize: 12, width: 190 }}
            />
          )}
          <span style={{ color: T.faint, fontSize: 11, fontFamily: MONO }}>
            {total === rows.length ? `${total} rows` : `${total} of ${rows.length}`}
          </span>
          {sort.key && (
            <button onClick={() => setSort({ key: null, dir: 1 })}
              style={{ background: "none", border: `1px solid ${T.line}`, color: T.dim,
                       borderRadius: 5, padding: "2px 8px", fontSize: 10.5, cursor: "pointer" }}>
              clear sort
            </button>
          )}
          <span style={{ flex: 1 }} />
          {exportable && (
            <button onClick={download} disabled={!total}
              style={{ background: T.panel2, border: `1px solid ${T.line}`, color: T.dim,
                       borderRadius: 6, padding: "4px 11px", fontSize: 11,
                       cursor: total ? "pointer" : "not-allowed" }}>
              Export CSV
            </button>
          )}
        </div>
      )}

      <div
        ref={boxRef}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        style={{ height, overflow: "auto", border: `1px solid ${T.lineSoft}`, borderRadius: 7 }}
      >
        <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
          <colgroup>
            {columns.map((c) => <col key={c.key} style={{ width: c.width }} />)}
          </colgroup>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} style={{ ...th, textAlign: c.align || "left" }}
                    onClick={() => toggleSort(c.key)}
                    aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                    title={`Sort by ${c.header ?? c.key}`}>
                  {c.header ?? c.key}
                  {sort.key === c.key && (
                    <span style={{ color: T.teal, marginLeft: 4 }}>{sort.dir === 1 ? "▲" : "▼"}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {total === 0 && (
              <tr><td colSpan={columns.length}
                      style={{ padding: 18, color: T.faint, fontSize: 12, textAlign: "center" }}>
                {query ? `Nothing matches “${query}”.` : empty}
              </td></tr>
            )}
            {/* Spacers keep the scrollbar proportional to the FULL list while only
                the visible slice is laid out. */}
            {first > 0 && <tr style={{ height: first * rowH }}><td colSpan={columns.length} /></tr>}
            {slice.map((r, i) => {
              const key = rowKey(r, first + i);
              const on = selectedKey != null && key === selectedKey;
              return (
                <tr key={key}
                    onClick={onRowClick ? () => onRowClick(r) : undefined}
                    style={{ height: rowH, cursor: onRowClick ? "pointer" : "default",
                             background: on ? T.panel2 : "transparent" }}>
                  {columns.map((c) => (
                    <td key={c.key}
                        style={{ padding: dense ? "2px 8px" : "4px 9px",
                                 textAlign: c.align || "left",
                                 color: c.tone ? c.tone(r) : T.text,
                                 fontFamily: c.mono === false ? "inherit" : MONO,
                                 fontSize: dense ? 11 : 11.5,
                                 borderBottom: `1px solid ${T.lineSoft}`,
                                 whiteSpace: "nowrap", overflow: "hidden",
                                 textOverflow: "ellipsis" }}>
                      {c.render ? c.render(r) : (r[c.key] ?? "")}
                    </td>
                  ))}
                </tr>
              );
            })}
            {last < total && (
              <tr style={{ height: (total - last) * rowH }}><td colSpan={columns.length} /></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export { toCsv, compare };
