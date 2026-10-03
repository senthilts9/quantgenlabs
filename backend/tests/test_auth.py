"""Auth and RBAC tests.

The important half of this file runs with enforcement ENABLED. A security feature
that is only ever tested in its disabled state is an untested branch that ships.

Separation of duties is asserted in both directions: a trader must be refused a
risk action, and a risk manager must be refused a trading action. Testing only
that the permitted case works would pass against a guard that permits everything.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth import Permission, Role, has_permission
from app.core.auth.passwords import (hash_password, needs_rehash, verify_password)
from app.core.auth.store import UserStore
from app.core.auth.tokens import TokenError, decode_token, issue_token
from app.main import app

client = TestClient(app)


# ------------------------------------------------------------------- passwords
def test_hash_verify_round_trip():
    h = hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_each_hash_has_a_unique_salt():
    a, b = hash_password("same"), hash_password("same")
    assert a != b                      # identical passwords, different hashes
    assert verify_password("same", a) and verify_password("same", b)


@pytest.mark.parametrize("bad", ["", "notascheme$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb",
                                 "scrypt$1024$8$1$nothex$bb", None])
def test_malformed_stored_hash_fails_closed(bad):
    """A malformed hash must be a failed verification, never an exception a
    caller might catch and treat as success."""
    assert verify_password("anything", bad) is False


def test_empty_password_refused_at_hash_time():
    with pytest.raises(ValueError):
        hash_password("")


def test_needs_rehash_detects_weaker_parameters():
    weak = hash_password("pw", n=1 << 12)
    assert needs_rehash(weak) is True
    assert needs_rehash(hash_password("pw")) is False


# ---------------------------------------------------------------------- tokens
def test_token_round_trip_carries_claims():
    tok, exp = issue_token(username="alice", roles=["trader"], token_version=3)
    claims = decode_token(tok)
    assert claims["sub"] == "alice"
    assert claims["roles"] == ["trader"]
    assert claims["ver"] == 3
    assert claims["iss"] == "quantgenlabs"


def test_tampered_token_is_rejected():
    tok, _ = issue_token(username="alice", roles=["viewer"])
    head, payload, sig = tok.split(".")
    forged = f"{head}.{payload}.{'A' * len(sig)}"
    with pytest.raises(TokenError):
        decode_token(forged)


def test_alg_none_token_is_rejected():
    """The classic JWT attack: a token claiming alg=none. The verifier pins
    HS256, so this must fail rather than be honoured."""
    import base64, json
    def seg(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    forged = f'{seg({"alg": "none", "typ": "JWT"})}.{seg({"sub": "admin", "roles": ["admin"], "iss": "quantgenlabs", "exp": 9999999999, "iat": 1})}.'
    with pytest.raises(TokenError):
        decode_token(forged)


def test_expired_token_is_rejected():
    tok, _ = issue_token(username="alice", roles=["viewer"], ttl_minutes=-1)
    with pytest.raises(TokenError, match="expired"):
        decode_token(tok)


# ------------------------------------------------------------- permission matrix
@pytest.mark.parametrize("role,permission,allowed", [
    # A trader can trade but cannot change the limits that constrain them.
    (Role.TRADER, Permission.SUBMIT_ORDER, True),
    (Role.TRADER, Permission.CANCEL_ORDER, True),
    (Role.TRADER, Permission.SET_LIMITS, False),
    (Role.TRADER, Permission.KILL_SWITCH, False),
    # A risk manager controls the limits but cannot trade.
    (Role.RISK, Permission.SET_LIMITS, True),
    (Role.RISK, Permission.KILL_SWITCH, True),
    (Role.RISK, Permission.CANCEL_ORDER, True),      # can pull, cannot place
    (Role.RISK, Permission.SUBMIT_ORDER, False),
    # Admin administers; it is deliberately NOT a superset of trader.
    (Role.ADMIN, Permission.MANAGE_USERS, True),
    (Role.ADMIN, Permission.SUBMIT_ORDER, False),
    (Role.ADMIN, Permission.SET_LIMITS, False),
    # Viewer reads only.
    (Role.VIEWER, Permission.READ_BLOTTER, True),
    (Role.VIEWER, Permission.SUBMIT_ORDER, False),
])
def test_separation_of_duties(role, permission, allowed):
    assert has_permission([role], permission) is allowed


def test_multiple_roles_union_their_permissions():
    both = [Role.TRADER, Role.RISK]
    assert has_permission(both, Permission.SUBMIT_ORDER)     # from trader
    assert has_permission(both, Permission.KILL_SWITCH)      # from risk


# ------------------------------------------------------------------- user store
def test_unknown_user_and_wrong_password_both_fail():
    s = UserStore()
    s.add("alice", "pw12345678", [Role.TRADER])
    assert s.authenticate("alice", "pw12345678") is not None
    assert s.authenticate("alice", "wrong") is None
    assert s.authenticate("nobody", "pw12345678") is None


def test_revoking_bumps_token_version():
    s = UserStore()
    u = s.add("alice", "pw12345678", [Role.TRADER])
    assert u.token_version == 1
    assert s.revoke_tokens("alice") is True
    assert s.get("alice").token_version == 2
    assert s.revoke_tokens("ghost") is False


def test_disabled_account_cannot_authenticate():
    s = UserStore()
    u = s.add("alice", "pw12345678", [Role.TRADER])
    u.disabled = True
    assert s.authenticate("alice", "pw12345678") is None


def test_store_is_empty_without_configuration():
    """No default accounts. A seeded admin/admin would be a public account on
    every deployment that forgot to change it."""
    assert len(UserStore()) == 0


# ------------------------------------------------------------ endpoints (open)
def test_status_does_not_enumerate_accounts():
    d = client.get("/api/auth/status").json()
    assert "accounts_configured" in d
    assert "usernames" not in d and "users" not in d


def test_matrix_is_published_as_data():
    d = client.get("/api/auth/matrix").json()
    assert "write:order" in d["roles"]["trader"]
    assert "write:order" not in d["roles"]["risk"]
    assert "write:limits" not in d["roles"]["trader"]


def test_bad_credentials_give_one_message(monkeypatch):
    a = client.post("/api/auth/token", json={"username": "ghost", "password": "x"})
    b = client.post("/api/auth/token", json={"username": "alice", "password": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]   # no account enumeration


# ---------------------------------------------------- endpoints (ENFORCEMENT ON)
@pytest.fixture
def enforced(monkeypatch):
    """Enforcement on, with three accounts. This is the branch that matters."""
    import app.core.auth.store as store_mod
    monkeypatch.setenv("QGL_REQUIRE_AUTH", "1")
    monkeypatch.setenv("QGL_JWT_SECRET", "x" * 48)
    s = UserStore()
    s.add("trader1", "pw12345678", [Role.TRADER])
    s.add("risk1", "pw12345678", [Role.RISK])
    s.add("admin1", "pw12345678", [Role.ADMIN])
    monkeypatch.setattr(store_mod, "_store", s)
    yield s


def _tok(username: str) -> str:
    r = client.post("/api/auth/token", json={"username": username, "password": "pw12345678"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_enforced_requires_a_token(enforced):
    r = client.post("/api/desk/session", json={"mid": 100})
    assert r.status_code == 401
    assert "Authentication required" in r.json()["detail"]


def test_enforced_rejects_a_garbage_token(enforced):
    r = client.post("/api/desk/session", json={"mid": 100},
                    headers=_auth("not-a-real-token"))
    assert r.status_code == 401


def test_trader_may_open_a_desk_and_send_orders(enforced):
    tok = _tok("trader1")
    r = client.post("/api/desk/session", json={"mid": 100}, headers=_auth(tok))
    assert r.status_code in (200, 503)     # 503 only if the C++ ext is missing
    if r.status_code == 503:
        pytest.skip("C++ extension not built")
    did = r.json()["desk_id"]
    o = client.post(f"/api/desk/{did}/order",
                    json={"side": "1", "qty": 10, "price": 100.01},
                    headers=_auth(tok))
    assert o.status_code == 200


def test_risk_manager_is_refused_order_submission(enforced):
    """403, not 401: we know who they are, and they may not do this."""
    r = client.post("/api/desk/session", json={"mid": 100}, headers=_auth(_tok("risk1")))
    assert r.status_code == 403
    assert "write:order" in r.json()["detail"]


def test_admin_is_refused_order_submission(enforced):
    """Admin is not a superset of trader, so an admin account is not also a
    trading account."""
    r = client.post("/api/desk/session", json={"mid": 100}, headers=_auth(_tok("admin1")))
    assert r.status_code == 403


def test_risk_manager_may_evaluate_the_pretrade_gate(enforced):
    r = client.post("/api/connectivity/pretrade",
                    json={"side": 0, "qty": 10, "price": 100.0, "reference_price": 100.0},
                    headers=_auth(_tok("risk1")))
    assert r.status_code in (200, 503)


def test_revocation_invalidates_an_issued_token(enforced):
    tok = _tok("trader1")
    assert client.get("/api/auth/me", headers=_auth(tok)).json()["authenticated"] is True
    enforced.revoke_tokens("trader1")
    r = client.get("/api/auth/me", headers=_auth(tok))
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"]


def test_me_reports_roles_and_permissions(enforced):
    d = client.get("/api/auth/me", headers=_auth(_tok("trader1"))).json()
    assert d["username"] == "trader1"
    assert d["roles"] == ["trader"]
    assert "write:order" in d["permissions"]
    assert "write:limits" not in d["permissions"]
    assert d["enforcement"] == "on"


def test_only_admin_may_revoke(enforced):
    r = client.post("/api/auth/revoke", params={"username": "trader1"},
                    headers=_auth(_tok("trader1")))
    assert r.status_code == 403
    r = client.post("/api/auth/revoke", params={"username": "trader1"},
                    headers=_auth(_tok("admin1")))
    assert r.status_code == 200


# ------------------------------------- provisioning display names from the env
def _seed(raw: str):
    """Build a store from a QGL_USERS string without touching the global one."""
    import os
    from app.core.auth.store import UserStore, _seed_from_env
    s = UserStore()
    old = os.environ.get("QGL_USERS")
    os.environ["QGL_USERS"] = raw
    try:
        _seed_from_env(s)
    finally:
        if old is None:
            os.environ.pop("QGL_USERS", None)
        else:
            os.environ["QGL_USERS"] = old
    return s


def test_display_name_is_optional_and_defaults_to_the_username():
    """Three-field entries predate the display name and must keep working --
    silently dropping those accounts would lock everyone out on upgrade."""
    s = _seed("alice:s3cret:trader")
    u = s.get("alice")
    assert u is not None and u.display_name == "alice"
    assert s.authenticate("alice", "s3cret") is not None


def test_a_fourth_field_sets_the_display_name():
    s = _seed("bob:hunter2:risk,viewer:Bob Chen")
    u = s.get("bob")
    assert u.display_name == "Bob Chen"
    assert {r.value for r in u.roles} == {"risk", "viewer"}


def test_a_display_name_may_contain_a_colon():
    """Split on at most four parts. Splitting unbounded would truncate any name
    with a colon in it and, worse, silently discard the rest."""
    s = _seed("carol:pw:admin:O'Neill: Head of Risk")
    assert s.get("carol").display_name == "O'Neill: Head of Risk"


def test_malformed_entries_are_skipped_not_fatal():
    """One bad entry must not take out the accounts either side of it, or a
    typo in an env var locks everybody out at once."""
    s = _seed("good1:pw:trader;;garbage;also_bad:onlytwo;good2:pw:viewer:Two")
    assert s.get("good1") is not None
    assert s.get("good2") is not None and s.get("good2").display_name == "Two"
    assert s.get("garbage") is None
    assert s.get("also_bad") is None


def test_entry_with_no_valid_role_is_refused():
    """An account with no role has no permissions. Creating it anyway produces
    a login that succeeds and then fails on every single action."""
    s = _seed("ghost:pw:notarole")
    assert s.get("ghost") is None
