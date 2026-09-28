import type { DeskSession } from "../types/domain";

/**
 * Wallet-signature sign-in (EIP-4361, "Sign-In With Ethereum").
 *
 * This is how a perps venue actually authenticates: no password, no emailed
 * token — the account *is* an address, and proving control of it means signing
 * a challenge with its key. The signature is produced by the user's wallet
 * (`personal_sign` over an EIP-4361 message); this file never sees a private
 * key and could not use one if it did.
 *
 * **Where this stops short of Paradex.** Paradex takes the L1 signature and
 * derives a Starknet L2 account from it, then signs every *order* with that L2
 * key (SNIP-12 typed data). That second half is deliberately not here: this
 * host has no order-submission endpoint and holds no signing material on any
 * public path — see TIER1_ARCHITECTURE §1 and §4, where the key-holding
 * process is explicitly not the public-facing one. So sign-in proves who you
 * are; it does not turn the browser into an execution gateway.
 */

/** Minimal EIP-1193 surface. Typed here rather than pulling in a wallet SDK. */
interface Eip1193 {
  request(args: { method: string; params?: unknown[] }): Promise<any>;
  on?(event: string, handler: (...args: any[]) => void): void;
  removeListener?(event: string, handler: (...args: any[]) => void): void;
  isMetaMask?: boolean;
}

declare global {
  interface Window { ethereum?: Eip1193 }
}

export const hasWallet = (): boolean => typeof window !== "undefined" && !!window.ethereum;

/* ------------------------------------------------- multi-wallet discovery */

/**
 * Wallets announced under EIP-6963.
 *
 * `window.ethereum` is a single slot, so when a browser has MetaMask *and*
 * Coinbase Wallet *and* Rabby installed they fight over it and the loser is
 * unreachable — the user picks a wallet in our UI and a different one opens.
 * EIP-6963 fixes that by having each wallet announce itself with its own
 * provider object, which is what a venue's "Select your wallet" list is
 * actually enumerating.
 */
export interface DiscoveredWallet {
  uuid: string; name: string; icon: string; provider: Eip1193;
  /** EIP-6963 reverse-DNS id, used to match a detection to its catalogue row. */
  rdns?: string;
}

/**
 * Wallets we name even when they are not installed.
 *
 * EIP-6963 only announces extensions that are **present**, so a browser with
 * one wallet shows a list of one — which reads as "this desk supports Coinbase
 * Wallet" rather than "you have one wallet installed". Venues avoid that by
 * listing the field and marking what is missing; the user learns the choice is
 * theirs, not the site's.
 *
 * `rdns` is the EIP-6963 reverse-DNS id each wallet announces itself under. It
 * is what lets a detected wallet be matched to its catalogue entry rather than
 * guessed at by display name, which varies ("MetaMask" vs "MetaMask Wallet").
 */
export interface CatalogWallet {
  rdns: string; name: string; install: string;
}

export const WALLET_CATALOG: CatalogWallet[] = [
  { rdns: "io.metamask",        name: "MetaMask",        install: "https://metamask.io/download/" },
  { rdns: "io.rabby",           name: "Rabby",           install: "https://rabby.io/" },
  { rdns: "com.okex.wallet",    name: "OKX Wallet",      install: "https://www.okx.com/web3" },
  { rdns: "com.coinbase.wallet",name: "Coinbase Wallet", install: "https://www.coinbase.com/wallet/downloads" },
  { rdns: "com.brave.wallet",   name: "Brave Wallet",    install: "https://brave.com/wallet/" },
  { rdns: "com.trustwallet.app",name: "Trust Wallet",    install: "https://trustwallet.com/download" },
  { rdns: "app.phantom",        name: "Phantom",         install: "https://phantom.app/download" },
];

interface Eip6963Detail { info: { uuid: string; name: string; icon: string; rdns: string }; provider: Eip1193 }

/**
 * Listen for wallet announcements and request a fresh round.
 *
 * Discovery is push-based and racy by design: wallets announce on page load,
 * and a listener attached afterwards hears nothing until it asks. So we
 * subscribe *first*, then dispatch the request — the reverse order misses
 * every wallet that answers immediately.
 */
