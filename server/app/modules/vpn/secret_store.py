"""A VPN-konfiguráció titkosított tárolása (VPN-terv Rev. 5, 6.6).

AES-256-GCM, verziózott boríték (`v1:`). A kulcs a `VPN_SECRET_KEY` környezeti
változóból jön, vagy az első induláskor generált `data/system/vpn.key` fájlból.
A kulcs szándékosan független a `SESSION_SECRET`-től.
"""

import base64
import binascii
import contextlib
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_PREFIX = "v1:"
KEY_FILENAME = "vpn.key"
_AAD = b"stremhu-vpn-config:v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class SecretStoreError(Exception):
    """Titokmentes hiba; a `code` a felület és a naplók számára."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _decode_key(text: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SecretStoreError("invalid-key") from None
    if len(raw) != _KEY_BYTES:
        raise SecretStoreError("invalid-key")
    return raw


def load_or_create_key_file(path: Path) -> bytes:
    """Atomikusan (`O_EXCL`, `0600`) létrehozott, tartós kulcsfájl. Sérült fájlt nem ír felül."""
    for _ in range(5):
        try:
            text = path.read_text(encoding="ascii")
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeDecodeError):
            raise SecretStoreError("key-file-unreadable") from None
        if text is not None and text.strip():
            return _decode_key(text)
        if text is not None:
            # Egy párhuzamos létrehozó még ír; ha üres marad, sérültnek tekintjük.
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        except OSError:
            raise SecretStoreError("key-file-unwritable") from None
        key = secrets.token_bytes(_KEY_BYTES)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(base64.b64encode(key).decode("ascii") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()
            raise SecretStoreError("key-file-unwritable") from None
        return key
    raise SecretStoreError("key-file-corrupt")


class SecretStore:
    """Az első kulccsal titkosít; visszafejtéskor minden megadott kulcsot kipróbál."""

    def __init__(self, keys: Sequence[bytes]):
        if not keys or any(len(key) != _KEY_BYTES for key in keys):
            raise SecretStoreError("invalid-key")
        self._keys = tuple(keys)

    @classmethod
    def from_settings(cls, env_key: str | None, key_file: Path) -> "SecretStore":
        """Környezeti kulcs esetén a meglévő kulcsfájl csak a rotációhoz (visszafejtéshez) kell."""
        if env_key:
            keys = [_decode_key(env_key)]
            if key_file.exists():
                file_key = load_or_create_key_file(key_file)
                if file_key != keys[0]:
                    keys.append(file_key)
            return cls(keys)
        return cls([load_or_create_key_file(key_file)])

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._keys[0]).encrypt(nonce, plaintext.encode("utf-8"), _AAD)
        return ENVELOPE_PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, envelope: str) -> tuple[str, bool]:
        """A nyílt szöveg és az, hogy újra kell-e titkosítani az aktuális kulccsal."""
        if not envelope.startswith(ENVELOPE_PREFIX):
            raise SecretStoreError("unknown-envelope")
        try:
            blob = base64.b64decode(envelope[len(ENVELOPE_PREFIX):], validate=True)
        except (binascii.Error, ValueError):
            raise SecretStoreError("corrupt-envelope") from None
        if len(blob) <= _NONCE_BYTES:
            raise SecretStoreError("corrupt-envelope")
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        for index, key in enumerate(self._keys):
            try:
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
            except InvalidTag:
                continue
            return plaintext.decode("utf-8"), index > 0
        raise SecretStoreError("decrypt-failed")
