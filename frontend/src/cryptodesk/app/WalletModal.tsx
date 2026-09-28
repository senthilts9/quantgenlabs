import { useEffect, useState } from "react";
import { Modal } from "../components/UI";
import { liveProviders } from "../services/liveProviders";
import {
  WALLET_CATALOG, connectWallet, currentChainId, discoverWallets,
  sessionFromSignature, shortAddress, signMessage, siweMessage,
  type DiscoveredWallet,
} from "../services/wallet";
import type { DeskSession } from "../types/domain";

/**
 * Connect wallet → choose wallet → sign in.
 *
 * Two steps, as a venue does it, and the split is not decoration. Browsers
 * routinely have several wallets installed, and they all compete for the single
 * `window.ethereum` slot — so "connect" without a chooser opens whichever one
 * won that race, which is frequently not the one the user meant. The list comes
 * from EIP-6963 announcements, where each wallet identifies itself separately.
 *
 * Sign-in is a **wallet signature**, not a password. The account is an address;
 * proving control of it means signing an EIP-4361 challenge whose nonce the
 * server issued and will burn after one use.
 *
 * **Where this deliberately stops short.** Paradex derives a Starknet L2
 * account from the L1 signature and signs every order with that L2 key. That
 * second step is absent by design: TIER1_ARCHITECTURE §1 puts signing material
 * in an execution gateway that is never publicly reachable, and this console is
 * the public tier. Signing in establishes who you are; it does not put a
 * trading key in your browser.
 */
