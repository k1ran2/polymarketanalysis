"""
Storage for the ONE secret live trading needs: the wallet private key that
signs orders. This module is the only place in the codebase that ever sees
it in plaintext, and only for as long as one function call takes -- it's
encrypted (app.crypto_store) before it touches the database, and every
function here that returns anything returns derived PUBLIC information
(an address) or a boolean, never the key itself.

How the user connects: they generate or export a private key themselves,
from their OWN Polymarket account settings or wallet software, and paste it
into this app's own Settings page (their browser, talking to their own
local backend) -- never into a chat with an AI, never into any other
website. See TradingPage's connect-wallet copy for the exact warning about
using a DEDICATED wallet funded only with money they'd accept losing --
that's the real control here, encryption is secondary to it (see
crypto_store's module docstring for why).

This module does NOT do anything with the key beyond store/retrieve it.
Deriving L2 CLOB API credentials and constructing/signing/submitting orders
is a separate, not-yet-built module (app.clob_trader) -- see the trading
build order. Storing a key here has no effect on anything until that
exists.
"""
from __future__ import annotations

import time
from typing import Optional

from eth_account import Account

from . import crypto_store
from . import db


class InvalidPrivateKeyError(ValueError):
    pass


def derive_address(private_key: str) -> str:
    """The public wallet address for a private key -- never a secret, safe
    to store/return/display. Raises InvalidPrivateKeyError if the key
    doesn't parse (garbage input, wrong length, non-hex characters)."""
    try:
        return Account.from_key(private_key).address
    except Exception as e:
        raise InvalidPrivateKeyError(f"That doesn't look like a valid private key: {e}") from e


def save_credentials(
    conn, private_key: str, funder_address: Optional[str] = None, signature_type: int = 0, chain_id: int = 137,
) -> dict:
    """
    Encrypts and stores the private key, replacing whatever was there
    before. Returns ONLY public info (signer_address, funder_address) --
    never the key, encrypted or not.

    funder_address defaults to the signer's own address (signature_type 0,
    a plain EOA wallet with no proxy) -- pass it explicitly for a Polymarket
    proxy wallet (signature_type 1) or Gnosis Safe (2), where the address
    holding the money differs from the address that signs.
    """
    signer_address = derive_address(private_key)
    funder = funder_address or signer_address
    ciphertext, key_source = crypto_store.encrypt(private_key)
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT INTO trading_credentials
            (id, ciphertext, key_source, signature_type, signer_address, funder_address, chain_id, created_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ciphertext=excluded.ciphertext, key_source=excluded.key_source,
            signature_type=excluded.signature_type, signer_address=excluded.signer_address,
            funder_address=excluded.funder_address, chain_id=excluded.chain_id,
            created_at=excluded.created_at, last_verified_at=NULL, last_error=NULL,
            api_key_ciphertext=NULL, api_secret_ciphertext=NULL, api_passphrase_ciphertext=NULL
        """,
        (ciphertext, key_source, signature_type, signer_address, funder, chain_id, now_ts),
    )
    return {"signer_address": signer_address, "funder_address": funder, "configured": True}


def get_status(conn) -> dict:
    """Public info only -- signer/funder address, when connected, whether a
    private key is actually present. Never the key or its ciphertext."""
    row = conn.execute(
        "SELECT signature_type, signer_address, funder_address, chain_id, created_at, last_verified_at, last_error "
        "FROM trading_credentials WHERE id=1"
    ).fetchone()
    if row is None:
        return {"configured": False}
    return {
        "configured": True,
        "signature_type": row["signature_type"],
        "signer_address": row["signer_address"],
        "funder_address": row["funder_address"],
        "chain_id": row["chain_id"],
        "created_at": row["created_at"],
        "last_verified_at": row["last_verified_at"],
        "last_error": row["last_error"],
    }


def load_private_key(conn) -> Optional[str]:
    """Decrypts and returns the raw private key -- for internal use by the
    (not yet built) order-signing code ONLY. Never call this from an API
    route; nothing that returns to an HTTP response should ever hold this
    value."""
    row = conn.execute("SELECT ciphertext, key_source FROM trading_credentials WHERE id=1").fetchone()
    if row is None:
        return None
    return crypto_store.decrypt(row["ciphertext"], row["key_source"])


def clear_credentials(conn) -> None:
    """Removes the stored credentials AND wipes the master encryption key --
    so even a surviving backup of the database's ciphertext can never be
    decrypted again after this."""
    conn.execute("DELETE FROM trading_credentials WHERE id=1")
    crypto_store.wipe_master_key()
