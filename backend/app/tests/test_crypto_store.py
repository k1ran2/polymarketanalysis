"""
Unit tests for app.crypto_store. Every test monkeypatches keyring away
entirely (never touches the real OS keychain -- a test run must not leave
stray entries in the developer's actual macOS/Windows/Linux keychain) and
points the keyfile at a tmp_path, so these are fully hermetic.
"""
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from app import crypto_store


@pytest.fixture(autouse=True)
def _no_real_keyring_or_keyfile(monkeypatch, tmp_path):
    """Forces every test onto the keyfile path, at a throwaway location --
    never the real OS keychain, never the real project data/ directory."""
    monkeypatch.setattr(crypto_store, "_try_keyring_get", lambda: None)
    monkeypatch.setattr(crypto_store, "_try_keyring_set", lambda value: False)
    monkeypatch.setattr(crypto_store, "_KEYFILE_PATH", tmp_path / ".trading_master_key")


def test_get_or_create_master_key_persists_across_calls():
    key1, source1 = crypto_store.get_or_create_master_key()
    key2, source2 = crypto_store.get_or_create_master_key()
    assert key1 == key2
    assert source1 == source2 == "keyfile"


def test_keyfile_has_owner_only_permissions():
    crypto_store.get_or_create_master_key()
    mode = stat.S_IMODE(crypto_store._KEYFILE_PATH.stat().st_mode)
    assert mode == 0o600


def test_encrypt_then_decrypt_round_trips():
    ciphertext, source = crypto_store.encrypt("super-secret-private-key")
    assert ciphertext != b"super-secret-private-key"  # never stored as plaintext
    plaintext = crypto_store.decrypt(ciphertext, source)
    assert plaintext == "super-secret-private-key"


def test_decrypt_fails_with_a_wrong_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    ciphertext, source = crypto_store.encrypt("secret-a")
    # Simulate the master key having been replaced with a DIFFERENT valid
    # key (not simply missing) -- decrypt must fail on the mismatch itself.
    different_key_path = tmp_path / "different_key_file"
    different_key_path.write_text(Fernet.generate_key().decode())
    monkeypatch.setattr(crypto_store, "_KEYFILE_PATH", different_key_path)
    with pytest.raises(ValueError):
        crypto_store.decrypt(ciphertext, source)


def test_wipe_master_key_removes_the_keyfile():
    crypto_store.get_or_create_master_key()
    assert crypto_store._KEYFILE_PATH.exists()
    crypto_store.wipe_master_key()
    assert not crypto_store._KEYFILE_PATH.exists()


def test_prefers_keyring_when_available(monkeypatch):
    store = {}
    monkeypatch.setattr(crypto_store, "_try_keyring_get", lambda: store.get("key"))
    monkeypatch.setattr(crypto_store, "_try_keyring_set", lambda value: store.setdefault("key", value) or True)

    key, source = crypto_store.get_or_create_master_key()
    assert source == "keyring"
    assert not crypto_store._KEYFILE_PATH.exists()  # keyfile never touched when keyring works

    # A second call reads the same key back from the (fake) keyring
    key2, source2 = crypto_store.get_or_create_master_key()
    assert key2 == key
    assert source2 == "keyring"
