"""Password hashing with hashlib.scrypt.

Parameters are stored WITH the hash, so raising them later does not invalidate
existing passwords: an old hash keeps verifying at its original cost and is
upgraded on the next successful login (see needs_rehash).

Comparison is constant-time. A string == on a digest leaks its prefix through
timing, which is a real attack on a hash that is otherwise fine.
"""
from __future__ import annotations

import hashlib
import hmac
import os

# n=2**15 with r=8 is ~32 MB per hash: enough to make large-scale GPU cracking
# expensive without making a login noticeably slow on modest hardware.
DEFAULT_N = 1 << 15
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                  p: int = DEFAULT_P) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES,
                         maxmem=n * r * 256 + (1 << 20))
    return f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
    except (ValueError, AttributeError):
        # A malformed stored hash is a failed verification, never an exception
        # that a caller might catch and treat as success.
        return False
    try:
        candidate = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                                   dklen=len(expected), maxmem=n * r * 256 + (1 << 20))
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                 p: int = DEFAULT_P) -> bool:
    """True when the stored hash used weaker parameters than current policy."""
    try:
        scheme, n_s, r_s, p_s, _, _ = stored.split("$")
        return scheme != "scrypt" or (int(n_s), int(r_s), int(p_s)) != (n, r, p)
    except (ValueError, AttributeError):
        return True
