"""Encryption of secret settings stored from the admin UI (ticket 43, ADR-0008).

``SETTINGS_ENCRYPTION_KEY`` (any random string of >= 32 characters; env / K8s Secret only) is stretched with
HKDF-SHA256 into a Fernet key (AES-128-CBC + HMAC-SHA256, authenticated). The plaintext is an envelope
``{"k": <setting key>, "v": <value>}``: Fernet authenticates it, so a ciphertext copied onto another row (a DB
write-access attacker swapping blobs) is rejected on decrypt. Older keys (``SETTINGS_ENCRYPTION_KEY_PREVIOUS``)
are accepted for decryption only, which is the rotation path (see ``rotate``).

Nothing here ever logs or raises with a plaintext value.
"""

from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MIN_KEY_CHARS = 32
_SALT = b"rag-pipeline/settings-encryption/v1"
_INFO = b"fernet-key"


class CryptoError(Exception):
    """Encryption is unavailable or a ciphertext cannot be read (wrong key, tampered, wrong row)."""


def _fernet(secret: str) -> Fernet:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(raw))


def _split(previous: str) -> list[str]:
    return [p.strip() for p in previous.split(",") if p.strip()]


class SettingsCipher:
    def __init__(self, key: str, previous: str = "") -> None:
        self._keys = [key, *_split(previous)] if key else []
        for k in self._keys:
            if len(k) < MIN_KEY_CHARS:
                raise CryptoError(
                    f"SETTINGS_ENCRYPTION_KEY must be at least {MIN_KEY_CHARS} characters."
                )
        self._multi = MultiFernet([_fernet(k) for k in self._keys]) if self._keys else None
        self._primary = _fernet(key) if key else None

    @property
    def enabled(self) -> bool:
        return self._multi is not None

    def encrypt(self, setting: str, value: object) -> str:
        if self._primary is None:
            raise CryptoError("SETTINGS_ENCRYPTION_KEY is not set")
        envelope = json.dumps({"k": setting, "v": value}).encode()
        return self._primary.encrypt(envelope).decode()

    def decrypt(self, setting: str, blob: str) -> object:
        if self._multi is None:
            raise CryptoError("SETTINGS_ENCRYPTION_KEY is not set")
        try:
            envelope = json.loads(self._multi.decrypt(blob.encode()))
        except (InvalidToken, ValueError):
            raise CryptoError(
                f"cannot decrypt the stored value of '{setting}' (wrong key or corrupted)"
            ) from None
        if not isinstance(envelope, dict) or envelope.get("k") != setting:
            raise CryptoError(f"stored value of '{setting}' does not belong to this setting")
        return envelope.get("v")

    def rotate(self, setting: str, blob: str) -> str:
        """Re-encrypt ``blob`` under the primary key (after verifying it belongs to ``setting``)."""
        return self.encrypt(setting, self.decrypt(setting, blob))
