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

    def issue(self, user_id: str, epoch: int = 0) -> str:
        return self.signer.dumps({"uid": user_id, "ep": epoch, "iat": int(time.time())})

    def read(self, token: str | None) -> tuple[str, int] | None:
        """(user id, session epoch) from a valid token. The caller must compare the epoch with the user's current one:
        bumping `users.session_epoch` revokes every session issued before it."""
        if not token:
            return None
        try:
            data = self.signer.loads(token, max_age=MAX_AGE)
        except BadSignature:
            return None
        uid = data.get("uid") if isinstance(data, dict) else None
        return (uid, int(data.get("ep", 0))) if isinstance(uid, str) else None


class Tokens:
    """Stateless, purpose-bound, expiring tokens for email links (verify address, reset password).

    Each token is signed with its own salt, so a token for one purpose is useless for another, and carries a `binding`
    string the caller must compare with current state: a reset token is bound to a fingerprint of the password hash, so
    it stops working the moment the password changes (single use) and a verify token to the address it was sent to.
    """

    def __init__(self, secret: str):
        self.secret = secret or secrets.token_urlsafe(48)

    def issue(self, purpose: str, user_id: str, binding: str) -> str:
        return URLSafeTimedSerializer(self.secret, salt=f"hotload-{purpose}").dumps({"uid": user_id, "b": binding})

    def read(self, purpose: str, token: str | None, max_age: int) -> tuple[str, str] | None:
        if not token or len(token) > 1024:
            return None
        try:
            data = URLSafeTimedSerializer(self.secret, salt=f"hotload-{purpose}").loads(token, max_age=max_age)
        except BadSignature:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("uid"), str) or not isinstance(data.get("b"), str):
            return None
        return data["uid"], data["b"]


def fingerprint(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:24]


def safe_equal(a: str | None, b: str | None) -> bool:
    """Constant-time string comparison that cannot raise on non-ASCII input (compare_digest on str can)."""
    return hmac.compare_digest((a or "").encode("utf-8", "ignore"), (b or "").encode("utf-8", "ignore"))


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
