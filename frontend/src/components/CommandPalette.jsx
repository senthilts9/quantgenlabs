/* CommandPalette.jsx — ⌘K / Ctrl-K navigation.
 *
 * This replaces a dead input. The top bar previously rendered
 * `<input placeholder="Search instruments, models, commands…" />` with a ⌘K chip
 * beside it, and neither did anything: no state, no handler, no key listener. A
 * control that advertises a shortcut it does not implement is worse than no
 * control, because it teaches people the UI lies to them.
 *
 * Matching is subsequence-based rather than substring, which is what makes a
 * palette feel fast: "pfl" finds "Performance Lab", "tdk" finds "Trading Desk".
 * Ties break toward earlier, tighter matches, so the obvious candidate is
 * selected by the time you have typed two characters.
 *
 * Keyboard is the whole point, so it is complete: ⌘K/Ctrl-K toggles, ↑/↓ or
 * Ctrl-P/Ctrl-N move, Enter selects, Escape closes, and focus returns to
 * whatever had it before. It is a real dialog (role, aria-modal, labelled) so it
 * is announced properly rather than being a div that happens to float.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { T, MONO } from "../theme.js";

/* Subsequence match. Returns null when `q` does not fit in `text`, otherwise a
 * score where LOWER is better: earlier first hit and tighter packing win. */
export function fuzzyScore(text, q) {
  if (!q) return 0;
  const t = text.toLowerCase();
  const needle = q.toLowerCase().replace(/\s+/g, "");
  let ti = 0, first = -1, gaps = 0, last = -1;
  for (const ch of needle) {
    const at = t.indexOf(ch, ti);
    if (at === -1) return null;
    if (first === -1) first = at;
    if (last !== -1) gaps += at - last - 1;
    last = at;
    ti = at + 1;
  }
  // A word-boundary start outranks raw position: someone typing "co" almost
  // always means a word beginning "co", not the "co" inside "Reconciliation".
  //
  // The weight is 6 rather than 3 because 3 only ties against a three-character
  // position difference -- "Risk Copilot" (hit at 5, on a boundary) and
  // "Reconciliation" (hit at 2, mid-word) both scored 5, so the tie broke
  // alphabetically and the wrong one won. A unit test caught that.
  const boundary = first === 0 || /[\s&/·-]/.test(t[first - 1] || "") ? 0 : 6;
  return first + gaps * 2 + boundary;
}

