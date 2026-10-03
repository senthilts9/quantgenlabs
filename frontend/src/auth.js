/* auth.js — token storage and the fetch wrapper that uses it.
 *
 * WHY A GLOBAL WRAPPER AND NOT 40 EDITED CALL SITES. There are roughly forty
 * fetch() calls across App.jsx, the screens and the crypto desk's TypeScript
 * modules. Editing each one guarantees that the next call someone adds is the
 * one that forgets the header, and the failure is a 401 in a panel nobody is
 * looking at. Wrapping fetch once covers every site, including modules this
 * file never imports.
 *
 * THE HEADER IS SCOPED TO SAME-ORIGIN /api/ REQUESTS, and that is the whole
 * reason this is careful rather than three lines. The crypto desk calls real
 * external venues — Coinbase, Binance, Hyperliquid, DefiLlama. A wrapper that
 * attached the bearer token to every outbound fetch would post this platform's
 * session token to third-party exchanges. Scoping by origin is not a nicety.
 *
 * STORAGE. localStorage, wrapped in try/catch: it throws in a private window
 * and in some embedded webviews, and a thrown read at module load would take
 * the whole app down rather than merely logging the person out. Every accessor
 * degrades to "no session" instead.
 */

const KEY = "qgl.session";

function readStored() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    // An expired token is the same as no token. Checking here means a reload
    // after expiry shows the login screen rather than a wall of 401s.
    if (s?.expires_at && new Date(s.expires_at).getTime() <= Date.now()) return null;
    return s?.access_token ? s : null;
  } catch {
    return null;
  }
}

function writeStored(session) {
  try {
    if (session) localStorage.setItem(KEY, JSON.stringify(session));
    else localStorage.removeItem(KEY);
  } catch {
    /* Non-persistent session. The app still works for this tab. */
  }
}

let current = readStored();
const listeners = new Set();

function emit() { listeners.forEach((fn) => { try { fn(current); } catch { /* ignore */ } }); }

export function getSession() { return current; }
export function isAuthenticated() { return !!current?.access_token; }
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function setSession(session) {
  current = session;
  writeStored(session);
  emit();
}

export function logout() { setSession(null); }

/** POST /api/auth/token. Returns the session, or throws with the server's reason. */
export async function login(username, password) {
  const r = await fetch("/api/auth/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    // The server deliberately returns one message for "no such user" and
    // "wrong password" so an attacker cannot enumerate usernames. Surfacing it
    // verbatim keeps that property instead of inventing a more specific one.
    throw new Error(body?.detail || `Sign-in failed (${r.status})`);
  }
  setSession(body);
  return body;
}

/** Whether enforcement is on, and whether any accounts exist. Unauthenticated. */
export async function authStatus() {
  try {
    const r = await fetch("/api/auth/status");
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

export function hasPermission(p) {
  return !!current?.permissions?.includes(p);
}

export function hasRole(r) {
  return !!current?.roles?.includes(r);
}

/* ------------------------------------------------------------ fetch wrapper */
let installed = false;

export function installAuthFetch({ onUnauthorized } = {}) {
  if (installed || typeof window === "undefined" || !window.fetch) return;
  installed = true;
  const original = window.fetch.bind(window);

  const sameOriginApi = (input) => {
    try {
      const url = new URL(
        typeof input === "string" ? input : input?.url ?? String(input),
        window.location.origin,
      );
      return url.origin === window.location.origin && url.pathname.startsWith("/api/");
    } catch {
      return false;
    }
  };

  window.fetch = async (input, init = {}) => {
    if (!sameOriginApi(input) || !current?.access_token) return original(input, init);

    // Headers may arrive as a Headers instance, an array or a plain object.
    // Normalising through Headers handles all three; building a plain object
    // would silently drop the first two forms.
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${current.access_token}`);

    const res = await original(input, { ...init, headers });
    if (res.status === 401) {
      // The token is gone or revoked. Clear it so the UI shows the login screen
      // instead of every panel failing separately with no explanation.
      setSession(null);
      onUnauthorized?.();
    }
    return res;
  };
}