export function discoverWallets(onChange: (wallets: DiscoveredWallet[]) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const found = new Map<string, DiscoveredWallet>();

  const handler = (event: Event) => {
    const detail = (event as CustomEvent<Eip6963Detail>).detail;
    if (!detail?.info?.uuid || found.has(detail.info.uuid)) return;
    found.set(detail.info.uuid, {
      uuid: detail.info.uuid, name: detail.info.name,
      icon: detail.info.icon, provider: detail.provider,
      rdns: detail.info.rdns,
    });
    onChange([...found.values()]);
  };

  window.addEventListener("eip6963:announceProvider", handler);

  // Re-ask a few times rather than once. Extensions inject on their own
  // schedule, and a wallet that is still initialising when the dialog opens
  // hears the first request and has nothing to say yet. A single shot at mount
  // silently under-reports exactly the wallet the user is waiting for.
  const request = () => window.dispatchEvent(new Event("eip6963:requestProvider"));
  request();
  const retries = [150, 500, 1200, 2500].map(ms => window.setTimeout(request, ms));

  // Fallback for wallets predating EIP-6963: if nothing announced, the legacy
  // single slot is still a usable wallet and hiding it strands older builds.
  const fallback = window.setTimeout(() => {
    if (found.size === 0 && window.ethereum) {
      found.set("legacy", {
        uuid: "legacy",
        name: window.ethereum.isMetaMask ? "MetaMask" : "Browser Wallet",
        icon: "", provider: window.ethereum,
      });
      onChange([...found.values()]);
    }
  }, 800);

  return () => {
    window.removeEventListener("eip6963:announceProvider", handler);
    retries.forEach(window.clearTimeout);
    window.clearTimeout(fallback);
  };
}

/** `0x1adA…f0b4` — the form every venue uses, because 42 hex chars is not readable. */
export const shortAddress = (a: string, lead = 6, tail = 4): string =>
  a.length <= lead + tail ? a : `${a.slice(0, lead)}…${a.slice(-tail)}`;

export class WalletError extends Error {}

/** EIP-1193 standard error codes we treat specially. */
const USER_REJECTED = 4001;
//: "Already processing eth_requestAccounts" — a prior prompt is still open.
const ALREADY_PENDING = -32002;

/**
 * Bound a wallet call in time.
 *
 * **This exists because of a real hang, not as belt-and-braces.** Cancel a
 * Coinbase Wallet connect prompt and the extension keeps the permission
 * request pending; the next `eth_requestAccounts` then never settles — it does
 * not resolve and it does not reject. An `await` on that promise waits
 * forever, so a `finally` that clears the spinner never runs and the button
 * sits on "Check your wallet…" with no error and no way forward. The user sees
 * a dead dialog.
 *
 * The timeout is deliberately generous: approving a prompt legitimately takes
 * as long as it takes to find the window and read it. This is here to convert
 * *infinity* into an actionable message, not to rush anyone.
 */
function withTimeout<T>(p: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: number;
  return Promise.race([
    p,
    new Promise<never>((_, reject) => {
      timer = window.setTimeout(() => reject(new WalletError(message)), ms);
    }),
  ]).finally(() => window.clearTimeout(timer)) as Promise<T>;
}

const PENDING_HELP =
  "Your wallet already has a connection request open. Open the extension, " +
  "approve or dismiss the pending prompt, then try again.";

/**
 * Ask the wallet to expose accounts.
 *
 * Tries the silent path first. `eth_accounts` never prompts — it reports what
 * this origin is already authorised for. When someone cancels and comes back,
 * they have often already granted access on an earlier visit, and re-prompting
 * a wallet that has nothing to ask is how the pending-request state gets
 * created in the first place.
 */
