import { useState } from "react";
import { Modal } from "../../components/UI";
import { liveProviders } from "../../services/liveProviders";
import { money, num } from "../../utils/format";

/**
 * Deposit: fund the margin account the ticket prices against.
 *
 * Two steps, as a venue does it — choose a route, then act on it. The routes
 * differ in what this host can honestly offer:
 *
 * * **External wallet** shows a deposit address and reads its **real on-chain
 *   balance** at a named block. The read is evidence; the block number is what
 *   makes it reproducible, since a balance nobody can re-derive is an
 *   assertion.
 * * **Credit locally** sets the collateral figure margin and liquidation are
 *   computed from. Without it both are undefined, because this host has no
 *   custody service to ask.
 *
 * **No transaction is originated on either path.** The chain module in this
 * codebase is read-only by construction — a library that could both build and
 * broadcast makes an address-handling bug irreversible — so a deposit screen
 * here confirms on-chain state and records intent, and never moves funds.
 */

const NETWORKS = [
  { id: "sepolia", label: "Ethereum Sepolia" },
  { id: "base-sepolia", label: "Base Sepolia" },
  { id: "ethereum", label: "Ethereum Mainnet" },
];

type Route = null | "external" | "local";

export function DepositModal({ collateral, onCredit, onClose }: {
  collateral: number; onCredit: (amount: number) => void; onClose: () => void;
}) {
  const [route, setRoute] = useState<Route>(null);
  const [amount, setAmount] = useState("25000");
  const [address, setAddress] = useState("");
  const [chain, setChain] = useState("sepolia");
  const [snapshot, setSnapshot] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);

  const parsed = Number(amount);
  const valid = Number.isFinite(parsed) && parsed > 0;
  const addressOk = /^0x[0-9a-fA-F]{40}$/.test(address.trim());

  const read = async () => {
    setReading(true); setError(null); setSnapshot(null);
    try { setSnapshot(await liveProviders.getWallet(address.trim(), chain)); }
    catch (e) { setError((e as Error).message); }
    finally { setReading(false); }
  };

  /* ------------------------------------------------------------- chooser */
  if (route === null) {
    return <Modal title="Deposit" onClose={onClose}>
      <div className="kv"><span>Token</span><b>USDC</b></div>
      <div className="kv"><span>Current collateral</span><b>{money(collateral)}</b></div>

      <button className="route-card" onClick={() => setRoute("local")}>
        <div><b>Credit margin account</b><small>Sets the collateral the ticket prices against · instant</small></div>
        <span>›</span>
      </button>

      <div className="bracket-head">Other deposit methods</div>
      <button className="route-card" onClick={() => setRoute("external")}>
        <div><b>Deposit from External Wallet</b><small>Verify an on-chain balance at a named block · read-only</small></div>
        <span>›</span>
      </button>
      <button className="route-card disabled" disabled>
        <div><b>Deposit from Exchange</b><small>Unavailable — this host originates no transactions</small></div>
        <span>›</span>
      </button>
    </Modal>;
  }

  /* -------------------------------------------------------- local credit */
  if (route === "local") {
    return <Modal title="Credit Margin Account" onClose={onClose} footer={
      <>
        <button className="mini" onClick={() => setRoute(null)}>‹ Back</button>
        <button className="primary" disabled={!valid}
          onClick={() => { onCredit(parsed); onClose(); }}>
          Credit {valid ? money(parsed) : "—"}</button>
      </>
    }>
      <div className="kv"><span>Current collateral</span><b>{money(collateral)}</b></div>
      <label>Amount (USDC)
        <input type="number" min={0} step={1000} value={amount}
          onChange={e => setAmount(e.target.value)} /></label>
      {valid && <div className="kv"><span>After credit</span>
        <b className="positive">{money(collateral + parsed)}</b></div>}
      <div className="notice warn">
        No transaction is originated. This sets the collateral figure the order
        ticket uses for margin, liquidation and position sizing — without it
        both margin and liquidation price are undefined. Funding a real account
        happens through custody, not through a browser.
      </div>
    </Modal>;
  }

  /* ------------------------------------------------------ external wallet */
  return <Modal title="Deposit from External Wallet" onClose={onClose} footer={
    <>
      <button className="mini" onClick={() => setRoute(null)}>‹ Back</button>
      <span className="muted small">Read-only. Nothing is broadcast.</span>
    </>
  }>
    <div className="kv"><span>Token</span><b>USDC</b></div>
    <label>From Network
      <select value={chain} onChange={e => { setChain(e.target.value); setSnapshot(null); }}>
        {NETWORKS.map(n => <option key={n.id} value={n.id}>{n.label}</option>)}
      </select></label>

    <label>Address
      <input value={address} placeholder="0x…" spellCheck={false}
        onChange={e => setAddress(e.target.value)} /></label>
    {address.trim() !== "" && !addressOk &&
      <div className="notice warn">Not a 20-byte hex address.</div>}

    <button className="primary wide" disabled={!addressOk || reading} onClick={read}>
      {reading ? "Reading chain…" : "Read balance"}</button>

    {error && <div className="notice reject">{error}</div>}

    {snapshot && <div className="kv-block">
      <div className="kv"><span>Network</span><b>{snapshot.chain}</b></div>
      <div className="kv"><span>Block</span><b className="mono">{snapshot.block}</b></div>
      <div className="kv"><span>{snapshot.native_symbol}</span>
        <b className="mono">{num(Number(snapshot.native), 6)}</b></div>
      {(snapshot.tokens || []).map((t: any) => <div className="kv" key={t.address}>
        <span>{t.symbol}</span>
        <b className="mono">{num(Number(t.amount), 6)}
          {/* An unpriced token and a worthless one are different facts. */}
          {t.usd_value !== null && <small> · {money(Number(t.usd_value))}</small>}</b>
      </div>)}
      {!(snapshot.tokens || []).length &&
        <div className="muted small">No tracked ERC-20 balances at this address.</div>}
    </div>}

    <div className="notice warn">
      This reads an address; it does not credit the desk. The balance and its
      block number come from a public RPC and are reproducible. To make funds
      spendable by the ticket, use <b>Credit margin account</b>.
    </div>
  </Modal>;
}
