import { useAsync } from "../../hooks/useAsync";
import { liveProviders, type LiveBook } from "../../services/liveProviders";
import { money, num, px } from "../../utils/format";

/**
 * Contract header: last, 24h change, mark, spot, funding, volume, open interest.
 *
 * Every figure here comes from a different endpoint on purpose, because they
 * are different facts and they fail independently:
 *
 * * **Last / Mark** — `/book`, the venue's own touch and mark.
 * * **Spot** — `/chain`, a Chainlink aggregator read at a named block. It is
 *   deliberately *not* the venue's spot: two sources that share no failure
 *   mode are what make a mark checkable, and a stale socket at the venue does
 *   not make the chain agree.
 * * **Funding** — `/funding`, annualised, with the settlement period shown,
 *   because the same per-period rate is eight times the cost when it settles
 *   hourly rather than eight-hourly.
 * * **24h volume / OI / change** — `/perps`, the venue's published statistics.
 *
 * A cell that could not be fetched renders "—" rather than zero. Zero open
 * interest is a market nobody holds; unknown open interest is a broken fetch,
 * and a header that conflates them is lying in the direction of confidence.
 */
export function InstrumentBar({ symbol, book, bookError }: {
  symbol: string; book: LiveBook | null; bookError: string | null;
}) {
  // Venue statistics move on the minute, not the tick.
  const perp = useAsync(
    async () => (await liveProviders.getPerps(1, "mainnet", symbol)).rows[0] ?? null,
    [symbol], 60000);
  const funding = useAsync(() => liveProviders.getFunding(symbol), [symbol], 60000);
  // The oracle read is the slowest and the most expensive; it is also the one
  // that matters least often, so it polls least.
  const chain = useAsync(
    async () => liveProviders.getChainCheck(symbol.replace("-PERP", "")).catch(() => null),
    [symbol], 120000);

  const mid = book?.mid ?? null;
  const change = perp.data?.change24hPct ?? null;
  const fundingRow = funding.data?.find(f => f.venue === "hyperliquid") ?? funding.data?.[0] ?? null;
  const [base] = symbol.split("-");

  return <div className="inst-bar">
    <div className="inst-name">
      <span className="coin">{base.slice(0, 1)}</span>
      <b>{symbol.replace("-PERP", "")}</b>
      <span className="tag">PERP</span>
    </div>

    <Stat label="Last Price" big value={mid === null ? "—" : px(mid)}
      tone={change === null ? "" : change >= 0 ? "positive" : "negative"} />
    <Stat label="24h Change"
      value={change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}
      tone={change === null ? "" : change >= 0 ? "positive" : "negative"} />
    <Stat label="Mark Price" value={book ? `$${px(book.mark)}` : "—"} />
    <Stat label="Spot Price"
      value={chain.data ? `$${px(chain.data.oraclePrice)}` : "—"}
      title={chain.data ? `Chainlink ${chain.data.oracleSource} @ block ${chain.data.block}` : undefined} />
    <Stat
      label={fundingRow ? `${fundingRow.periodHours}h Funding` : "Funding"}
      value={fundingRow ? `${fundingRow.ratePct.toFixed(4)}%` : "—"}
      tone={fundingRow ? (fundingRow.ratePct >= 0 ? "positive" : "negative") : ""}
      title={fundingRow ? `${fundingRow.aprPct.toFixed(2)}% APR · ${fundingRow.direction}` : undefined} />
    <Stat label="24h Volume" value={perp.data ? money(perp.data.dayVolumeUsd, 0) : "—"} />
    <Stat label="Open Interest" value={perp.data ? money(perp.data.openInterestUsd, 0) : "—"} />

    <span className={`status ${bookError ? "critical" : book ? "healthy" : "warning"}`}>
      <i />{bookError ? "FEED ERROR" : book ? `LIVE · ${book.source.toUpperCase()}` : "CONNECTING"}
    </span>
  </div>;
}

function Stat({ label, value, tone = "", big = false, title }: {
  label: string; value: string; tone?: string; big?: boolean; title?: string;
}) {
  return <div className={`inst-stat${big ? " big" : ""}`} title={title}>
    <span>{label}</span><b className={tone}>{value}</b>
  </div>;
}
