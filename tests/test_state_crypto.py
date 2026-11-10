"""Encryption of state archives: round trips, tampering, key rotation, key files and secrecy."""

from __future__ import annotations

import os
import stat

import pytest

from laptop_tracker import state_crypto as sc


def ring_with(*previous: bytes) -> sc.KeyRing:
    return sc.KeyRing(sc.parse_key(sc.generate_key()), previous)


def test_a_round_trip_restores_the_bytes_and_hides_them_at_rest() -> None:
    ring = ring_with()
    blob = sc.encrypt_bytes(b"sales data" * 100, ring, purpose="state", name="v1")
    assert blob.startswith(sc.MAGIC) and b"sales data" not in blob
    assert sc.decrypt_bytes(blob, ring, ring.current_id, purpose="state", name="v1") == (
        b"sales data" * 100
    )


def test_each_encryption_uses_a_fresh_nonce() -> None:
    ring = ring_with()
    one = sc.encrypt_bytes(b"x", ring, purpose="state", name="v1")
    two = sc.encrypt_bytes(b"x", ring, purpose="state", name="v1")
    assert one != two


def test_a_flipped_bit_the_wrong_key_or_a_swapped_purpose_all_fail() -> None:
    ring = ring_with()
    blob = bytearray(sc.encrypt_bytes(b"payload", ring, purpose="state", name="v1"))
    good = bytes(blob)
    blob[-1] ^= 1
    with pytest.raises(sc.CryptoError, match="could not be decrypted"):
        sc.decrypt_bytes(bytes(blob), ring, ring.current_id, purpose="state", name="v1")
    with pytest.raises(sc.CryptoError, match="could not be decrypted"):
        sc.decrypt_bytes(good, ring, ring.current_id, purpose="bundle", name="v1")
    with pytest.raises(sc.CryptoError, match="could not be decrypted"):
        sc.decrypt_bytes(good, ring, ring.current_id, purpose="state", name="v2")
    stranger = ring_with()
    with pytest.raises(sc.CryptoError, match="not available"):
        sc.decrypt_bytes(good, stranger, ring.current_id, purpose="state", name="v1")
    with pytest.raises(sc.CryptoError, match="bad header"):
        sc.decrypt_bytes(
            b"not encrypted at all, just text........", ring, "x", purpose="s", name="n"
        )


def test_rotation_keeps_old_archives_readable_through_the_previous_key() -> None:
    old = ring_with()
    blob = sc.encrypt_bytes(b"history", old, purpose="state", name="v1")
    new = sc.KeyRing(sc.parse_key(sc.generate_key()), (old.current,))
    assert new.current_id != old.current_id
    assert sc.decrypt_bytes(blob, new, old.current_id, purpose="state", name="v1") == b"history"
    fresh = sc.encrypt_bytes(b"history", new, purpose="state", name="v2")
    assert sc.decrypt_bytes(fresh, new, new.current_id, purpose="state", name="v2") == b"history"


def test_keys_are_validated_and_errors_never_echo_them() -> None:
    secret = "A" * 10
    with pytest.raises(sc.CryptoError) as caught:
        sc.parse_key(secret)
    assert secret not in str(caught.value)
    with pytest.raises(sc.CryptoError, match="32 bytes"):
        sc.parse_key(sc.generate_key()[:20])
    assert len(sc.parse_key(sc.generate_key())) == 32


def test_the_key_ring_reads_current_and_previous_keys_from_the_environment() -> None:
    current, previous = sc.generate_key(), sc.generate_key()
    ring = sc.KeyRing.from_env(
        {"STATE_ENCRYPTION_KEY": current, "STATE_ENCRYPTION_KEY_PREVIOUS": previous}
    )
    assert len(ring.keys) == 2 and ring.current == sc.parse_key(current)
    with pytest.raises(sc.CryptoError, match="STATE_ENCRYPTION_KEY is not set"):
        sc.KeyRing.from_env({})
    with pytest.raises(sc.CryptoError, match="STATE_ENCRYPTION_KEY_PREVIOUS"):
        sc.KeyRing.from_env({"STATE_ENCRYPTION_KEY": current, "STATE_ENCRYPTION_KEY_PREVIOUS": "!"})


def test_the_key_id_is_a_short_fingerprint_that_is_not_the_key() -> None:
    key = sc.generate_key()
    ident = sc.key_id(sc.parse_key(key))
    assert len(ident) == 16 and ident not in key and key not in ident


def test_a_key_file_is_private_never_overwritten_and_never_printed(tmp_path) -> None:
    path = tmp_path / "state.key"
    ident = sc.write_key_file(path)
    key = path.read_text().strip()
    assert sc.key_id(sc.parse_key(key)) == ident and key != ident
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(sc.CryptoError, match="refusing to overwrite"):
        sc.write_key_file(path)
    assert path.read_text().strip() == key
