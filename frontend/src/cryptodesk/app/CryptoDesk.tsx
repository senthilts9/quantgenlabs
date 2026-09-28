import { useEffect, useState } from "react";
import { DeskNav } from "./DeskNav";
import { WalletModal } from "./WalletModal";
import { DeskDashboard } from "../features/overview/DeskDashboard";
import { TradingWorkstation } from "../features/trading/TradingWorkstation";
import { OrdersExecutions } from "../features/trading/OrdersExecutions";
import { Positions } from "../features/trading/Positions";
import type { TicketReport } from "../features/trading/OrderTicket";
import { AlgoExecution } from "../features/execution/AlgoExecution";
import { TCA } from "../features/execution/TCA";
import { Perpetuals } from "../features/perpetuals/Perpetuals";
// DEX board screens — the venue's public read surface. All live, all
// unauthenticated; see services/dexProviders.ts.
import { Markets } from "../features/markets/Markets";
import { OptionsBoard } from "../features/options/OptionsBoard";
import { StrategyVaults } from "../features/vaults/StrategyVaults";
import { FundingTape } from "../features/funding/FundingTape";
import { Risk } from "../features/risk/Risk";
import { Custody } from "../features/custody/Custody";
import { DepositModal } from "../features/custody/DepositModal";
import { Transfers } from "../features/custody/Transfers";
import { Blockchain } from "../features/blockchain/Blockchain";
import { Operations } from "../features/operations/Operations";
import { SystemHealth } from "../features/system/SystemHealth";
import { executions, initialOrders } from "../data/mockData";
import { VENUE, liveProviders } from "../services/liveProviders";
import { shortAddress } from "../services/wallet";
import type { DeskSession, Order, TicketOrder } from "../types/domain";
import { money, num } from "../utils/format";

/** Opening collateral for the local margin account. Deposit changes it. */
const STARTING_COLLATERAL = 250000;

/**
 * Execution venue for the *fill*.
 *
 * Deliberately not the venue whose book the ticket quotes. Validation runs
 * against the real venue's published rules — tick, step, bands, leverage caps
 * — because those are the constraints an order would actually meet. The fill
 * then comes from the local simulator, and the run is stored under that name,
 * because that is where it came from. Labelling a simulated fill with an
 * exchange's name is how a blotter starts lying.
 */
const EXECUTION_VENUE = "paper";

