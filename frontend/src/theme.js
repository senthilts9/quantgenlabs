/* theme.js — the console's palette, in one place.
 *
 * Extracted from App.jsx so screens living in their own modules use the same
 * tokens rather than a hand-copied duplicate that drifts a shade at a time.
 *
 * Colour encodes risk bands and state, not decoration: teal = healthy/measured,
 * amber = caution or "not measured here", red = breach or rejection.
 */
export const T = {
  bg: "#0E1420", panel: "#141C2A", panel2: "#101725", line: "#233144",
  lineSoft: "#1A2536", text: "#E6ECF3", dim: "#8A99AE", faint: "#5B6B82",
  teal: "#38BFA6", blue: "#5B9BF0", amber: "#E0A44E", red: "#E5535B",
  green: "#46C08A", violet: "#9B87F5",
};

export const MONO = "'IBM Plex Mono',monospace";
