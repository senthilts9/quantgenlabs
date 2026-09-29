/* useStream.js — WebSocket subscription with reconnect and gap detection.
 *
 * Replaces `setInterval` polling. The screens here refreshed every 8–60 seconds,
 * which meant data was stale by up to a full interval, every client re-fetched
 * the whole payload whether or not anything had changed, and load scaled with
 * client count rather than with market activity.
 *
 * WHAT THIS HANDLES THAT A NAIVE `new WebSocket(...)` DOES NOT:
 *
 *   RECONNECT WITH BACKOFF AND JITTER. A dropped connection must retry, but a
 *   fixed retry interval means every client reconnects in lockstep after a server
 *   restart and stampedes it. Exponential backoff with jitter spreads them out.
 *
 *   GAP DETECTION. Frames carry a sequence number. A jump means frames were
 *   dropped — which the server does deliberately under backpressure — so the UI
 *   can say so rather than silently showing stale data as if it were current.
 *
 *   STALENESS IS SURFACED, NOT HIDDEN. `msSinceFrame` lets a screen show how old
 *   what it is displaying actually is. A trading UI that shows a number with no
 *   indication of its age is the failure mode this exists to prevent.
 *
 *   THE SOCKET IS CLOSED ON UNMOUNT. React Strict Mode mounts twice in
 *   development; without cleanup that leaves an orphaned socket per mount, and
 *   the server's client limit fills with connections nobody is reading.
 */
import { useCallback, useEffect, useRef, useState } from "react";

const MAX_BACKOFF_MS = 15000;
const BASE_BACKOFF_MS = 500;

export default function useStream(path, { enabled = true, onFrame } = {}) {
  const [status, setStatus] = useState("idle");  // idle|connecting|open|closed|error
  const [lastFrame, setLastFrame] = useState(null);
  const [gaps, setGaps] = useState(0);
  const [msSinceFrame, setMsSinceFrame] = useState(null);

  const wsRef = useRef(null);
  const attemptRef = useRef(0);
  const seqRef = useRef(null);
  const lastAtRef = useRef(null);
  const timerRef = useRef(null);
  const closedRef = useRef(false);
  // Held in a ref so a changing callback identity does not tear down the socket.
  const onFrameRef = useRef(onFrame);
  useEffect(() => { onFrameRef.current = onFrame; }, [onFrame]);

  const connect = useCallback(() => {
    if (closedRef.current || !path) return;
    setStatus("connecting");

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}${path}`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch {
      setStatus("error");
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      attemptRef.current = 0;      // reset backoff only on a SUCCESSFUL open
      seqRef.current = null;
      setStatus("open");
    };

    ws.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;                    // a malformed frame is dropped, not fatal
      }
      if (typeof frame.seq === "number") {
        const prev = seqRef.current;
        // The server drops frames deliberately under backpressure, so a gap is
        // expected behaviour rather than an error -- but it must be visible.
        if (prev !== null && frame.seq > prev + 1) setGaps((g) => g + 1);
        seqRef.current = frame.seq;
      }
      lastAtRef.current = Date.now();
      setMsSinceFrame(0);
      setLastFrame(frame);
      onFrameRef.current?.(frame);
    };

    ws.onerror = () => setStatus("error");

    ws.onclose = () => {
      wsRef.current = null;
      if (closedRef.current) { setStatus("closed"); return; }
      setStatus("closed");
      // Exponential backoff with jitter: a fixed interval makes every client
      // reconnect in lockstep after a server restart and stampede it.
      const n = attemptRef.current++;
      const backoff = Math.min(BASE_BACKOFF_MS * 2 ** n, MAX_BACKOFF_MS);
      const jitter = Math.random() * backoff * 0.3;
      timerRef.current = setTimeout(connect, backoff + jitter);
    };
  }, [path]);

  useEffect(() => {
    closedRef.current = false;
    if (enabled) connect();
    return () => {
      // Strict Mode mounts twice in development; without this each mount leaves
      // an orphaned socket and the server's client limit fills with connections
      // nobody is reading.
      closedRef.current = true;
      clearTimeout(timerRef.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect, enabled]);

  // Age of what is on screen. A trading UI showing a number with no indication
  // of how old it is is the failure this exists to prevent.
  useEffect(() => {
    const t = setInterval(() => {
      if (lastAtRef.current) setMsSinceFrame(Date.now() - lastAtRef.current);
    }, 500);
    return () => clearInterval(t);
  }, []);

  return { status, lastFrame, gaps, msSinceFrame, connected: status === "open" };
}
