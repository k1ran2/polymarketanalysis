"""
Symmetric encryption for secrets at rest -- the ONLY thing this module
knows about is bytes in, bytes out. app.trading_credentials is the layer
that knows what a private key or an API secret is; this module has no idea.

Threat model, stated plainly rather than implied: this protects a stolen
disk, a copied database file, or an old backup someone finds later. It does
NOT protect against malware running as the same user while the backend
process is live -- the backend has to decrypt unattended to sign orders, so
the key material is, by construction, reachable by anything that can run
code as you. The actual control against that is using a dedicated wallet
funded only with money you'd accept losing (see the Trading page's
connect-wallet copy) -- encryption is real but secondary to that.

Key storage, in preference order:
  1. OS keychain (via the `keyring` package, if installed and available) --
     gets the key out of the project directory entirely, so it's not in a
     folder you might zip, `git add .`, or sync to cloud storage.
  2. A local key file (data/.trading_master_key, mode 0600) -- the fallback
     when no OS keychain is available (e.g. some Linux setups, CI).
A passphrase-derived mode (nothing at rest, entered once at process start,
held only in memory -- the strongest option, since a stolen laptop at rest
yields nothing) is intentionally NOT built yet: it means the bot can't
restart unattended, which is a real product trade-off worth the user
deciding on explicitly rather than defaulting to. keyfile/keyring both auto-
restart cleanly, which is why one of them is the default.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_KEYFILE_PATH = Path(__file__).resolve().parent.parent / "data" / ".trading_master_key"
_KEYRING_SERVICE = "polymarket-dashboard"
_KEYRING_USERNAME = "trading-master-key"


def _try_keyring_get() -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        return None  # keyring not installed, no backend available, or locked -- fall back


def _try_keyring_set(value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, value)
        return True
    except Exception:
        return False


def get_or_create_master_key() -> tuple:
    """Returns (key_bytes, source) where source is 'keyring' or 'keyfile' --
    trading_credentials stores `source` alongside the ciphertext so decrypt
    knows where to look without probing both every time."""
    existing = _try_keyring_get()
    if existing:
        return existing.encode(), "keyring"

    if _KEYFILE_PATH.exists():
        key = _KEYFILE_PATH.read_text().strip()
        if key:
            return key.encode(), "keyfile"

    new_key = Fernet.generate_key().decode()
    if _try_keyring_set(new_key):
        return new_key.encode(), "keyring"

    _KEYFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEYFILE_PATH.write_text(new_key)
    _KEYFILE_PATH.chmod(0o600)
    return new_key.encode(), "keyfile"


def _load_key(source: str) -> bytes:
    if source == "keyring":
        key = _try_keyring_get()
        if not key:
            raise RuntimeError("Master key was stored in the OS keychain but can't be read back now.")
        return key.encode()
    if _KEYFILE_PATH.exists():
        return _KEYFILE_PATH.read_text().strip().encode()
    raise RuntimeError("Master key file is missing.")


def encrypt(plaintext: str) -> tuple:
    """Returns (ciphertext_bytes, key_source) -- store both; decrypt needs
    key_source to know where the key living."""
    key, source = get_or_create_master_key()
    return Fernet(key).encrypt(plaintext.encode()), source


def decrypt(ciphertext: bytes, key_source: str) -> str:
    key = _load_key(key_source)
    try:
        return Fernet(key).decrypt(ciphertext).decode()
    except InvalidToken as e:
        raise ValueError("Could not decrypt -- wrong or corrupted master key.") from e


def wipe_master_key() -> None:
    """Removes the master key from wherever it lives -- called when the
    user disconnects/clears their credentials, so old ciphertext (if a
    backup of the DB survives somewhere) can never be decrypted again."""
    try:
        import keyring
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        pass
    if _KEYFILE_PATH.exists():
        _KEYFILE_PATH.unlink()
