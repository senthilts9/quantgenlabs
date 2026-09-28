import React, { useEffect, useRef, useState } from "react";

/*
 * CryptoCopilot — Claude chat grounded in live desk state.
 * Streams from POST /api/copilot/stream (SSE). Sends the current positions,
 * funding rates and return windows so answers reason about the real book.
 * Falls back to the backend's deterministic offline summary when no API key
 * is set, so the panel is never dead in a demo.
 *
 * Matches the Meridian / genai-risk-dashboard design tokens.
 */

const T = {
  bg: "#0E1420", panel: "#141C2A", panel2: "#101725", line: "#233144",
  lineSoft: "#1A2536", text: "#E6ECF3", dim: "#8A99AE",
  teal: "#38BFA6", blue: "#5B9BF0", amber: "#E0A44E", red: "#E5535B",
};
const MONO = "'IBM Plex Mono',monospace";

const SUGGESTIONS = [
  "Which position is closest to liquidation?",
  "What's my net funding carry across the book?",
  "Explain my XRP-PERP tail risk in plain English.",
  "If XRP drops 8% intraday, what happens to my margin?",
];

export default function CryptoCopilot({ apiBase = "", desk }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scroller = useRef(null);

  useEffect(() => {
    scroller.current?.scrollTo(0, scroller.current.scrollHeight);
  }, [messages]);

  async function send(text) {
    const q = (text ?? input).trim();
    if (!q || busy) return;
    setInput("");
    const next = [...messages, { role: "user", content: q }];
    setMessages([...next, { role: "assistant", content: "" }]);
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/copilot/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: next,
          positions: desk?.positions ?? [],
          funding_rates: desk?.funding_rates ?? {},
          returns_window: desk?.returns_window ?? {},
        }),
      });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const p of parts) {
          const line = p.replace(/^data: /, "");
          if (line === "[DONE]") continue;
          try {
            const { text: tok } = JSON.parse(line);
            setMessages((m) => {
              const copy = [...m];
              copy[copy.length - 1] = {
                role: "assistant",
                content: copy[copy.length - 1].content + (tok || ""),
              };
              return copy;
            });
          } catch { /* keep partial buffer */ }
        }
      }
    } catch (e) {
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant",
          content: `Copilot unreachable: ${e.message}. Is the backend running?` };
        return copy;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section style={wrap}>
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={dot} />
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 600 }}>Risk Copilot</div>
            <div style={{ color: T.dim, fontSize: 11 }}>
              Claude · grounded in your live desk state
            </div>
          </div>
        </div>
        <span style={badge}>{desk?.positions?.length ?? 0} positions in context</span>
      </div>

      <div ref={scroller} style={feed}>
        {messages.length === 0 && (
          <div style={{ display: "grid", gap: 8 }}>
            <div style={{ color: T.dim, fontSize: 12.5, marginBottom: 2 }}>
              Ask about the book — the copilot sees your positions, funding and tail risk.
            </div>
            {SUGGESTIONS.map((s) => (
              <button key={s} onClick={() => send(s)} style={chip}>{s}</button>
            ))}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ display: "flex",
            justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
            <div style={m.role === "user" ? bubbleUser : bubbleBot}>
              {m.content || (busy && i === messages.length - 1
                ? <span style={{ color: T.dim }}>▍</span> : "")}
            </div>
          </div>
        ))}
      </div>

      <div style={composer}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask the risk copilot…"
          style={inputBox}
        />
        <button onClick={() => send()} disabled={busy} style={{ ...sendBtn,
          opacity: busy ? 0.5 : 1 }}>{busy ? "…" : "Send"}</button>
      </div>
    </section>
  );
}

const wrap = { display: "flex", flexDirection: "column", height: "100%",
  background: T.panel, border: `1px solid ${T.line}`, borderRadius: 10, overflow: "hidden" };
const header = { display: "flex", justifyContent: "space-between", alignItems: "center",
  padding: "12px 14px", borderBottom: `1px solid ${T.line}` };
const dot = { width: 9, height: 9, borderRadius: 9, background: T.teal,
  boxShadow: `0 0 8px ${T.teal}` };
const badge = { fontSize: 10.5, color: T.dim, border: `1px solid ${T.line}`,
  borderRadius: 5, padding: "3px 7px", fontFamily: MONO };
const feed = { flex: 1, overflowY: "auto", padding: 14, display: "grid",
  gap: 10, alignContent: "start" };
const bubbleBot = { maxWidth: "88%", background: T.panel2, border: `1px solid ${T.lineSoft}`,
  borderRadius: "10px 10px 10px 3px", padding: "9px 12px", fontSize: 13, lineHeight: 1.5,
  color: T.text, whiteSpace: "pre-wrap" };
const bubbleUser = { maxWidth: "88%", background: T.teal, color: T.bg,
  borderRadius: "10px 10px 3px 10px", padding: "9px 12px", fontSize: 13, lineHeight: 1.5 };
const chip = { textAlign: "left", background: T.panel2, border: `1px solid ${T.line}`,
  borderRadius: 8, padding: "8px 11px", color: T.blue, fontSize: 12.5, cursor: "pointer" };
const composer = { display: "flex", gap: 8, padding: 12, borderTop: `1px solid ${T.line}` };
const inputBox = { flex: 1, background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8,
  padding: "9px 12px", color: T.text, fontSize: 13, outline: "none" };
const sendBtn = { background: T.teal, color: T.bg, border: "none", borderRadius: 8,
  padding: "0 16px", fontWeight: 600, cursor: "pointer", fontSize: 13 };