export function CryptoDesk() {
  const [screen, setScreen] = useState("Trading Workstation");
  const [orders, setOrders] = useState<Order[]>(initialOrders);

  // The session cookie is httpOnly, so the page cannot read it — it has to
  // ask. This is what restores a signed-in state across a reload.
  const [session, setSession] = useState<DeskSession | null>(null);
  useEffect(() => {
    let live = true;
    liveProviders.authSession().then(s => {
      if (live && s.authenticated && s.address) {
        setSession({ token: "", actor: s.address, expiresAt: null,
                     address: s.address, method: "SIWE" });
      }
    }).catch(() => { /* unauthenticated is a valid answer, not an error */ });
    return () => { live = false; };
  }, []);
  const [collateral, setCollateral] = useState(STARTING_COLLATERAL);
  const [modal, setModal] = useState<null | "deposit" | "wallet">(null);
  const [toast, setToast] = useState<null | {
    ok: boolean; symbol: string; kind: string; status: string;
    size: string; price: string; reason?: string;
    /** Renders a Log in button in the toast rather than making the user hunt
     *  for the one in the top bar. */
    needsAuth?: boolean;
  }>(null);

  const [submitting, setSubmitting] = useState(false);
  const [report, setReport] = useState<TicketReport | null>(null);
  // Bumped when a run is persisted, so the blotter refetches rather than
  // waiting out its poll interval.
  const [refreshKey, setRefreshKey] = useState(0);

  /**
   * Submit: validate, then execute and persist.
   *
   * The two calls are sequential and the first one gates the second. That
   * ordering is the whole discipline — TIER1_ARCHITECTURE §4 requires pre-trade
   * validation to be mandatory rather than advisory, which means refusing on a
   * reject, not logging it and sending anyway. A screen that fires both in
   * parallel and reports the validation afterwards has implemented the log.
   */
  const submit = async (o: TicketOrder) => {
    // Checked here as well as server-side. The API is the authority and
    // answers 401 regardless; this exists so the answer is a readable message
    // instead of a failed request the operator has to interpret.
    if (!session) {
      setToast({
        ok: false, symbol: o.symbol, kind: `${o.type} Order`,
        status: "Not signed in", size: num(o.qty, 6),
        price: o.limitPrice === null ? "Market" : String(o.limitPrice),
        reason: "Reads are open; placing an order needs a wallet signature.",
        needsAuth: true,
      });
      return;
    }
    setSubmitting(true);
    setReport(null);

    const id = `QG-${10426 + orders.length}`;
    const now = new Date().toLocaleTimeString("en-GB", { hour12: false });
    const staged: Order = {
      id, time: now, symbol: o.symbol, side: o.side, type: o.type, algo: o.algo,
      qty: o.qty, filled: 0, limitPrice: o.limitPrice ?? undefined,
      status: "NEW", venue: EXECUTION_VENUE, actor: o.actor,
    };
    setOrders(prev => [staged, ...prev]);
    const settle = (patch: Partial<Order>) =>
      setOrders(prev => prev.map(x => x.id === id ? { ...x, ...patch } : x));

    try {
      const v = await liveProviders.validateOrder({
        venue: VENUE, market: o.symbol, side: o.side, type: o.type,
        size: String(o.qty),
        price: o.limitPrice === null ? null : String(o.limitPrice),
        triggerPrice: o.triggerPrice === null ? null : String(o.triggerPrice),
        maxSlippageBps: String(o.maxSlippageBps),
        leverage: String(o.leverage),
        postOnly: o.postOnly, reduceOnly: o.reduceOnly,
      });

      if (!v?.retail?.accepted) {
        settle({ status: "REJECTED" });
        const rejections = v?.desk?.rejections ?? [];
        setReport({
          ok: false,
          message: v?.retail?.message ?? "Order rejected in pre-trade validation.",
          rejections, warnings: v?.desk?.warnings ?? [], repairs: v?.desk?.repairs ?? [],
        });
        setToast({
          ok: false, symbol: o.symbol, kind: `${o.type} Order`, status: "Rejected",
          size: num(o.qty, 6),
          price: o.limitPrice === null ? "Market" : String(o.limitPrice),
          // The first rule that fired is the reason. A toast listing all of
          // them is a toast nobody reads.
          reason: rejections[0]?.message ?? rejections[0]?.code ?? "pre-trade validation",
        });
        return;
      }

      settle({ status: "ACCEPTED" });

      const run = await liveProviders.runLifecycle({
        venue: EXECUTION_VENUE, market: o.symbol, side: o.side,
        size: String(o.qty),
        // DIRECT is one slice, which is what a direct order is. The endpoint
        // schedules TWAP or VWAP; a single slice of either is neither.
        algo: o.algo === "VWAP" ? "VWAP" : "TWAP",
        slices: Math.max(1, o.slices),
      });

      const exec = run?.stages?.execution ?? {};
      const persisted = run?.stages?.persistence ?? {};
      const avg = exec.avg_price == null ? undefined : Number(exec.avg_price);
      settle({
        status: exec.status === "FILLED" ? "FILLED" : (exec.status ?? "WORKING"),
        filled: Number(exec.filled_size ?? 0), avgPrice: avg,
      });

      setReport({
        ok: true,
        message: persisted.stored
          ? `Filled ${exec.filled_size} at ${exec.avg_price} — run persisted.`
          : `Filled ${exec.filled_size} at ${exec.avg_price} — but the run was NOT stored.`,
        parentId: run?.parent_id ?? null,
        // A persistence failure is not cosmetic: the fill happened and the
        // record of it did not, which is precisely the break reconciliation
        // exists to catch. Surface it rather than reporting a clean fill.
        warnings: [
          ...(v?.desk?.warnings ?? []),
          ...(persisted.error ? [`persistence failed: ${persisted.error}`] : []),
        ],
        repairs: v?.desk?.repairs ?? [],
      });

      setToast({
        ok: !!persisted.stored, symbol: o.symbol, kind: `${o.type} Order`,
        status: exec.status === "FILLED" ? "Filled" : (exec.status ?? "Working"),
        size: String(exec.filled_size ?? o.qty),
        price: exec.avg_price ? `$${Number(exec.avg_price).toFixed(2)}` : "Market",
        reason: persisted.error ? `not persisted: ${persisted.error}` : undefined,
      });
      setRefreshKey(k => k + 1);
    } catch (e) {
      const msg = (e as Error).message || String(e);
      // A session can lapse mid-ticket; the cookie expires server-side and the
      // page has no way to know until a call comes back 401.
      const lapsed = /sign in with a wallet|401/i.test(msg);
      if (lapsed) setSession(null);
      settle({ status: "REJECTED" });
      setReport({ ok: false, message: msg });
      setToast({
        ok: false, symbol: o.symbol, kind: `${o.type} Order`,
        status: lapsed ? "Not signed in" : "Failed",
        size: num(o.qty, 6),
        price: o.limitPrice === null ? "Market" : String(o.limitPrice),
        reason: lapsed ? "Your session expired. Sign in with your wallet to continue." : msg,
        needsAuth: lapsed,
      });
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = (id: string) =>
    setOrders(prev => prev.map(o => o.id === id ? { ...o, status: "CANCELLED" } : o));

  const render = () => ({
    "Desk Dashboard": <DeskDashboard />,
    "Trading Workstation": <TradingWorkstation
      session={session} collateral={collateral} submitting={submitting}
      report={report} refreshKey={refreshKey}
      onDeposit={() => setModal("deposit")} onConnect={() => setModal("wallet")}
      onSubmit={submit} />,
    "Orders & Executions": <OrdersExecutions
      orders={orders} executions={executions} onCancel={cancel} refreshKey={refreshKey} />,
    "Positions": <Positions />,
    "Algo Execution": <AlgoExecution />,
    "Execution & TCA": <TCA />,
    "Perpetuals": <Perpetuals />,
    "Markets": <Markets />,
    "Options Board": <OptionsBoard />,
    "Strategy Vaults": <StrategyVaults />,
    "Funding & Tape": <FundingTape />,
    "Exposure & P&L": <Risk />,
    "Limits & Alerts": <Risk alerts />,
    "Vaults & Wallets": <Custody />,
    "Transfers & Approvals": <Transfers />,
    "On-chain Activity": <Blockchain />,
    "Settlement": <Blockchain settlement />,
    "Reconciliation": <Operations />,
    "Audit Trail": <Operations audit />,
    "Connectivity & Health": <SystemHealth />
  } as Record<string, JSX.Element>)[screen] || <DeskDashboard />;

  return <div className="desk-layout">
    <DeskNav active={screen} onSelect={setScreen} />
    <main className="desk-content">
      <div className="desk-topbar">
        <span className="muted">Equity <b>{money(collateral)}</b></span>
        <span className="spacer" />
        <button className="deposit" onClick={() => setModal("deposit")}>Deposit</button>
        {session
          ? <button className="addr-pill" onClick={() => setModal("wallet")}>
              <i className="avatar sm" />{shortAddress(session.actor, 6, 4)}</button>
          : <button className="login" onClick={() => setModal("wallet")}>Log in</button>}
      </div>
      {render()}
    </main>

    {modal === "deposit" && <DepositModal collateral={collateral}
      onCredit={amount => setCollateral(c => c + amount)}
      onClose={() => setModal(null)} />}
    {modal === "wallet" && <WalletModal session={session}
      onSession={setSession} onClose={() => setModal(null)} />}

    {/* Order-status toast, as a venue reports it: what, how much, and — when
        it failed — the reason, which is the only part worth reading. */}
    {toast && <div className={`toast ${toast.ok ? "ok" : "bad"}`}>
      <header><b>{toast.symbol}</b><span className="tag">PERP</span>
        <span className="spacer" />{toast.kind}
        <button className="mini" onClick={() => setToast(null)}>✕</button></header>
      <div className="t-row"><span>Status</span>
        <b className={toast.ok ? "positive" : "negative"}>{toast.status}</b></div>
      <div className="t-row"><span>Size</span><b>{toast.size}</b></div>
      <div className="t-row"><span>Price</span><b>{toast.price}</b></div>
      {toast.reason && <div className="t-row wrap"><span>Reason</span><b>{toast.reason}</b></div>}
      {toast.needsAuth && <button className="primary wide"
        onClick={() => { setToast(null); setModal("wallet"); }}>
        Log in with your wallet</button>}
    </div>}
  </div>;
}
