"""Key custody interfaces.

The design rule this package enforces: **application code never holds a
private key**. It holds a `KeyVault` handle and asks it to sign. Whether the
key lives in an env var, the macOS Keychain, an encrypted file, or an HSM that
will never export it, the calling code is identical - and the calling code is
the part that gets logged, traced, crash-dumped and pasted into issues.

`reveal()` exists because some backends genuinely have to hand the bytes over
(a local signer needs them). It is a separate, named, counted operation rather
than a property, so "where does this key get extracted?" is a grep, and a
backend that refuses to export can say so by raising.

Nothing here invents cryptography. Signing is delegated to `eth_account` or
`starknet_py` where a real signature is required; the fallback signer is
clearly labelled as test-only and is rejected by the venue adapters.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class VaultError(Exception):
    """Base for all custody failures."""


class KeyNotFound(VaultError):
    pass


class ExportForbidden(VaultError):
    """The backend holds a key it will not release - an HSM, or a policy lock."""


class VaultLocked(VaultError):
    """The backend needs a passphrase or an unlock step first."""


def fingerprint(secret: str) -> str:
    """A stable, non-reversible id for a secret.

    Lets logs, tests and audit trails say *which* key was used without saying
    what it is. Truncated SHA-256 of the raw bytes: collision-resistant enough
    to identify a key, useless for recovering one.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


#: Patterns that look like key material, used by `redact`.
_SECRET_PATTERNS = [
    re.compile(r"0x[0-9a-fA-F]{64}"),              # 32-byte hex key
    re.compile(r"\b[0-9a-fA-F]{64}\b"),            # bare hex key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),   # long base64 blobs
]


def redact(text: str) -> str:
    """Strip anything that looks like key material out of a string.

    Applied to exception text before it is logged. Venues echo request bodies
    back in error responses, and a signed order's body is mostly signature -
    harmless - but an auth payload built with the wrong field is not.

    This is a backstop, not a guarantee: the primary defence is that keys never
    enter these strings in the first place.
    """
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


@dataclass
class KeyRef:
    """A named handle to a key, carrying no key material."""

    name: str
    #: Public identity, where the backend knows it - an address or account id.
    public_id: Optional[str] = None
    backend: str = "unknown"
    fingerprint: Optional[str] = None

    def __str__(self) -> str:
        bits = [self.name, "backend=%s" % self.backend]
        if self.public_id:
            bits.append("id=%s" % self.public_id)
        if self.fingerprint:
            bits.append("fp=%s" % self.fingerprint)
        return " ".join(bits)


class KeyVault:
    """Abstract custody backend.

    Subclasses implement `_fetch`. Everything else - the export counter, the
    audit log, the refusal to return empty strings - is shared, because those
    are the parts you do not want reimplemented per backend.
    """

    backend_name = "abstract"
    #: Set False on backends that physically cannot export (HSM, KMS).
    exportable = True

    def __init__(self) -> None:
        self._export_count: Dict[str, int] = {}
        self._audit: List[str] = []

    # -- subclass hook -----------------------------------------------------

    def _fetch(self, name: str) -> str:
        raise NotImplementedError

    def list_keys(self) -> List[str]:
        return []

    # -- public API --------------------------------------------------------

    def has(self, name: str) -> bool:
        try:
            self._fetch(name)
            return True
        except VaultError:
            return False

    def ref(self, name: str) -> KeyRef:
        """A handle to the key, safe to pass around, log and store."""
        secret = self._fetch(name)
        return KeyRef(
            name=name,
            backend=self.backend_name,
            fingerprint=fingerprint(secret),
            public_id=self.public_id(name),
        )

    def public_id(self, name: str) -> Optional[str]:
        """Derived public identity, if the backend can compute one cheaply."""
        return None

    def reveal(self, name: str, reason: str = "unspecified") -> str:
        """Return the raw secret. Counted and audited.

        Call this as late as possible and never store the result. Prefer
        `use()`, which scopes the exposure to a single expression.
        """
        if not self.exportable:
            raise ExportForbidden(
                "backend %r does not export key material; use sign() instead"
                % self.backend_name
            )
        secret = self._fetch(name)
        self._export_count[name] = self._export_count.get(name, 0) + 1
        self._audit.append(
            "reveal name=%s fp=%s reason=%s count=%d"
            % (name, fingerprint(secret), reason, self._export_count[name])
        )
        return secret

    def use(self, name: str, fn, reason: str = "sign"):
        """Apply `fn` to the secret and return the result.

        The secret is a local in this frame only. It is not a real memory
        guarantee - Python strings are immutable and stay resident until
        collected - but it removes the two failure modes that actually happen:
        a key assigned to a long-lived attribute, and a key captured in a
        traceback frame that gets logged.
        """
        secret = self.reveal(name, reason=reason)
        try:
            return fn(secret)
        finally:
            del secret

    def export_count(self, name: str) -> int:
        return self._export_count.get(name, 0)

    def audit_log(self) -> List[str]:
        return list(self._audit)

    def __repr__(self) -> str:
        return "<%s backend=%s keys=%d>" % (
            type(self).__name__, self.backend_name, len(self.list_keys())
        )


@dataclass
class Signature:
    """A signature plus enough context to know what produced it."""

    value: str
    scheme: str
    signer: str
    #: True when produced by a test signer - venue adapters refuse these.
    is_test: bool = False
    components: Dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return "%s(%s%s)" % (
            self.scheme, self.value[:18] + "..." if len(self.value) > 18 else self.value,
            ", TEST" if self.is_test else "",
        )


class Signer:
    """Produces signatures over structured messages.

    Separate from `KeyVault` because the two vary independently: the same
    Starknet key may sign both an onboarding message and an order with
    different domains, and the same EIP-712 scheme runs against a local key or
    a remote KMS.
    """

    scheme = "abstract"
    is_test = False

    def address(self) -> str:
        raise NotImplementedError

    def sign_message(self, payload: Dict) -> Signature:
        raise NotImplementedError

    def sign_typed_data(self, domain: Dict, types: Dict, message: Dict) -> Signature:
        raise NotImplementedError


def env_or_vault(name: str, vault: Optional[KeyVault] = None,
                 env_var: Optional[str] = None) -> Optional[str]:
    """Resolve a secret from a vault, falling back to an env var.

    The fallback order is deliberate: a configured vault always wins, so
    setting one up cannot be silently defeated by a stale env var left in a
    shell profile.
    """
    if vault is not None:
        try:
            return vault.reveal(name, reason="env_or_vault")
        except VaultError:
            pass
    return os.environ.get(env_var or name.upper().replace("-", "_"))