export function WalletModal({ session, onSession, onClose }: {
  session: DeskSession | null;
  onSession: (s: DeskSession | null) => void;
  onClose: () => void;
}) {
  const [step, setStep] = useState<"intro" | "choose">("intro");
  const [wallets, setWallets] = useState<DiscoveredWallet[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  // `scan` lets the user re-run discovery without closing the dialog: the
  // common case for "my wallet isn't listed" is an extension that was
  // installed or unlocked after this page loaded.
  const [scan, setScan] = useState(0);
  useEffect(() => discoverWallets(setWallets), [scan]);

  const q = query.trim().toLowerCase();
  const shown = wallets.filter(w => w.name.toLowerCase().includes(q));

  // Catalogue entries with no matching announcement. Matched on `rdns` first —
  // the id a wallet announces itself under — falling back to the display name,
  // because the legacy `window.ethereum` path has no rdns to compare.
  const detected = new Set(wallets.flatMap(w =>
    [w.rdns?.toLowerCase(), w.name.toLowerCase()].filter(Boolean) as string[]));
  const missing = WALLET_CATALOG
    .filter(c => !detected.has(c.rdns.toLowerCase()) && !detected.has(c.name.toLowerCase()))
    .filter(c => c.name.toLowerCase().includes(q));

  const copy = (label: string, text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => { setCopied(label); setTimeout(() => setCopied(null), 1500); },
      () => setError("Clipboard unavailable"));
  };

  const signInWith = async (w: DiscoveredWallet) => {
    // Guard against a second click landing while one attempt is still open.
    // Firing `eth_requestAccounts` twice is itself what puts a wallet into the
    // pending state this dialog then has to explain.
    if (busy) return;
    setBusy(w.uuid); setError(null);
    try {
      const address = await connectWallet(w.provider);
      const chainId = (await currentChainId(w.provider)) ?? 1;
      const { nonce, statement } = await liveProviders.authNonce();
      const message = siweMessage({
        domain: window.location.host, address, uri: window.location.origin,
        chainId, nonce, issuedAt: new Date().toISOString(), statement,
      });
      const signature = await signMessage(address, message, w.provider);
      // The server recovers the address from the signature; it is never taken
      // from the browser's word for it. That recovered value is what the
      // blotter records, so the actor on a run is tied to the key that signed.
      const verified = await liveProviders.authVerify(message, signature);
      onSession({
        ...sessionFromSignature(verified.address, signature, chainId),
        expiresAt: verified.expiresMs,
      });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const signOut = async () => {
    await liveProviders.authLogout().catch(() => {});
    onSession(null); onClose();
  };

  /* ------------------------------------------------------- signed in */
  if (session) {
    return <Modal title="Wallet" onClose={onClose} footer={
      <>
        <span className="muted small">Session is an httpOnly cookie; log out clears it server-side.</span>
        <button className="mini" onClick={signOut}>Log out</button>
      </>
    }>
      <div className="wallet-head">
        <div className="avatar" aria-hidden />
        <div>
          <b className="mono">{shortAddress(session.actor, 6, 4)}</b>
          <small>Signed in by signature</small>
        </div>
      </div>

      <div className="bracket-head">Addresses</div>
      <div className="addr-row">
        <div><span>Ethereum Address</span><b className="mono">{shortAddress(session.actor, 10, 8)}</b></div>
        <button className="mini" onClick={() => copy("eth", session.actor)}>
          {copied === "eth" ? "✓" : "Copy"}</button>
      </div>
      {session.chainId !== undefined &&
        <div className="kv"><span>Chain ID</span><b className="mono">{session.chainId}</b></div>}
      {session.expiresAt &&
        <div className="kv"><span>Session expires</span>
          <b>{new Date(session.expiresAt).toLocaleString()}</b></div>}

      <div className="notice warn">
        No trading key exists in this browser. A venue like Paradex would derive
        an L2 signing account from this signature; here the signing material
        lives only in an execution gateway that is never publicly reachable, so
        there is no private key to export and nothing here can move funds.
      </div>
    </Modal>;
  }

  /* --------------------------------------------------- choose a wallet */
  if (step === "choose") {
    return <Modal title="Select your wallet" onClose={onClose} footer={
      <>
        <button className="mini" onClick={() => setStep("intro")}>‹ Back</button>
        <span className="muted small">{wallets.length} detected · {WALLET_CATALOG.length} supported</span>
      </>
    }>
      <p className="modal-lede">Connect a wallet to your QuantGenLabs desk.</p>

      {/* Counts the whole field, not just what is installed — the list is
          always at least the catalogue, so gating on detections alone would
          hide the search on the exact browser that needs it most. */}
      {wallets.length + WALLET_CATALOG.length > 4 && <input
        className="wallet-search" type="search" placeholder="Search wallets"
        value={query} onChange={e => setQuery(e.target.value)}
        aria-label="Search wallets" />}

      <div className="wallet-list">
        {shown.length === 0 && missing.length === 0
          ? <div className="empty">No wallet matches “{query}”.</div>
          : shown.map(w => <button key={w.uuid}
              className={`wallet-row ${busy === w.uuid ? "connecting" : ""}`}
              // Only the row being attempted is disabled. Greying out every
              // other wallet during a hung attempt is what left the dialog
              // with nothing clickable and no way back.
              disabled={busy === w.uuid} onClick={() => signInWith(w)}>
              {w.icon ? <img src={w.icon} alt="" width={26} height={26} />
                : <span className="wallet-glyph" aria-hidden />}
              <b>{w.name}</b>
              <span>{busy === w.uuid ? "Check your wallet…" : "›"}</span>
            </button>)}

        {/* The rest of the field, marked as absent rather than hidden. A list
            showing only what happens to be installed reads as the desk's
            supported set — this says the choice is the reader's, and links
            each one rather than leaving them to search for it. */}
        {missing.length > 0 && <>
          <div className="bracket-head">Not installed</div>
          {missing.map(c => <a key={c.rdns} className="wallet-row absent"
            href={c.install} target="_blank" rel="noopener noreferrer">
            <span className="wallet-glyph dim" aria-hidden />
            <b>{c.name}</b>
            <span>Install ↗</span>
          </a>)}
        </>}
      </div>

      {/* An explicit way out of a waiting state. The wallet prompt lives in
          another window this page cannot close, so without this the only
          escape from a prompt the user has already dismissed is a reload. */}
      {busy && <div className="rescan">
        <span className="muted small">Waiting for the wallet…</span>
        <button className="mini" onClick={() => { setBusy(null); setError(null); }}>
          Cancel
        </button>
      </div>}

      <div className="rescan">
        <span>Wallet not listed?</span>
        <button className="mini" onClick={() => setScan(n => n + 1)}>Scan again</button>
      </div>
      <div className="notice warn">
        Only wallets that are <b>installed and set up</b> announce themselves.
        A freshly installed extension with no wallet created yet often stays
        silent — finish its setup, then scan again.
      </div>

      {error && <div className="notice reject">{error}</div>}
    </Modal>;
  }

  /* ----------------------------------------------------------- intro */
  return <Modal title="Connect your Wallet" onClose={onClose}>
    <p className="modal-lede">
      By connecting a wallet you agree that this is a <b>demonstration desk</b>.
      It executes against a local simulator, holds no custody, and can neither
      move funds nor reach an exchange.
    </p>

    <button className="primary wide" onClick={() => setStep("choose")}>
      Choose Wallet
    </button>

    <button className="route-card disabled" disabled>
      <div><b>Email or Social</b>
        <small>Not offered — that needs a custodial embedded-wallet provider, and this host holds no custody</small></div>
      <span>›</span>
    </button>
    <button className="route-card disabled" disabled>
      <div><b>Starknet Wallet</b>
        <small>Not offered — L2 order signing lives in the execution gateway, not the browser</small></div>
      <span>›</span>
    </button>

    <div className="notice ok">
      Reading the desk is open to everyone — the book, the blotter, TCA and the
      risk engines need no account. Signing in unlocks the one endpoint that
      <b> writes</b>: submitting an order. The server issues the nonce, recovers
      your address from the signature, and records it against every run you place.
    </div>
    {error && <div className="notice reject">{error}</div>}
  </Modal>;
}