export default function CommandPalette({ open, onClose, items, onSelect, current }) {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const restoreRef = useRef(null);

  const results = useMemo(() => {
    const scored = items
      .map((it) => {
        const hay = `${it.label} ${it.group || ""} ${it.id}`;
        return { it, score: fuzzyScore(hay, q) };
      })
      .filter((r) => r.score !== null);
    scored.sort((a, b) => a.score - b.score || a.it.label.localeCompare(b.it.label));
    return scored.map((r) => r.it);
  }, [items, q]);

  // Reset on open, and remember what to hand focus back to.
  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement;
    setQ("");
    setCursor(0);
    const id = requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      cancelAnimationFrame(id);
      // Only restore if focus is still inside the palette; otherwise the user
      // has clicked elsewhere and stealing it back would be hostile.
      if (restoreRef.current && document.activeElement?.closest?.("[data-cmdk]") !== null) {
        restoreRef.current.focus?.();
      }
    };
  }, [open]);

  useEffect(() => { setCursor(0); }, [q]);

  // Keep the highlighted row in view when arrowing past the fold.
  useEffect(() => {
    listRef.current?.querySelector('[data-on="1"]')?.scrollIntoView({ block: "nearest" });
  }, [cursor, results.length]);

  const choose = useCallback((it) => { if (it) { onSelect(it.id); onClose(); } }, [onSelect, onClose]);

  const onKeyDown = (e) => {
    const n = results.length;
    if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
    if (e.key === "Enter") { e.preventDefault(); choose(results[cursor]); return; }
    const down = e.key === "ArrowDown" || (e.ctrlKey && e.key === "n");
    const up = e.key === "ArrowUp" || (e.ctrlKey && e.key === "p");
    if (down) { e.preventDefault(); setCursor((c) => (n ? (c + 1) % n : 0)); }
    if (up) { e.preventDefault(); setCursor((c) => (n ? (c - 1 + n) % n : 0)); }
    if (e.key === "Home") { e.preventDefault(); setCursor(0); }
    if (e.key === "End") { e.preventDefault(); setCursor(Math.max(0, n - 1)); }
  };

  if (!open) return null;

  return (
    <div
      data-cmdk
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
      style={{
        position: "fixed", inset: 0, zIndex: 900, background: "rgba(6,10,17,.68)",
        display: "flex", alignItems: "flex-start", justifyContent: "center", padding: "12vh 16px 16px",
      }}
    >
      <div
        role="dialog" aria-modal="true" aria-label="Command palette"
        onKeyDown={onKeyDown}
        style={{
          width: "min(620px,100%)", background: T.panel, border: `1px solid ${T.line}`,
          borderRadius: 11, boxShadow: "0 18px 50px rgba(0,0,0,.5)", overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "12px 14px", borderBottom: `1px solid ${T.lineSoft}` }}>
          <span style={{ color: T.faint, fontFamily: MONO, fontSize: 13 }}>&gt;</span>
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Jump to a screen…"
            aria-label="Search screens"
            aria-controls="cmdk-list"
            style={{ flex: 1, background: "none", border: "none", outline: "none", color: T.text, fontSize: 14 }}
          />
          <span style={{ color: T.faint, fontSize: 10, fontFamily: MONO, border: `1px solid ${T.line}`, borderRadius: 4, padding: "1px 6px" }}>ESC</span>
        </div>

        <div id="cmdk-list" ref={listRef} role="listbox" aria-label="Screens" style={{ maxHeight: "48vh", overflowY: "auto", padding: 6 }}>
          {results.length === 0 && (
            <div style={{ padding: "16px 12px", color: T.faint, fontSize: 12.5 }}>
              Nothing matches “{q}”.
            </div>
          )}
          {results.map((it, i) => {
            const on = i === cursor;
            const Icon = it.icon;
            return (
              <div
                key={it.id}
                role="option"
                aria-selected={on}
                data-on={on ? "1" : "0"}
                onMouseMove={() => setCursor(i)}
                onClick={() => choose(it)}
                style={{
                  display: "flex", alignItems: "center", gap: 10, padding: "8px 10px",
                  borderRadius: 7, cursor: "pointer",
                  background: on ? T.panel2 : "transparent",
                  borderLeft: `2px solid ${on ? T.teal : "transparent"}`,
                }}
              >
                {Icon ? <Icon size={14} color={on ? T.teal : T.dim} /> : <span style={{ width: 14 }} />}
                <span style={{ color: on ? T.text : T.dim, fontSize: 13 }}>{it.label}</span>
                {it.group && (
                  <span style={{ color: T.faint, fontSize: 9.5, letterSpacing: 0.8, textTransform: "uppercase" }}>
                    {it.group}
                  </span>
                )}
                {it.id === current && (
                  <span style={{ marginLeft: "auto", color: T.teal, fontSize: 9.5, fontFamily: MONO }}>CURRENT</span>
                )}
              </div>
            );
          })}
        </div>

        <div style={{ display: "flex", gap: 14, padding: "8px 14px", borderTop: `1px solid ${T.lineSoft}`, color: T.faint, fontSize: 10.5 }}>
          <span><b style={{ color: T.dim, fontFamily: MONO }}>↑↓</b> move</span>
          <span><b style={{ color: T.dim, fontFamily: MONO }}>↵</b> open</span>
          <span><b style={{ color: T.dim, fontFamily: MONO }}>esc</b> close</span>
          <span style={{ marginLeft: "auto" }}>{results.length} of {items.length}</span>
        </div>
      </div>
    </div>
  );
}

/* Registers the global ⌘K / Ctrl-K toggle. Kept as a hook so the listener is
 * bound once at the shell rather than per screen. */
export function useCommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return [open, setOpen];
}
