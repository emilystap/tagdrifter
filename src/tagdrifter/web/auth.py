"""Optional single-user login and CSRF protection for the web UI.

- CSRF: a POST is refused when the browser says it came from another site (`Sec-Fetch-Site`,
  falling back to `Origin` vs `Host`). Clients that send neither header (curl, scripts) can't be
  driven by a third-party page and are allowed. Always on.
- Login: when a password is configured, every page except /login, /static and /healthz needs a
  session cookie. The password is stored as an scrypt hash; the cookie is an expiry time signed
  with HMAC-SHA256 using a secret key kept in the data directory. Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings

SESSION_COOKIE = "tagdrifter_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # seconds
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = ("/login", "/healthz", "/static/")

# scrypt parameters for password hashes: ~16 MiB of memory, well under a second per check.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- Passwords ------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """`scrypt:N:r:p:salt:hash`, with no `$` so it can be pasted into compose files as-is."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = stored.split(":")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=128 * 1024 * 1024,
            dklen=len(_unb64(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(expected))


def validate_password_hash(stored: str) -> None:
    """Raise ValueError for a malformed hash, so a typo fails at startup, not at login."""
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != "scrypt" or not all(p.isdigit() for p in parts[1:4]):
        raise ValueError(
            "invalid password hash (expected the output of `tagdrifter hash-password`, "
            "which starts with 'scrypt:')"
        )


class Authenticator:
    """Checks the password and issues/verifies session cookies. Disabled without a password."""

    def __init__(self, settings: Settings) -> None:
        self.password_hash = settings.password_hash
        if self.password_hash:
            validate_password_hash(self.password_hash)
        self.enabled = bool(self.password_hash)
        self._key = load_secret_key(settings) if self.enabled else b""

    def check_password(self, password: str) -> bool:
        return self.enabled and verify_password(password, self.password_hash)

    def new_session(self, now: float | None = None) -> str:
        expires = str(int((now or time.time()) + SESSION_MAX_AGE))
        return f"{expires}.{self._sign(expires)}"

    def valid_session(self, cookie: str | None, now: float | None = None) -> bool:
        if not self.enabled or not cookie or "." not in cookie:
            return False
        expires, signature = cookie.split(".", 1)
        if not hmac.compare_digest(signature, self._sign(expires)):
            return False
        return expires.isdigit() and int(expires) > (now or time.time())

    def _sign(self, value: str) -> str:
        return _b64(hmac.new(self._key, value.encode(), hashlib.sha256).digest())


def load_secret_key(settings: Settings) -> bytes:
    """The configured secret key, else one generated once and kept next to the database, so
    sessions survive restarts. Changing it logs everyone out."""
    if settings.secret_key:
        return settings.secret_key.encode()
    path = Path(settings.database_path).parent / "secret_key"
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    path.write_bytes(key)
    path.chmod(0o600)
    return key


# --- Request checks ---------------------------------------------------------------------------


def is_cross_site(method: str, headers) -> bool:
    """Whether an unsafe request was sent by a page on another site (a CSRF attempt)."""
    if method.upper() not in UNSAFE_METHODS:
        return False
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin:
        # Compare host[:port] only: behind a TLS-terminating proxy the app sees http while the
        # browser's Origin says https.
        return (
            origin == "null" or urlsplit(origin).netloc.lower() != headers.get("host", "").lower()
        )
    return False


def is_public(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in PUBLIC_PATHS)


def safe_next(target: str | None) -> str:
    """A same-site path to return to after login; anything else goes to the dashboard."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
