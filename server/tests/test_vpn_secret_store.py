import base64
import secrets

import pytest

from app.modules.vpn.secret_store import (
    SecretStore,
    SecretStoreError,
    load_or_create_key_file,
)


def test_round_trip_uses_versioned_envelope_and_fresh_nonce():
    store = SecretStore([secrets.token_bytes(32)])
    first, second = store.encrypt("config"), store.encrypt("config")
    assert first.startswith("v1:") and first != second
    assert store.decrypt(first) == ("config", False)


def test_wrong_key_fails_closed_without_plaintext():
    envelope = SecretStore([secrets.token_bytes(32)]).encrypt("config")
    with pytest.raises(SecretStoreError) as raised:
        SecretStore([secrets.token_bytes(32)]).decrypt(envelope)
    assert raised.value.code == "decrypt-failed"


def test_previous_key_decrypts_and_requests_rotation():
    old, new = secrets.token_bytes(32), secrets.token_bytes(32)
    envelope = SecretStore([old]).encrypt("config")
    assert SecretStore([new, old]).decrypt(envelope) == ("config", True)


def test_key_file_is_created_once_and_reused(tmp_path):
    path = tmp_path / "system" / "vpn.key"
    first = load_or_create_key_file(path)
    assert load_or_create_key_file(path) == first
    assert len(first) == 32


def test_corrupt_key_file_is_never_overwritten(tmp_path):
    path = tmp_path / "vpn.key"
    path.write_text("dG9vLXNob3J0\n", encoding="ascii")
    with pytest.raises(SecretStoreError) as raised:
        load_or_create_key_file(path)
    assert raised.value.code == "invalid-key"
    assert path.read_text(encoding="ascii") == "dG9vLXNob3J0\n"


def test_env_key_takes_over_and_existing_file_key_only_decrypts(tmp_path):
    path = tmp_path / "vpn.key"
    file_store = SecretStore.from_settings(None, path)
    envelope = file_store.encrypt("config")
    env_key = base64.b64encode(secrets.token_bytes(32)).decode()
    env_store = SecretStore.from_settings(env_key, path)
    assert env_store.decrypt(envelope) == ("config", True)
    assert env_store.decrypt(env_store.encrypt("config")) == ("config", False)


def test_env_key_without_file_does_not_create_one(tmp_path):
    path = tmp_path / "vpn.key"
    SecretStore.from_settings(base64.b64encode(secrets.token_bytes(32)).decode(), path)
    assert not path.exists()
