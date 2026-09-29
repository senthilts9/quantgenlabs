/* theme.js — the console's palette, as CSS custom properties.
 *
 * WHY VARIABLES RATHER THAN LITERAL HEX. The tokens used to be hex strings
 * inlined into React style objects, which meant the palette was fixed at build
 * time: there was no way to offer a light theme without rewriting every style
 * object in a 4,000-line file.
 *
 * Every token is now `var(--qgl-*)`, so inline styles keep working unchanged and
 * the theme switches by setting one attribute on <html>. React never re-renders
 * for a theme change; the browser repaints.
 *
 * THE ONE THING THIS BREAKS, and how it is handled. `${T.teal}14` — appending a
 * hex alpha pair to a colour — produces `var(--qgl-teal)14`, which is invalid
 * CSS and silently renders nothing. Tokens that need transparency are therefore
 * declared explicitly as ALPHA below rather than constructed by concatenation.
 * Concatenating onto a token is now a mistake the palette does not support, and
 * that is deliberate: a silently-invisible element is worse than a compile error.
 *
 * COLOUR ENCODES STATE, NOT DECORATION: teal = healthy or measured, amber =
 * caution or "not measured here", red = breach or rejection. That contract holds
 * in both themes, which is why the light palette is a re-derivation rather than a
 * naive inversion — a light theme with the same lightness ordering would put
 * "good" and "bad" at the same visual weight.
 */

/* Semantic tokens. The values are variable references; the definitions live in
 * THEME_CSS below. */
export const T = {
  bg: "var(--qgl-bg)",
  panel: "var(--qgl-panel)",
  panel2: "var(--qgl-panel2)",
  line: "var(--qgl-line)",
  lineSoft: "var(--qgl-line-soft)",
  text: "var(--qgl-text)",
  dim: "var(--qgl-dim)",
  faint: "var(--qgl-faint)",
  teal: "var(--qgl-teal)",
  blue: "var(--qgl-blue)",
  amber: "var(--qgl-amber)",
  red: "var(--qgl-red)",
  green: "var(--qgl-green)",
  violet: "var(--qgl-violet)",
};

/* Transparent fills, declared rather than concatenated. See the note above. */
export const ALPHA = {
  green: "var(--qgl-green-a)",
  red: "var(--qgl-red-a)",
  teal: "var(--qgl-teal-a)",
  blue: "var(--qgl-blue-a)",
};

export const MONO = "'IBM Plex Mono',monospace";

export const THEMES = ["dark", "light"];
const STORAGE_KEY = "qgl-theme";

export const THEME_CSS = `
:root, [data-theme="dark"] {
  --qgl-bg:#0E1420; --qgl-panel:#141C2A; --qgl-panel2:#101725;
  --qgl-line:#233144; --qgl-line-soft:#1A2536;
  --qgl-text:#E6ECF3; --qgl-dim:#8A99AE; --qgl-faint:#5B6B82;
  --qgl-teal:#38BFA6; --qgl-blue:#5B9BF0; --qgl-amber:#E0A44E;
  --qgl-red:#E5535B; --qgl-green:#46C08A; --qgl-violet:#9B87F5;
  --qgl-green-a:rgba(70,192,138,.08); --qgl-red-a:rgba(229,83,91,.08);
  --qgl-teal-a:rgba(56,191,166,.10);  --qgl-blue-a:rgba(91,155,240,.10);
  color-scheme: dark;
}

/* Light is a re-derivation, not an inversion. The accents are DARKENED rather
 * than reused: #38BFA6 on white is ~1.9:1 and unreadable as text, so each accent
 * is stepped down until it clears 4.5:1 on the panel colour while keeping its
 * hue — the state contract (teal good / amber caution / red breach) has to
 * survive the theme change or the colour stops meaning anything. */
[data-theme="light"] {
  --qgl-bg:#F4F6F9; --qgl-panel:#FFFFFF; --qgl-panel2:#F7F9FC;
  --qgl-line:#D8DEE8; --qgl-line-soft:#E8EDF4;
  --qgl-text:#111823; --qgl-dim:#55637A; --qgl-faint:#7A889E;
  --qgl-teal:#0A7D6B; --qgl-blue:#1F5FC4; --qgl-amber:#9A6212;
  --qgl-red:#B3202A; --qgl-green:#127046; --qgl-violet:#5B41C7;
  --qgl-green-a:rgba(18,112,70,.09); --qgl-red-a:rgba(179,32,42,.09);
  --qgl-teal-a:rgba(10,125,107,.10); --qgl-blue-a:rgba(31,95,196,.10);
  color-scheme: light;
}
`;

/** Resolved theme: an explicit choice, else the OS preference. */
export function initialTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (THEMES.includes(saved)) return saved;
  } catch {
    /* private mode / blocked storage — fall through to the OS preference */
  }
  try {
    if (window.matchMedia?.("(prefers-color-scheme: light)").matches) return "light";
  } catch { /* no matchMedia */ }
  return "dark";
}

export function applyTheme(theme) {
  const t = THEMES.includes(theme) ? theme : "dark";
  document.documentElement.setAttribute("data-theme", t);
  // Mobile browser chrome reads this; left unchanged it stays dark behind a
  // light page.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "light" ? "#F4F6F9" : "#0E1420");
  try {
    localStorage.setItem(STORAGE_KEY, t);
  } catch {
    /* Storage can be unavailable; the theme still applies for this session.
       Refusing to switch because we cannot remember it would be worse. */
  }
  return t;
}
