"""
Unit tests for app.trading_credentials. Every test forces crypto_store onto
a throwaway keyfile (never the real OS keychain or the real project data/
directory -- see test_crypto_store.py's fixture for why) and uses a
synthetic private key generated fresh per test, never a real one.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from eth_account import Account

from app import crypto_store, trading_credentials as creds
from app.db import SCHEMA


@pytest.fixture(autouse=True)
def _throwaway_keystore(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto_store, "_try_keyring_get", lambda: None)
    monkeypatch.setattr(crypto_store, "_try_keyring_set", lambda value: False)
    monkeypatch.setattr(crypto_store, "_KEYFILE_PATH", tmp_path / ".trading_master_key")


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _fresh_key() -> str:
    return Account.create().key.hex()


# ---------------------------------------------------------------------------
# derive_address
# ---------------------------------------------------------------------------

def test_derive_address_matches_the_known_address_for_a_key():
    account = Account.create()
    assert creds.derive_address(account.key.hex()) == account.address


def test_derive_address_accepts_with_or_without_0x_prefix():
    account = Account.create()
    raw = account.key.hex()
    bare = raw[2:] if raw.startswith("0x") else raw
    assert creds.derive_address(bare) == account.address
    assert creds.derive_address("0x" + bare) == account.address


def test_derive_address_rejects_garbage():
    with pytest.raises(creds.InvalidPrivateKeyError):
        creds.derive_address("definitely not a private key")


# ---------------------------------------------------------------------------
# save_credentials / get_status / load_private_key / clear_credentials
# ---------------------------------------------------------------------------

def test_save_and_get_status_returns_only_public_info():
    conn = _make_conn()
    key = _fresh_key()
    expected_address = Account.from_key(key).address

    result = creds.save_credentials(conn, key)
    assert result["signer_address"] == expected_address
    assert result["funder_address"] == expected_address  # defaults to signer for a plain EOA
    assert "ciphertext" not in result
    assert key not in str(result)

    status = creds.get_status(conn)
    assert status["configured"] is True
    assert status["signer_address"] == expected_address
    assert key not in str(status)
    conn.close()


def test_get_status_when_nothing_saved():
    conn = _make_conn()
    assert creds.get_status(conn) == {"configured": False}
    conn.close()


def test_save_credentials_never_stores_plaintext_in_the_database():
    conn = _make_conn()
    key = _fresh_key()
    creds.save_credentials(conn, key)
    row = conn.execute("SELECT ciphertext FROM trading_credentials WHERE id=1").fetchone()
    assert key.encode() not in row["ciphertext"]
    assert key not in row["ciphertext"].decode("latin1")  # not even byte-embedded anywhere in the blob
    conn.close()


def test_load_private_key_round_trips_the_original_key():
    conn = _make_conn()
    key = _fresh_key()
    creds.save_credentials(conn, key)
    assert creds.load_private_key(conn) == key
    conn.close()


def test_load_private_key_returns_none_when_nothing_saved():
    conn = _make_conn()
    assert creds.load_private_key(conn) is None
    conn.close()


def test_save_credentials_with_explicit_funder_address_for_a_proxy_wallet():
    conn = _make_conn()
    key = _fresh_key()
    signer_address = Account.from_key(key).address
    proxy_funder = "0x000000000000000000000000000000000000fF"

    result = creds.save_credentials(conn, key, funder_address=proxy_funder, signature_type=1)
    assert result["signer_address"] == signer_address
    assert result["funder_address"] == proxy_funder
    status = creds.get_status(conn)
    assert status["signature_type"] == 1
    assert status["funder_address"] == proxy_funder
    conn.close()


def test_saving_new_credentials_replaces_the_old_ones():
    conn = _make_conn()
    key1 = _fresh_key()
    key2 = _fresh_key()
    creds.save_credentials(conn, key1)
    creds.save_credentials(conn, key2)
    assert creds.load_private_key(conn) == key2
    assert conn.execute("SELECT COUNT(*) AS n FROM trading_credentials").fetchone()["n"] == 1
    conn.close()


def test_clear_credentials_removes_the_row():
    conn = _make_conn()
    creds.save_credentials(conn, _fresh_key())
    creds.clear_credentials(conn)
    assert creds.get_status(conn) == {"configured": False}
    assert creds.load_private_key(conn) is None
    conn.close()


def test_clear_credentials_wipes_the_master_key_so_old_ciphertext_cant_be_recovered(tmp_path):
    """Even if a copy of the ciphertext survived somewhere (a stale backup),
    clearing credentials must make it permanently undecryptable."""
    conn = _make_conn()
    key = _fresh_key()
    creds.save_credentials(conn, key)
    row = conn.execute("SELECT ciphertext, key_source FROM trading_credentials WHERE id=1").fetchone()
    old_ciphertext, old_source = row["ciphertext"], row["key_source"]

    creds.clear_credentials(conn)

    # A fresh save now gets a NEW master key -- the old ciphertext must not
    # decrypt under whatever key exists now.
    creds.save_credentials(conn, _fresh_key())
    with pytest.raises(ValueError):
        crypto_store.decrypt(old_ciphertext, old_source)
    conn.close()
