"""App login: one password, stored only as a salted scrypt hash in ``APP_PASSWORD_HASH``.

Set it with ``uv run stockapp set-password`` (prompts twice, prints the line for .env). The app
refuses to start without it. Comparison is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N, _R, _P = 2**14, 8, 1
PREFIX = "scrypt"


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ValueError("use at least 10 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "$".join([PREFIX, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, salt_b64, digest_b64 = stored.split("$")
        if prefix != PREFIX:
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(actual, expected)
