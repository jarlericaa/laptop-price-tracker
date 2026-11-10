"""Application-level encryption of the state archives stored in GitHub Releases.

AES-256-GCM, one random 96-bit nonce per object, and associated data that binds the ciphertext to
its purpose and version (a state archive cannot be swapped for a run bundle or an older version).
The key is a 32-byte random value, base64url encoded, held only in the GitHub Actions Secret
``STATE_ENCRYPTION_KEY`` and in the owner's own backup. ``STATE_ENCRYPTION_KEY_PREVIOUS`` may hold
the key being rotated out so older versions stay readable.

A wrong or missing key is always an error. Nothing here falls back to plaintext or to empty state,
and no message ever contains key material.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

MAGIC = b"LTSE1"  # format marker + version
NONCE_BYTES = 12
KEY_BYTES = 32
KEY_ENV = "STATE_ENCRYPTION_KEY"
PREVIOUS_KEY_ENV = "STATE_ENCRYPTION_KEY_PREVIOUS"


class CryptoError(RuntimeError):
    """Encryption problem that must stop the run. Messages never contain key material."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def parse_key(value: str, *, name: str = KEY_ENV) -> bytes:
    text = (value or "").strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise CryptoError(f"{name} is not valid base64url text.") from exc
    if len(raw) != KEY_BYTES:
        raise CryptoError(
            f"{name} must decode to {KEY_BYTES} bytes (create one with "
            "'python scripts/ci_state.py keygen')."
        )
    return raw


def key_id(key: bytes) -> str:
    """A short public fingerprint recorded in manifests so the right key is picked later. It is
    derived through a domain-separated hash and reveals nothing usable about the key."""
    return hashlib.sha256(b"laptop-tracker-state-key-id:" + key).hexdigest()[:16]


class KeyRing:
    """The current key (used to encrypt) plus optional previous keys (decrypt only)."""

    def __init__(self, current: bytes, previous: tuple[bytes, ...] = ()) -> None:
        self.current = current
        self.keys = {key_id(k): k for k in (current, *previous)}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KeyRing:
        env = os.environ if env is None else env
        current = (env.get(KEY_ENV) or "").strip()
        if not current:
            raise CryptoError(
                f"{KEY_ENV} is not set. Snapshots are always encrypted; the key is a GitHub "
                "Actions Secret (see docs/GITHUB_ACTIONS_SETUP.md)."
            )
        previous = (env.get(PREVIOUS_KEY_ENV) or "").strip()
        older = (parse_key(previous, name=PREVIOUS_KEY_ENV),) if previous else ()
        return cls(parse_key(current), older)

    @property
    def current_id(self) -> str:
        return key_id(self.current)

    def key_for(self, wanted_id: str) -> bytes:
        try:
            return self.keys[wanted_id]
        except KeyError:
            raise CryptoError(
                f"This object was encrypted with key {wanted_id}, which is not available. "
                f"Available: {', '.join(sorted(self.keys))}. Provide it as "
                f"{PREVIOUS_KEY_ENV} (or restore the key from your backup)."
            ) from None


def _aad(purpose: str, name: str) -> bytes:
    return f"laptop-tracker|{purpose}|{name}".encode()


def encrypt_bytes(data: bytes, ring: KeyRing, *, purpose: str, name: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(ring.current).encrypt(nonce, data, _aad(purpose, name))


def decrypt_bytes(
    blob: bytes, ring: KeyRing, wanted_key_id: str, *, purpose: str, name: str
) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE_BYTES + 16:
        raise CryptoError(f"{name} is not an encrypted tracker archive (bad header).")
    key = ring.key_for(wanted_key_id)
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES :], _aad(purpose, name))
    except InvalidTag:
        raise CryptoError(
            f"{name} could not be decrypted: the key is wrong or the archive was altered. "
            "Nothing was restored."
        ) from None


def write_key_file(path: Path) -> str:
    """Create a new key file readable only by its owner, refusing to overwrite. Returns the key
    id (never the key)."""
    key = generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise CryptoError(f"{path} already exists; refusing to overwrite a key file.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return key_id(parse_key(key))
