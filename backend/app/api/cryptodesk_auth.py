"""Wallet-signature authentication for the crypto desk.

**Why this exists.** The desk is a public host with an endpoint that writes:
`/lifecycle` executes a parent order and persists it. Reads can stay open — a
blotter nobody can see proves nothing — but a write path open to the whole
internet fills the run tables with strangers' orders and makes the audit trail
worthless. TIER1_ARCHITECTURE §2 states the rule: every write needs an actor,
or the audit trail is decoration.

**Why a signature rather than a password.** On a perps venue the account *is*
an address. Proving control of it means signing a challenge with its key, and
that is a stronger claim than any password: nothing secret crosses the wire,
there is no credential for this host to store, and there is no reset flow to
attack. The flow is EIP-4361 ("Sign-In With Ethereum"):

1. `POST /auth/nonce` — the server mints a single-use nonce and stores it with
   an expiry.
2. The browser builds the EIP-4361 message and the wallet signs it.
3. `POST /auth/verify` — the server recovers the signing address from the
   signature, checks the nonce is one it issued and has not seen used, burns
   it, and returns a session token.

**The nonce must be server-issued.** A nonce the browser invents proves
freshness to nobody: an attacker replaying a captured signature would simply
replay its nonce too. Issuing and burning it here is what makes the signature
good exactly once. The existing `replay` table is reused for this — it is the
same problem it already solves for order ids, with the same expiry sweep.

**Recovery is delegated to `eth-account`.** `cryptodesk/vault/signers.py` sets
this policy and the reasoning holds: hand-rolling keccak or secp256k1 to save
a dependency makes every signature a bug candidate, and a signature bug is not
a crash, it is a silent rejection you debug for a day. The import fails closed
with a named error rather than degrading into an endpoint that accepts
anything.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/cryptodesk/auth", tags=["crypto-desk-auth"])

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    SIG_AVAILABLE = True
    SIG_ERROR = None
except Exception as exc:                                    # pragma: no cover
    SIG_AVAILABLE = False
    SIG_ERROR = "%s: %s" % (type(exc).__name__, exc)

#: Nonces are short-lived: the window between "start sign-in" and "wallet
#: returns a signature" is seconds, and a long window is just a larger replay
#: surface.
NONCE_TTL_S = 300
SESSION_TTL_S = int(os.environ.get("CRYPTODESK_SESSION_TTL_S", "43200"))  # 12h

#: Signing key for session tokens. Ephemeral by default, which means a restart
#: invalidates every session — correct for a demo host, and the alternative
#: (a hardcoded default) is a forgeable token on every deployment that forgets
#: to override it.
_SECRET = os.environ.get("CRYPTODESK_SESSION_SECRET") or secrets.token_hex(32)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

SESSION_COOKIE = "cryptodesk_session"
CSRF_COOKIE = "cryptodesk_csrf"
CSRF_HEADER = "X-CSRF-Token"

#: Off by default so sign-in works over plain http on localhost. Set
#: CRYPTODESK_COOKIE_SECURE=1 anywhere the console is served over TLS — which
#: is anywhere it is reachable by someone other than the developer.
COOKIE_SECURE = os.environ.get("CRYPTODESK_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def _store():
    from .cryptodesk import _store as store
    return store()


def _require_sig() -> None:
    if not SIG_AVAILABLE:
        raise HTTPException(
            503, "signature verification unavailable: %s. Install eth-account "
                 "to enable wallet sign-in." % SIG_ERROR)


# ---------------------------------------------------------------- session ---

def _sign_session(address: str, expires_ms: int) -> str:
    """`address.expiry.hmac` — a signed bearer token, not an encrypted one.

    Nothing in it is secret; the point is that the *server* issued it. HMAC
    over the payload is what stops a caller editing the address and promoting
    themselves, and `compare_digest` is what stops the comparison leaking the
    expected value a byte at a time.
    """
    # The EIP-55 checksummed form is kept, not lowercased. That capitalisation
    # is a checksum: it is what makes a mistyped address fail loudly instead of
    # resolving to a different, valid-looking account. Discarding it to
    # "canonicalise" throws away the guard and leaves the blotter recording an
    # actor that no longer string-matches the wallet that signed.
    payload = "%s.%d" % (address, expires_ms)
    mac = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return "%s.%s" % (payload, mac)


def _verify_session(token: str) -> Optional[str]:
    """Return the actor address for a valid token, else None."""
    try:
        address, expiry, mac = token.rsplit(".", 2)
    except ValueError:
        return None
    expected = hmac.new(_SECRET.encode(), ("%s.%s" % (address, expiry)).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        if int(expiry) < int(time.time() * 1000):
            return None
    except ValueError:
        return None
    return address


# -------------------------------------------------------------- endpoints ---

class NonceResponse(BaseModel):
    nonce: str
    expires_ms: int
    statement: str


@router.post("/nonce", response_model=NonceResponse)
def issue_nonce() -> dict:
    """Mint a single-use nonce and remember it until it expires."""
    _require_sig()
    nonce = secrets.token_hex(16)
    expires_ms = int((time.time() + NONCE_TTL_S) * 1000)
    try:
        _store().save_replay(None, "siwe:%s" % nonce, expires_ms)
    except Exception as exc:
        raise HTTPException(503, "cannot persist nonce: %s: %s"
                            % (type(exc).__name__, exc))
    return {
        "nonce": nonce, "expires_ms": expires_ms,
        "statement": ("Sign in to the QuantGenLabs crypto desk. This signature "
                      "proves address control. It authorises no transfer and "
                      "no trade."),
    }


class VerifyRequest(BaseModel):
    message: str
    signature: str


class VerifyResponse(BaseModel):
    address: str
    csrf_token: str
    expires_ms: int


@router.post("/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest, response: Response) -> dict:
    """Recover the signer, burn the nonce, and open a session.

    The session lands in an **httpOnly cookie**, not in a body the browser
    stores. That is the shape TIER1_ARCHITECTURE §2 asks for and the reason is
    concrete: a token in `localStorage` or `sessionStorage` is readable by any
    script that reaches the page, so one XSS exfiltrates a trading session. An
    httpOnly cookie is not readable by script at all.

    The cost of cookies is that the browser attaches them automatically, which
    is what CSRF exploits. So a second, *non*-httpOnly token is returned for
    the client to echo in a header on every mutating call. An attacker's page
    can cause the cookie to be sent but cannot read the CSRF value to match it.
    """
    _require_sig()

    # The nonce is read back out of the message the user actually signed, not
    # taken as a separate field. Trusting a caller-supplied nonce alongside a
    # message would let them sign one challenge and present another.
    match = re.search(r"^Nonce: ([0-9a-fA-F]{8,})$", req.message, re.MULTILINE)
    if not match:
        raise HTTPException(400, "message carries no EIP-4361 Nonce line")
    nonce = match.group(1)

    st = _store()
    now_ms = int(time.time() * 1000)
    try:
        _, seen = st.load_replay(now_ms)
    except Exception as exc:
        raise HTTPException(503, "replay store unavailable: %s: %s"
                            % (type(exc).__name__, exc))
    if ("siwe:%s" % nonce) not in seen:
        # Covers all three failures that matter and are indistinguishable to a
        # caller: never issued, already used, or expired and swept.
        raise HTTPException(401, "unknown, used or expired nonce")

    try:
        recovered = Account.recover_message(
            encode_defunct(text=req.message), signature=req.signature)
    except Exception as exc:
        raise HTTPException(401, "bad signature: %s" % type(exc).__name__)
    if not _ADDRESS_RE.match(recovered or ""):
        raise HTTPException(401, "signature recovered no address")

    # Burn it. Expiring it in the past is a delete that the existing sweep
    # performs, so this needs no new table operation.
    try:
        st.save_replay(None, "siwe:%s" % nonce, now_ms - 1)
    except Exception:
        pass

    expires_ms = int((time.time() + SESSION_TTL_S) * 1000)
    session = _sign_session(recovered, expires_ms)
    csrf = secrets.token_urlsafe(24)

    response.set_cookie(
        SESSION_COOKIE, session,
        max_age=SESSION_TTL_S, httponly=True, samesite="lax",
        # Secure is conditional: a hardcoded True makes sign-in silently fail
        # over plain http on localhost, which is where this is developed.
        secure=COOKIE_SECURE, path="/api/cryptodesk",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf,
        max_age=SESSION_TTL_S, httponly=False, samesite="lax",
        secure=COOKIE_SECURE, path="/api/cryptodesk",
    )
    return {"address": recovered, "csrf_token": csrf, "expires_ms": expires_ms}


@router.post("/logout")
def logout(response: Response) -> dict:
    """Clear the session. Deleting the cookie is the whole of it — there is no
    server-side row to revoke, because the token carries its own expiry."""
    response.delete_cookie(SESSION_COOKIE, path="/api/cryptodesk")
    response.delete_cookie(CSRF_COOKIE, path="/api/cryptodesk")
    return {"ok": True}


@router.get("/session")
def session(cryptodesk_session: Optional[str] = Cookie(None),
            authorization: Optional[str] = Header(None)) -> dict:
    """Who the caller is, if anyone. Never 401s — absence is a valid answer.

    This is how the console knows it is signed in: the session cookie is
    httpOnly, so the page cannot read it and must ask.
    """
    actor = actor_or_none(cryptodesk_session, authorization)
    return {"authenticated": actor is not None, "address": actor,
            "signature_backend": "eth-account" if SIG_AVAILABLE else None,
            "csrf_header": CSRF_HEADER}


# ------------------------------------------------------------ dependencies ---

def actor_or_none(session_cookie: Optional[str] = None,
                  authorization: Optional[str] = None) -> Optional[str]:
    """Resolve the caller, cookie first.

    The bearer header is still accepted because a CLI or a test has no cookie
    jar and no CSRF exposure — there is no ambient credential for a third-party
    page to abuse. Browsers get the cookie path, which is the one that needs
    the CSRF check below.
    """
    if session_cookie:
        actor = _verify_session(session_cookie)
        if actor:
            return actor
    if authorization and authorization.lower().startswith("bearer "):
        return _verify_session(authorization[7:].strip())
    return None


def require_actor(
    cryptodesk_session: Optional[str] = Cookie(None),
    cryptodesk_csrf: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
    x_csrf_token: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency for routes that write.

    Reads stay open deliberately — the blotter is the evidence, and hiding it
    would defeat the point of publishing the desk. Writes do not: an order
    recorded against nobody is a row, not an audit entry.

    Two checks, not one. The session proves *who*; the CSRF token proves the
    request came from the console rather than from a page that merely knows
    the browser will attach the cookie. Double-submit: the value must appear in
    both a readable cookie and a header, and only same-origin script can read
    the cookie to set the header.
    """
    actor = actor_or_none(cryptodesk_session, authorization)
    if actor is None:
        raise HTTPException(
            401, "sign in with a wallet signature to submit an order. "
                 "Reads are open; writes need an actor.")

    # Only enforced on the cookie path. A bearer caller supplied the credential
    # explicitly, so there is nothing ambient to forge.
    if cryptodesk_session and _verify_session(cryptodesk_session) == actor:
        if not x_csrf_token or not cryptodesk_csrf or \
                not hmac.compare_digest(x_csrf_token, cryptodesk_csrf):
            raise HTTPException(
                403, "missing or mismatched %s header" % CSRF_HEADER)
    return actor
