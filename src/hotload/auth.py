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


# --- API keys ---------------------------------------------------------------------------------------------
# Format: hl_<id>_<secret>. The id is public (lookup); only a SHA-256 of the secret is stored. The secret has 256
# bits of entropy, so a fast hash is appropriate here (unlike passwords).

import hashlib  # noqa: E402
import hmac  # noqa: E402


def new_api_key() -> tuple[str, str, str, str]:
    """Return (full key shown once, key id, secret hash, webhook signing secret)."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return f"hl_{key_id}_{secret}", key_id, hash_secret(secret), secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_api_key(token: str) -> tuple[str, str] | None:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "hl" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def key_matches(stored_hash: str, secret: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_secret(secret))
