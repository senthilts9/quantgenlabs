export const money = (n:number, digits=2) => new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:digits}).format(n);
export const num = (n:number, digits=2) => new Intl.NumberFormat("en-US",{maximumFractionDigits:digits}).format(n);
export const pct = (n:number) => `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;

/**
 * Price precision that follows magnitude.
 *
 * A fixed 2dp renders every DOGE book level as "0.08" — eight identical rows
 * where the real ticks differ in the 6th decimal. Cheap assets need more
 * decimals, not the same number.
 */
export const px = (n: number) =>
  n >= 1000 ? n.toFixed(2)
  : n >= 1 ? n.toFixed(4)
  : n.toFixed(7);

/** Wall-clock time of an epoch-ms stamp, to the millisecond. A blotter read
 *  to the second cannot order two children of the same parent. */
export const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString("en-GB", { hour12: false }) +
  "." + String(ms % 1000).padStart(3, "0");

export const stamp = (ms: number) => new Date(ms).toLocaleString("en-GB", { hour12: false });

/** Coarse age. Exact enough to answer "is this stale?", which is all it is for. */
export const ago = (ms: number) => {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

/** bps with an explicit sign — an unsigned shortfall hides which way it went. */
export const bps = (n: number | null, digits = 2) =>
  n === null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)} bps`;
