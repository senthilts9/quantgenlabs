"""Authentication endpoints for the institutional side.

Not the crypto desk: that authenticates by wallet signature under
/api/cryptodesk/auth/* and is untouched by this.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.auth import (PERMISSIONS, Permission, Role, User, auth_required,
                         current_user, get_user_store, issue_token, require)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    username: str
    # Carried on the login response as well as /me so the UI can render the
    # header from one call. Defaults to the username when unset.
    display_name: str
    roles: list[str]
    permissions: list[str]


@router.get("/status")
def status_() -> dict:
    """Whether enforcement is on, and how many accounts exist. Deliberately does
    NOT list usernames: an unauthenticated endpoint that enumerates accounts is a
    gift to anyone brute-forcing them."""
    store = get_user_store()
    return {
        "enforcement": "on" if auth_required() else "off",
        "accounts_configured": len(store),
        "roles": [r.value for r in Role],
        "note": (
            "Enforcement is controlled by QGL_REQUIRE_AUTH. With it off, guarded "
            "endpoints still run their guard and record the actor as 'anonymous', "
            "so the audit trail distinguishes authenticated calls from open ones."
            if not auth_required() else
            "Enforcement is ON. Guarded endpoints require a bearer token."
        ),
        "accounts_source": "QGL_USERS" if os.getenv("QGL_USERS") else "none configured",
    }


@router.post("/token", response_model=TokenResponse)
def token(req: TokenRequest) -> TokenResponse:
    store = get_user_store()
    user = store.authenticate(req.username, req.password)
    if user is None:
        # One message for both "no such user" and "wrong password": distinguishing
        # them tells an attacker which usernames are real. The store already makes
        # the two paths take the same time.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    tok, exp = issue_token(username=user.username,
                           roles=[r.value for r in user.roles],
                           token_version=user.token_version)
    return TokenResponse(
        access_token=tok, expires_at=exp.isoformat(), username=user.username,
        display_name=user.display_name or user.username,
        roles=[r.value for r in user.roles],
        permissions=sorted(p.value for p in user.permissions()),
    )


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name or user.username,
        "roles": [r.value for r in user.roles],
        "permissions": sorted(p.value for p in user.permissions()),
        "authenticated": user.username != "anonymous",
        "enforcement": "on" if auth_required() else "off",
    }


@router.get("/matrix")
def matrix() -> dict:
    """The permission matrix, as data. Published so the separation of duties is
    reviewable rather than buried in decorators."""
    return {
        "roles": {r.value: sorted(p.value for p in perms) for r, perms in PERMISSIONS.items()},
        "design_note": (
            "Roles are separations of duty, NOT a hierarchy. A trader cannot set "
            "limits (a trader who can raise their own limit does not have one); a "
            "risk manager cannot submit orders; and admin is deliberately not a "
            "superset of trader, so an administrator account is not also a "
            "trading account."
        ),
    }


@router.post("/revoke", dependencies=[Depends(require(Permission.MANAGE_USERS))])
def revoke(username: str) -> dict:
    """Invalidate every token a user currently holds. This is the revocation path
    a stateless token scheme otherwise lacks."""
    ok = get_user_store().revoke_tokens(username)
    if not ok:
        raise HTTPException(status_code=404, detail="No such user")
    return {"revoked": username}
