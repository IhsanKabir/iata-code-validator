"""A password in front of one tab.

The password itself is not in the source: only a salted PBKDF2 fingerprint
of it, so reading the code or the built program does not reveal it. It
gates the SCREEN only -- what a tab has already saved to disk is as
readable as any other file on the machine.
"""
from __future__ import annotations

import hashlib
import hmac

ITERATIONS = 200_000

#: The FFP Customers tab. Change by deriving a new pair with derive().
FFP_SALT = bytes.fromhex("698e9b517225022265b8a0635e45c9f8")
FFP_HASH = bytes.fromhex(
    "62615efa9de869e18fbd04b05eea01c059ddcfbe1998ee3f203dee55f4a57e99")


def derive(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               ITERATIONS)


def check(password: str, salt: bytes | None = None,
          expected: bytes | None = None) -> bool:
    """True only for the right password, compared in constant time."""
    if not password:
        return False
    salt = FFP_SALT if salt is None else salt
    expected = FFP_HASH if expected is None else expected
    return hmac.compare_digest(derive(password, salt), expected)
