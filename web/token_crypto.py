"""Authenticated encryption for OAuth credentials stored in the database."""
import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken


_TOKEN_PREFIX = "v1:"
_KEY_CONTEXT = b"BridgeMD marketing OAuth token encryption v1"


class TokenEncryptionError(RuntimeError):
    """Raised when token encryption is misconfigured or data cannot decrypt."""


def _fernet(fallback_secret):
    configured = os.environ.get("OAUTH_TOKEN_ENCRYPTION_KEY", "").strip()
    if configured:
        key = configured.encode("ascii", errors="strict")
    else:
        if isinstance(fallback_secret, bytes):
            secret = fallback_secret
        else:
            secret = str(fallback_secret or "").encode("utf-8")
        if not secret:
            raise TokenEncryptionError(
                "Set OAUTH_TOKEN_ENCRYPTION_KEY or a stable SECRET_KEY.")
        # Domain separation avoids using the Flask signing key directly as an
        # encryption key while retaining a safe local-development fallback.
        derived = hmac.new(secret, _KEY_CONTEXT, hashlib.sha256).digest()
        key = base64.urlsafe_b64encode(derived)
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise TokenEncryptionError(
            "OAUTH_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def encrypt_token(token, fallback_secret):
    value = str(token or "")
    if not value:
        return ""
    encrypted = _fernet(fallback_secret).encrypt(value.encode("utf-8"))
    return _TOKEN_PREFIX + encrypted.decode("ascii")


def decrypt_token(encrypted_token, fallback_secret):
    value = str(encrypted_token or "")
    if not value:
        return ""
    if not value.startswith(_TOKEN_PREFIX):
        raise TokenEncryptionError("Unsupported encrypted token format.")
    try:
        clear = _fernet(fallback_secret).decrypt(
            value[len(_TOKEN_PREFIX):].encode("ascii"))
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise TokenEncryptionError("Stored OAuth token could not be decrypted.") from exc
    return clear.decode("utf-8")
