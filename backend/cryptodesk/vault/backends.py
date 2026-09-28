"""Concrete key-custody backends, weakest to strongest.

`MemoryVault`      - tests only. Dies with the process.
`EnvVault`         - reads the environment. Convenient, and the easiest thing
                     to leak: env vars are inherited by every child process and
                     land in crash reports and CI logs.
`MacKeychainVault` - the OS keychain, via the `security` CLI. No dependencies,
                     encrypted at rest, access-controlled by the OS.
`EncryptedFileVault` - scrypt + AES-GCM file. Portable across machines.
`KmsVault`         - non-exporting backend contract for a real HSM/KMS.

`ChainVault` tries several in order, which is how you move from an env var in
development to a keychain in production without touching calling code.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .base import (
    ExportForbidden, KeyNotFound, KeyVault, VaultError, VaultLocked, fingerprint,
)

DEFAULT_SERVICE = "cryptodesk"


class MemoryVault(KeyVault):
    """In-process dict. For tests and nothing else."""

    backend_name = "memory"

    def __init__(self, secrets: Optional[Dict[str, str]] = None):
        super().__init__()
        self._secrets: Dict[str, str] = dict(secrets or {})

    def _fetch(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError:
            raise KeyNotFound("no key %r in memory vault" % name)

    def put(self, name: str, secret: str) -> None:
        self._secrets[name] = secret

    def list_keys(self) -> List[str]:
        return sorted(self._secrets)


class EnvVault(KeyVault):
    """Environment variables, with a configurable prefix.

    `paradex-l2` resolves to `CRYPTODESK_PARADEX_L2`. Fine for a throwaway
    testnet key; do not put anything that moves real value here.
    """

    backend_name = "env"

    def __init__(self, prefix: str = "CRYPTODESK_"):
        super().__init__()
        self.prefix = prefix

    def _var(self, name: str) -> str:
        return self.prefix + name.upper().replace("-", "_").replace(".", "_")

    def _fetch(self, name: str) -> str:
        value = os.environ.get(self._var(name))
        if not value:
            raise KeyNotFound("environment variable %s is unset" % self._var(name))
        return value

    def list_keys(self) -> List[str]:
        n = len(self.prefix)
        return sorted(
            k[n:].lower().replace("_", "-")
            for k in os.environ if k.startswith(self.prefix)
        )


class MacKeychainVault(KeyVault):
    """macOS Keychain via the `security` CLI.

    The best default on this machine: encrypted at rest, unlocked with the
    login session, and survives a shell restart without leaving a file around.

    One honest caveat on *writing*: `security add-generic-password -w` takes
    the secret as a command-line argument, so it is briefly visible in this
    user's own process list. On macOS other users cannot read it, but a
    same-user process can. Reading (the common path) does not have this
    problem. For keys that matter, add them through Keychain Access.app or
    `security -i` interactively and let this class read them.
    """

    backend_name = "macos-keychain"

    def __init__(self, service: str = DEFAULT_SERVICE):
        super().__init__()
        self.service = service
        # Index of stored names. Keychain has no cheap "list items for service"
        # query, and dumping the whole keychain to enumerate is both slow and
        # triggers an auth prompt.
        self._index_path = Path.home() / ".cryptodesk" / "keychain-index.json"

    @staticmethod
    def available() -> bool:
        if os.uname().sysname != "Darwin":
            return False
        try:
            subprocess.run(["security", "-h"], capture_output=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _fetch(self, name: str) -> str:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password",
                 "-a", name, "-s", self.service, "-w"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VaultError("keychain read failed: %s" % exc)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "could not be found" in err:
                raise KeyNotFound("no keychain item %r for service %r"
                                  % (name, self.service))
            if "User interaction is not allowed" in err:
                raise VaultLocked("keychain is locked; unlock it and retry")
            raise VaultError("keychain read failed: %s" % err)
        return proc.stdout.strip()

    def put(self, name: str, secret: str) -> None:
        """Store or replace an item. See the class docstring on argv exposure."""
        try:
            proc = subprocess.run(
                ["security", "add-generic-password",
                 "-a", name, "-s", self.service, "-w", secret,
                 "-U",  # update if it already exists
                 "-l", "%s: %s" % (self.service, name)],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VaultError("keychain write failed: %s" % exc)
        if proc.returncode != 0:
            raise VaultError("keychain write failed: %s" % (proc.stderr or "").strip())
        self._index_add(name)

    def delete(self, name: str) -> bool:
        proc = subprocess.run(
            ["security", "delete-generic-password", "-a", name, "-s", self.service],
            capture_output=True, text=True, timeout=15,
        )
        self._index_remove(name)
        return proc.returncode == 0

    # -- name index --------------------------------------------------------

    def _index(self) -> List[str]:
        try:
            return json.loads(self._index_path.read_text())
        except (OSError, ValueError):
            return []

    def _write_index(self, names: List[str]) -> None:
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            self._index_path.write_text(json.dumps(sorted(set(names)), indent=2))
        except OSError:
            pass  # index is a convenience; losing it costs nothing but `list_keys`

    def _index_add(self, name: str) -> None:
        self._write_index(self._index() + [name])

    def _index_remove(self, name: str) -> None:
        self._write_index([n for n in self._index() if n != name])

    def list_keys(self) -> List[str]:
        return sorted(self._index())


class EncryptedFileVault(KeyVault):
    """scrypt-derived key + AES-256-GCM, one JSON file.

    Deliberately **fails closed** when `cryptography` is not installed rather
    than falling back to something weaker. A vault that silently degrades to
    XOR is worse than no vault, because the operator still believes the key is
    protected.

    scrypt parameters follow the Ethereum keystore v3 convention (n=2^18) -
    slow enough that a stolen file is not trivially brute-forced, fast enough
    that unlocking at startup is tolerable.
    """

    backend_name = "encrypted-file"

    N, R, P = 1 << 18, 8, 1
    KEY_LEN = 32

    def __init__(self, path: Optional[Path] = None, passphrase: Optional[str] = None):
        super().__init__()
        self.path = Path(path) if path else Path.home() / ".cryptodesk" / "vault.json"
        self._passphrase = passphrase or os.environ.get("CRYPTODESK_VAULT_PASSPHRASE")
        self._cache: Optional[Dict[str, str]] = None

    @staticmethod
    def available() -> bool:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            return True
        except ImportError:
            return False

    def _aesgcm(self, salt: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise VaultError(
                "EncryptedFileVault needs the `cryptography` package "
                "(pip install cryptography). Refusing to fall back to weaker "
                "encryption. Use MacKeychainVault instead - it needs nothing."
            )
        if not self._passphrase:
            raise VaultLocked(
                "no passphrase; pass one or set CRYPTODESK_VAULT_PASSPHRASE"
            )
        return AESGCM(self._derive_key(salt))

    def _derive_key(self, salt: bytes) -> bytes:
        """scrypt KDF, from whichever implementation is present.

        `hashlib.scrypt` only exists when Python was linked against OpenSSL
        1.1+, which Apple's system Python here was not. Rather than making the
        vault unusable on that build, fall back to `cryptography`'s own Scrypt
        - same algorithm, same parameters, and it is already a dependency of
        this backend because AES-GCM comes from there. The derived key is
        identical either way, so a vault file stays portable between the two.
        """
        import hashlib

        password = self._passphrase.encode("utf-8")
        if hasattr(hashlib, "scrypt"):
            return hashlib.scrypt(
                password, salt=salt, n=self.N, r=self.R, p=self.P,
                dklen=self.KEY_LEN, maxmem=self.N * self.R * 200,
            )
        try:
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        except ImportError:
            raise VaultError(
                "no scrypt implementation available: this Python lacks "
                "hashlib.scrypt (needs OpenSSL 1.1+) and `cryptography` is "
                "not installed. Refusing to weaken the KDF."
            )
        return Scrypt(salt=salt, length=self.KEY_LEN,
                      n=self.N, r=self.R, p=self.P).derive(password)

    def _load(self) -> Dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        blob = json.loads(self.path.read_text())
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        ct = base64.b64decode(blob["ciphertext"])
        try:
            plain = self._aesgcm(salt).decrypt(nonce, ct, b"cryptodesk-vault-v1")
        except Exception as exc:
            # GCM authenticates: a failure here is a wrong passphrase or a
            # tampered file, and those are not distinguishable by design.
            raise VaultLocked("cannot decrypt vault (wrong passphrase or "
                              "tampered file): %s" % type(exc).__name__)
        self._cache = json.loads(plain.decode("utf-8"))
        return self._cache

    def _save(self, secrets: Dict[str, str]) -> None:
        salt = os.urandom(16)
        nonce = os.urandom(12)
        ct = self._aesgcm(salt).encrypt(
            nonce, json.dumps(secrets).encode("utf-8"), b"cryptodesk-vault-v1"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "version": 1,
            "kdf": {"name": "scrypt", "n": self.N, "r": self.R, "p": self.P},
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ct).decode(),
        }, indent=2))
        os.chmod(str(tmp), 0o600)
        # Atomic replace so an interrupted write cannot truncate the vault.
        tmp.replace(self.path)
        self._cache = secrets

    def _fetch(self, name: str) -> str:
        try:
            return self._load()[name]
        except KeyError:
            raise KeyNotFound("no key %r in %s" % (name, self.path))

    def put(self, name: str, secret: str) -> None:
        secrets = dict(self._load())
        secrets[name] = secret
        self._save(secrets)

    def delete(self, name: str) -> bool:
        secrets = dict(self._load())
        existed = secrets.pop(name, None) is not None
        if existed:
            self._save(secrets)
        return existed

    def list_keys(self) -> List[str]:
        try:
            return sorted(self._load())
        except VaultError:
            return []


class KmsVault(KeyVault):
    """Contract for a non-exporting backend (AWS KMS, GCP KMS, an HSM).

    Not wired to a provider - that needs an SDK and an account this package
    deliberately does not require. It is here because it is the shape the
    interface has to support: `exportable = False` means `reveal()` raises, and
    any code path that still works against this backend is a path that never
    touched key material. Running your test suite against `KmsVault` is how you
    prove that.
    """

    backend_name = "kms"
    exportable = False

    def __init__(self, key_ids: Optional[Dict[str, str]] = None):
        super().__init__()
        self.key_ids = dict(key_ids or {})

    def _fetch(self, name: str) -> str:
        raise ExportForbidden(
            "KMS-held key %r cannot be exported; sign through the provider" % name
        )

    def has(self, name: str) -> bool:
        return name in self.key_ids

    def list_keys(self) -> List[str]:
        return sorted(self.key_ids)


class ChainVault(KeyVault):
    """Tries several backends in order, first hit wins.

    Order is the policy: put the strongest backend first so a key present in
    both the keychain and an env var resolves to the keychain. `which()`
    reports where a key actually came from, which matters when a stale env var
    is shadowing the value you thought you were using.
    """

    backend_name = "chain"

    def __init__(self, *vaults: KeyVault):
        super().__init__()
        self.vaults: List[KeyVault] = [v for v in vaults if v is not None]

    def _fetch(self, name: str) -> str:
        errors = []
        for v in self.vaults:
            try:
                return v._fetch(name)
            except VaultError as exc:
                errors.append("%s: %s" % (v.backend_name, exc))
        raise KeyNotFound("key %r not in any backend (%s)" % (name, "; ".join(errors)))

    def which(self, name: str) -> Optional[str]:
        for v in self.vaults:
            try:
                v._fetch(name)
                return v.backend_name
            except VaultError:
                continue
        return None

    def list_keys(self) -> List[str]:
        seen: List[str] = []
        for v in self.vaults:
            for k in v.list_keys():
                if k not in seen:
                    seen.append(k)
        return sorted(seen)


def default_vault(service: str = DEFAULT_SERVICE) -> ChainVault:
    """The recommended stack for this machine: Keychain, then env.

    Keychain first so that adding a key properly takes precedence over a
    leftover env var, and env as a fallback so CI - which has no keychain -
    still works.
    """
    backends: List[KeyVault] = []
    if MacKeychainVault.available():
        backends.append(MacKeychainVault(service))
    backends.append(EnvVault())
    return ChainVault(*backends)