export async function connectWallet(provider?: Eip1193): Promise<string> {
  const eth = provider ?? window.ethereum;
  if (!eth) throw new WalletError("No wallet detected. Install MetaMask or another EIP-1193 wallet.");

  try {
    const existing: string[] = await withTimeout(
      eth.request({ method: "eth_accounts" }), 5_000, "Wallet did not respond.");
    if (existing?.length) return existing[0];
  } catch {
    // A wallet that will not answer the silent query might still answer the
    // prompting one, so this is not fatal — fall through and ask properly.
  }

  let accounts: string[];
  try {
    accounts = await withTimeout(
      eth.request({ method: "eth_requestAccounts" }), 120_000,
      "The wallet never answered. Its prompt may be waiting in another window, " +
      "or a previous request is still open — check the extension and try again.");
  } catch (e: any) {
    if (e instanceof WalletError) throw e;
    // 4001 is the standard "user rejected" code. Saying so is better than
    // reporting a failure the user deliberately caused.
    if (e?.code === USER_REJECTED) throw new WalletError("Connection rejected in wallet.");
    if (e?.code === ALREADY_PENDING) throw new WalletError(PENDING_HELP);
    // Some wallets report the pending state only in the message text.
    if (/already pending|already processing/i.test(e?.message || "")) {
      throw new WalletError(PENDING_HELP);
    }
    throw new WalletError(e?.message || String(e));
  }
  if (!accounts?.length) throw new WalletError("Wallet returned no accounts.");
  return accounts[0];
}

export async function currentChainId(provider?: Eip1193): Promise<number | null> {
  const eth = provider ?? window.ethereum;
  if (!eth) return null;
  try { return parseInt(await eth.request({ method: "eth_chainId" }), 16); }
  catch { return null; }
}

/**
 * Build an EIP-4361 message.
 *
 * The format is fixed by the standard, and the fixed parts are what make it
 * safe: the domain binds the signature to this origin, and the nonce binds it
 * to one session so a captured signature cannot be replayed. A bespoke
 * "sign this to log in" string has neither property.
 */
export function siweMessage(o: {
  domain: string; address: string; uri: string; chainId: number;
  nonce: string; issuedAt: string; statement: string;
}): string {
  return [
    `${o.domain} wants you to sign in with your Ethereum account:`,
    o.address,
    "",
    o.statement,
    "",
    `URI: ${o.uri}`,
    "Version: 1",
    `Chain ID: ${o.chainId}`,
    `Nonce: ${o.nonce}`,
    `Issued At: ${o.issuedAt}`,
  ].join("\n");
}

export async function signMessage(address: string, message: string,
                                  provider?: Eip1193): Promise<string> {
  const eth = provider ?? window.ethereum;
  if (!eth) throw new WalletError("No wallet detected.");
  try {
    // personal_sign takes (message, address) in that order — reversed from
    // eth_sign, and a silent source of "invalid signature" if transposed.
    // Bounded for the same reason connect is: a dismissed signing prompt can
    // leave the next one with nothing to resolve it.
    return await withTimeout(
      eth.request({ method: "personal_sign", params: [message, address] }),
      120_000,
      "The wallet never returned a signature. Check for a prompt waiting in " +
      "the extension, then try again.");
  } catch (e: any) {
    if (e instanceof WalletError) throw e;
    if (e?.code === USER_REJECTED) throw new WalletError("Signature rejected in wallet.");
    if (e?.code === ALREADY_PENDING || /already pending|already processing/i.test(e?.message || "")) {
      throw new WalletError(PENDING_HELP);
    }
    throw new WalletError(e?.message || String(e));
  }
}

/** Subscribe to account switches. Returns an unsubscribe function. */
export function onAccountsChanged(handler: (accounts: string[]) => void): () => void {
  const eth = window.ethereum;
  if (!eth?.on) return () => {};
  eth.on("accountsChanged", handler);
  return () => eth.removeListener?.("accountsChanged", handler);
}

/** A signed-in session, shaped like the rest of the desk's session model. */
export function sessionFromSignature(address: string, signature: string, chainId: number): DeskSession {
  return {
    token: signature,
    actor: address,
    expiresAt: null,
    address,
    chainId,
    method: "SIWE",
  };
}
