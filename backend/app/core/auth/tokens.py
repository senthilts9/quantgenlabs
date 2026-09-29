"""JWT issuance and verification.

THE SECRET IS NOT ALLOWED TO HAVE A DEFAULT. A signing key with a fallback value
is a signing key everyone knows: the fallback ships in the repository, nobody sets
the real one, and every deployment shares it. So an unset QGL_JWT_SECRET is a
startup error in production and an ephemeral per-process key in development —
which means tokens stop working when the dev server restarts, and that is the
correct, visible consequence rather than a silent security hole.

ALGORITHM IS PINNED. decode() is always given algorithms=["HS256"] explicitly. The
classic JWT attack is algorithm confusion: a token arrives claiming alg=none or
alg=RS256 and a permissive verifier honours it. Naming the algorithm at the
verification site is what closes that.

EXPIRY IS SHORT because a stateless token cannot be withdrawn. The revocation path
is token_version: the claim is checked against the user record, so bumping the
user's version invalidates every token they hold immediately.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

ALGORITHM = "HS256"
ISSUER = "quantgenlabs"
DEFAULT_TTL_MINUTES = int(os.getenv("QGL_JWT_TTL_MINUTES", "30"))


class TokenError(Exception):
    """Any failure to produce a trustworthy identity from a token."""


def _secret() -> str:
    s = os.getenv("QGL_JWT_SECRET")
    if s:
        if len(s) < 32:
            raise TokenError("QGL_JWT_SECRET must be at least 32 characters")
        return s
    if os.getenv("QGL_ENV", "development").lower() in ("production", "prod"):
        raise TokenError(
            "QGL_JWT_SECRET is required in production. Refusing to sign tokens "
            "with a generated key that every replica would generate differently."
        )
    # Development: ephemeral per-process. Tokens die with the server, which is
    # the honest consequence of not configuring a key.
    global _DEV_SECRET
    if _DEV_SECRET is None:
        _DEV_SECRET = secrets.token_urlsafe(48)
    return _DEV_SECRET


_DEV_SECRET: str | None = None


def issue_token(*, username: str, roles: list[str], token_version: int = 1,
                ttl_minutes: int | None = None) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ttl_minutes or DEFAULT_TTL_MINUTES)
    payload = {
        "sub": username,
        "roles": list(roles),
        "ver": token_version,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), exp


def decode_token(token: str) -> dict:
    """Returns the claims, or raises TokenError. Never returns partial claims on
    failure -- a caller that gets a dict back can rely on it."""
    try:
        return jwt.decode(
            token, _secret(),
            algorithms=[ALGORITHM],          # pinned: closes algorithm confusion
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token expired") from e
    except jwt.InvalidIssuerError as e:
        raise TokenError("wrong issuer") from e
    except jwt.InvalidTokenError as e:
        raise TokenError(f"invalid token: {e}") from e
