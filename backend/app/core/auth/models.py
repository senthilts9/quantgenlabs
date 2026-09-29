"""Roles and the permission matrix.

ROLES ARE COARSE ON PURPOSE. Four roles a trading organisation actually has,
rather than a fine-grained scheme nobody maintains:

  VIEWER  — read-only. Can see the book, blotter and risk, cannot act.
  TRADER  — can send and cancel orders. CANNOT change risk limits, because a
            trader who can raise their own limit does not have a limit.
  RISK    — can change limits and engage the kill switch. CANNOT send orders,
            for the same reason inverted: the control and the activity it
            controls belong to different people.
  ADMIN   — user administration and configuration. Deliberately NOT a superset
            of trading: an administrator does not need to send orders to do
            their job, and granting it anyway means every admin account is also
            a trading account.

That last point is the whole design. Roles are not a hierarchy where each level
inherits the one below; they are separations of duty. ADMIN inheriting TRADER
would defeat the segregation the other roles exist to create.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    VIEWER = "viewer"
    TRADER = "trader"
    RISK = "risk"
    ADMIN = "admin"


class Permission(str, Enum):
    READ_MARKET = "read:market"
    READ_BLOTTER = "read:blotter"
    READ_RISK = "read:risk"
    SUBMIT_ORDER = "write:order"
    CANCEL_ORDER = "write:cancel"
    SET_LIMITS = "write:limits"
    KILL_SWITCH = "write:killswitch"
    MANAGE_USERS = "write:users"
    READ_AUDIT = "read:audit"


# Explicit matrix rather than inheritance: every grant is visible and reviewable
# in one place, and nobody acquires a permission as a side effect of a hierarchy.
PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({
        Permission.READ_MARKET, Permission.READ_BLOTTER, Permission.READ_RISK,
    }),
    Role.TRADER: frozenset({
        Permission.READ_MARKET, Permission.READ_BLOTTER, Permission.READ_RISK,
        Permission.SUBMIT_ORDER, Permission.CANCEL_ORDER,
    }),
    Role.RISK: frozenset({
        Permission.READ_MARKET, Permission.READ_BLOTTER, Permission.READ_RISK,
        Permission.READ_AUDIT,
        Permission.SET_LIMITS, Permission.KILL_SWITCH,
        # A risk manager can pull an order without being able to place one.
        Permission.CANCEL_ORDER,
    }),
    Role.ADMIN: frozenset({
        Permission.READ_MARKET, Permission.READ_BLOTTER, Permission.READ_RISK,
        Permission.READ_AUDIT, Permission.MANAGE_USERS,
    }),
}


def has_permission(roles: list[Role] | list[str], permission: Permission | str) -> bool:
    """True when ANY held role grants the permission. A user may hold more than
    one role; that is how a desk head gets trader+risk, explicitly and visibly,
    rather than by a hierarchy granting it silently."""
    want = Permission(permission) if not isinstance(permission, Permission) else permission
    for r in roles:
        role = Role(r) if not isinstance(r, Role) else r
        if want in PERMISSIONS.get(role, frozenset()):
            return True
    return False


@dataclass
class User:
    username: str
    password_hash: str
    roles: list[Role] = field(default_factory=lambda: [Role.VIEWER])
    disabled: bool = False
    # Bumping this invalidates every token the user currently holds. It is the
    # revocation path a stateless token scheme otherwise lacks.
    token_version: int = 1
    display_name: str = ""

    def permissions(self) -> frozenset[Permission]:
        out: set[Permission] = set()
        for r in self.roles:
            out |= PERMISSIONS.get(r, frozenset())
        return frozenset(out)
