"""Password hashing and signed session cookies."""

import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "hl_session"
MAX_AGE = 30 * 24 * 3600
_ph = PasswordHasher()
_DUMMY = _ph.hash("hotload-dummy-password")  # verified against for unknown emails, to even out timing


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _ph.verify(stored or _DUMMY, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


class Sessions:
    def __init__(self, secret: str):
        if not secret:  # sessions then reset on every restart; fine for dev, warn loudly in docs
            secret = secrets.token_urlsafe(48)
        self.signer = URLSafeTimedSerializer(secret, salt="hotload-session")

    def issue(self, user_id: str) -> str:
        return self.signer.dumps({"uid": user_id, "iat": int(time.time())})

    def read(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self.signer.loads(token, max_age=MAX_AGE).get("uid")
        except BadSignature:
            return None
