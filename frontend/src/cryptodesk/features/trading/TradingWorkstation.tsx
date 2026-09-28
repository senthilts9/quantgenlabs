import { useMemo, useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { VENUE, liveProviders } from "../../services/liveProviders";
import type { DeskSession, TicketOrder } from "../../types/domain";
import { num, px } from "../../utils/format";
import { AccountPanel } from "./AccountPanel";
import { InstrumentBar } from "./InstrumentBar";
import { OrderTicket, type TicketReport } from "./OrderTicket";
import { DeskTabs } from "./DeskTabs";

/** Real contracts on the configured public venue. */
const WATCH = ["BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP", "DOGE-USD-PERP"];

export function TradingWorkstation({
  session, collateral, submitting, report, refreshKey, onDeposit, onConnect, onSubmit,
}: {
  session: DeskSession | null;
  collateral: number;
  submitting: boolean;
  report: TicketReport | null;
  refreshKey: number;
  onDeposit: () => void;
  onConnect: () => void;
  onSubmit: (o: TicketOrder) => void;
}) {
  const [symbol, setSymbol] = useState("BTC-USD-PERP");
  const [bookTab, setBookTab] = useState<"BOOK" | "TRADES">("BOOK");

  const book = useAsync(() => liveProviders.getBook(symbol, VENUE), [symbol], 5000);
  const specs = useAsync(() => liveProviders.getSpecs(VENUE), []);

  // Position is derived from persisted runs rather than held in the browser:
  // the blotter is the record, and a position the screen invented would
  // disagree with it the moment the tab reloads.
  const runs = useAsync(() => liveProviders.getRuns(200, symbol), [symbol, refreshKey], 15000);
  const position = useMemo(() => {
    let size = 0, cost = 0;
    for (const r of runs.data ?? []) {
      if (r.avgPrice === null || !r.filledSize) continue;
      const signed = r.side === "BUY" ? r.filledSize : -r.filledSize;
      size += signed;
      cost += signed * r.avgPrice;
    }
    return { size, entry: size !== 0 ? cost / size : null };
  }, [runs.data]);

  const spec = specs.data?.[symbol];
  const mid = book.data?.mid ?? null;
  const asks = book.data?.levels.filter(l => l.side === "ASK") ?? [];
  const bids = book.data?.levels.filter(l => l.side === "BID") ?? [];
  const [base] = symbol.split("-");

  const marginUsed = spec && mid !== null
    ? Math.abs(position.size) * mid * spec.imFraction : 0;

  return <div className="trade-screen">
    <InstrumentBar symbol={symbol} book={book.data} bookError={book.error} />

    <div className="trade-grid">
      <aside className="markets">
        <div className="panel-head"><div>Markets</div></div>
        {WATCH.map(s => {
          const sp = specs.data?.[s];
          return <button key={s} className={s === symbol ? "selected" : ""}
            onClick={() => setSymbol(s)}>
            <div><b>{s.replace("-PERP", "")}</b><small>PERP · {sp ? `${num(sp.maxLeverage, 0)}x` : "—"}</small></div>
          </button>;
        })}
      </aside>

      <section className="book-col">
        <div className="ord-tabs sub">
          {(["BOOK", "TRADES"] as const).map(t =>
            <button key={t} className={bookTab === t ? "active" : ""} onClick={() => setBookTab(t)}>
              {t === "BOOK" ? "Book" : "Trades"}</button>)}
          <span className="spacer" />
          <span className="muted small">{book.data?.source ?? "—"}</span>
        </div>

        {bookTab === "BOOK" ? <>
          <div className="book-head"><span>Price</span><span>Size</span><span>Total ({base})</span></div>
          {book.error && <div className="empty">Feed unavailable — {book.error}</div>}
          {!book.data && book.loading && <div className="empty">Loading book…</div>}
          {book.data && <>
            <div className="book-side asks">
              {asks.map((l, i) => <Level key={"a" + i} l={l} side="ask" max={asks[0]?.total ?? 1} />)}
            </div>
            <div className="book-mid">
              <b>{px(book.data.mid)}</b>
              <span>Spread <b>{book.data.spreadBps.toFixed(3)}%</b></span>
            </div>
            <div className="book-side bids">
              {bids.map((l, i) => <Level key={"b" + i} l={l} side="bid" max={bids[bids.length - 1]?.total ?? 1} />)}
            </div>
          </>}
        </> : <div className="empty">
          The tape needs a trades feed. This host polls REST for the book; a
          streaming gateway is stage 2 of the architecture and is not built.
        </div>}
      </section>

      <section className="ticket-col">
        <OrderTicket
          symbol={symbol} book={book.data} spec={spec} specError={specs.error}
          collateral={collateral} positionSize={position.size}
          session={session} venue={VENUE} submitting={submitting} report={report}
          onDeposit={onDeposit} onConnect={onConnect} onSubmit={onSubmit} />
        <AccountPanel symbol={symbol} collateral={collateral}
          positionSize={position.size} markPrice={book.data?.mark ?? null}
          entryPrice={position.entry} marginUsed={marginUsed} />
      </section>
    </div>

    <DeskTabs symbol={symbol} refreshKey={refreshKey}
      positionSize={position.size} entryPrice={position.entry}
      markPrice={book.data?.mark ?? null} collateral={collateral} />
  </div>;
}

/** One book level, with a depth bar behind it sized by cumulative total. */
function Level({ l, side, max }: { l: { price: number; size: number; total: number }; side: "ask" | "bid"; max: number }) {
  const pctWidth = max > 0 ? Math.min(100, (l.total / max) * 100) : 0;
  return <div className={`book-row ${side}`}>
    <i style={{ width: `${pctWidth}%` }} />
    <span>{px(l.price)}</span><span>{l.size.toFixed(5)}</span><span>{l.total.toFixed(5)}</span>
  </div>;
}
