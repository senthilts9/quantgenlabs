"""FastAPI dependencies for authentication and authorisation.

ENFORCEMENT IS OPT-IN, via QGL_REQUIRE_AUTH. Off by default, and that is a
deliberate, stated choice rather than an accident:

  * Turning it on unconditionally would break every existing screen and every
    existing test in one commit, which is how a security feature gets reverted.
  * The platform already degrades this way elsewhere (HAVE_CPP, QGL_DATABASE_URL),
    so the pattern is consistent and visible.
  * With it off, guarded endpoints still RUN the guard and still report who the
    actor is -- "anonymous" -- so the audit trail records the difference rather
    than pretending everything was authenticated.

The important property: enabling it is one environment variable, and the tests
exercise the ENABLED path, so what ships is not an untested branch.

401 vs 403 is kept honest. 401 means "I do not know who you are" and 403 means
"I know who you are and you may not do this". Collapsing them tells an attacker
less but tells a legitimate user nothing about how to fix their problem.
"""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import Permission, Role, User, has_permission
from .store import get_user_store
from .tokens import TokenError, decode_token

# auto_error=False so a missing header reaches our code and produces our message
# rather than FastAPI's generic one.
_bearer = HTTPBearer(auto_error=False)

ANONYMOUS = User(username="anonymous", password_hash="", roles=[Role.VIEWER])


def auth_required() -> bool:
    return os.getenv("QGL_REQUIRE_AUTH", "").lower() in ("1", "true", "yes", "on")


def current_user(request: Request,
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    """Resolves the caller. Returns ANONYMOUS when enforcement is off and no
    credentials were supplied; raises 401 when enforcement is on."""
    if creds is None or not creds.credentials:
        if auth_required():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. POST /api/auth/token to obtain one.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return ANONYMOUS

    try:
        claims = decode_token(creds.credentials)
    except TokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e

    store = get_user_store()
    user = store.get(claims.get("sub", ""))
    if user is None or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Unknown or disabled account")

    # The revocation path: a token minted before the user's version was bumped is
    # no longer valid, even though its signature and expiry are both fine.
    if int(claims.get("ver", 0)) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token has been revoked; sign in again")

    request.state.actor = user.username
    return user


def require(*permissions: Permission):
    """Guard requiring ALL listed permissions.

    Usage:  @router.post(..., dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
    """
    def guard(user: User = Depends(current_user)) -> User:
        if not auth_required():
            return user          # pass through, but the guard still ran
        missing = [p for p in permissions if not has_permission(user.roles, p)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(f"Role(s) {[r.value for r in user.roles]} lack "
                        f"{[p.value for p in missing]}"),
            )
        return user
    return guard


def require_role(*roles: Role):
    def guard(user: User = Depends(current_user)) -> User:
        if not auth_required():
            return user
        if not any(r in user.roles for r in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {[r.value for r in roles]}",
            )
        return user
    return guard
