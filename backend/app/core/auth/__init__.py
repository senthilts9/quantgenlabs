"""Authentication and role-based access control for the institutional side.

SCOPE. This is NOT the crypto desk's auth. That desk authenticates by wallet
signature (api/cryptodesk_auth.py, EIP-4361) because on a perps venue the account
IS an address, and proving control of it is a stronger claim than any password.
That module is untouched. This one covers the traditional side — OMS, desk, risk,
connectivity — where the actor is a person with a role, not an address.

WHY JWT RATHER THAN SERVER SESSIONS. The API is already stateless and horizontally
scalable; a session table would put shared mutable state back in front of every
request for no gain. The cost is that a token cannot be revoked before it expires,
which is why expiry is short and why the token carries a token_version claim
checked against the user — bumping that version invalidates every token that user
holds, which is the revocation path.

WHY SCRYPT RATHER THAN A HASHING LIBRARY. hashlib.scrypt is in the standard
library, is memory-hard (so GPU attacks gain far less than against PBKDF2), and
adds no dependency. Parameters are stored WITH each hash, so raising them later
does not invalidate existing passwords — old hashes keep verifying at their
original cost and are upgraded on next successful login.

WHAT IS DELIBERATELY NOT HAND-ROLLED. JWT signing and verification use PyJWT.
Hand-rolling it would mean hand-rolling algorithm confusion defences and expiry
validation, and a signature bug is not a crash — it is a silent acceptance you
find out about later. Same reasoning the crypto desk applies to secp256k1.
"""
from .models import Role, User, Permission, has_permission, PERMISSIONS
from .passwords import hash_password, needs_rehash, verify_password
from .tokens import TokenError, decode_token, issue_token
from .store import UserStore, get_user_store
from .deps import ANONYMOUS, auth_required, current_user, require, require_role

__all__ = ["Role", "User", "Permission", "PERMISSIONS", "has_permission",
           "ANONYMOUS", "auth_required", "current_user", "require", "require_role",
           "hash_password", "verify_password", "needs_rehash",
           "issue_token", "decode_token", "TokenError",
           "UserStore", "get_user_store"]
