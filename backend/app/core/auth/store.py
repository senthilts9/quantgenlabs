"""User store.

In-memory and seeded from the environment, deliberately: this platform has no
user-administration workflow, and a half-built one backed by a database would
imply an account lifecycle that does not exist. When QGL_DATABASE_URL is
configured the users table is the natural next step; until then the honest shape
is an explicit, small set of accounts the operator provisions.

NO DEFAULT PASSWORDS. Accounts come from QGL_USERS, and with it unset the store is
EMPTY and every guarded endpoint returns 401. A seeded admin/admin would be a
public account on every deployment that forgot to change it.
"""
from __future__ import annotations

import os
import threading

from .models import Role, User
from .passwords import hash_password, needs_rehash, verify_password


class UserStore:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._lock = threading.Lock()

    def add(self, username: str, password: str, roles: list[Role],
            display_name: str = "") -> User:
        with self._lock:
            u = User(username=username, password_hash=hash_password(password),
                     roles=roles, display_name=display_name or username)
            self._users[username] = u
            return u

    def get(self, username: str) -> User | None:
        return self._users.get(username)

    def authenticate(self, username: str, password: str) -> User | None:
        u = self._users.get(username)
        if u is None:
            # Verify against a dummy hash anyway so an unknown username and a
            # wrong password take the same time. Otherwise the response time
            # enumerates valid accounts.
            verify_password(password, _DUMMY_HASH)
            return None
        if u.disabled or not verify_password(password, u.password_hash):
            return None
        if needs_rehash(u.password_hash):
            with self._lock:
                u.password_hash = hash_password(password)
        return u

    def revoke_tokens(self, username: str) -> bool:
        """Invalidate every token the user currently holds."""
        with self._lock:
            u = self._users.get(username)
            if u is None:
                return False
            u.token_version += 1
            return True

    def list_users(self) -> list[User]:
        return list(self._users.values())

    def __len__(self) -> int:
        return len(self._users)


# Computed once: a real scrypt hash of a random string, so the timing of the
# unknown-user path matches the real one.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-comparison")

_store: UserStore | None = None
_store_lock = threading.Lock()


def _seed_from_env(store: UserStore) -> None:
    """QGL_USERS format: user:password:role[,role][:Display Name]

        alice:s3cret:trader
        bob:hunter2:risk,viewer:Bob Chen

    The display name is optional and defaults to the username. It exists so the
    UI can greet a person rather than an account id -- `store.add` has always
    accepted it; only this parser dropped it, which made the field unreachable
    for anyone provisioning through the environment.

    Intentionally awkward to use for anything but a small explicit set. If this
    needs a nicer format, it needs a real user table instead.
    """
    raw = os.getenv("QGL_USERS", "").strip()
    if not raw:
        return
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        # Split into at most 4 so a display name may contain a colon.
        parts = entry.split(":", 3)
        if len(parts) < 3:
            continue
        username, password, roles_s = parts[0], parts[1], parts[2]
        display = parts[3].strip() if len(parts) > 3 else ""
        roles = []
        for r in roles_s.split(","):
            try:
                roles.append(Role(r.strip().lower()))
            except ValueError:
                continue
        if username and password and roles:
            store.add(username, password, roles, display_name=display)


def get_user_store() -> UserStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                s = UserStore()
                _seed_from_env(s)
                _store = s
    return _store
