import base64
import secrets
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.modules.relay.network_policy import DirectPolicy, VpnProxyPolicy
from app.modules.relay.service import RelayService
from app.modules.relay_settings.service import RelaySettingsService
from app.modules.settings.schemas.internal import (
    RelaySettings,
    RelaySettingsUpdate,
    VpnSettings,
    VpnSettingsUpdate,
)
from app.modules.stream.service import StreamService
from app.modules.vpn import state as vpn_state
from app.modules.vpn.manager import VpnManager, VpnState, VpnStatus
from app.modules.vpn.schemas import VpnUpdateRequest
from app.modules.vpn.secret_store import SecretStore
from app.modules.vpn.service import VpnService

pytestmark = pytest.mark.anyio

VPN = VpnProxyPolicy(port=41234, username="fixture-user", password="fixture-pass")


def _config() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    raw = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    peer = base64.b64encode(
        X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    text = (
        f"[Interface]\nPrivateKey = {raw}\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n"
        f"[Peer]\nPublicKey = {peer}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\n"
    )
    return text, raw


class Manager:
    def __init__(self):
        self.applied: list[VpnSettings] = []
        self.status = VpnStatus(state=VpnState.DISABLED)
        self.policy: VpnProxyPolicy | None = None

    async def apply_settings(self, settings: VpnSettings) -> None:
        self.applied.append(settings)


@pytest.fixture
def service():
    store: dict[str, VpnSettings] = {"vpn": VpnSettings()}

    def save(update: VpnSettingsUpdate) -> VpnSettings:
        store["vpn"] = VpnSettings.model_validate(
            {**store["vpn"].model_dump(), **update.model_dump(exclude_unset=True)}
        )
        return store["vpn"]

    manager = Manager()
    vpn_service = VpnService(
        cast(VpnManager, manager),
        load_settings=lambda: store["vpn"],
        save_settings=save,
        secret_store_factory=lambda: SecretStore([secrets.token_bytes(32)]),
        network_settings=lambda: {},
    )
    return SimpleNamespace(service=vpn_service, manager=manager, store=store)


async def test_import_stores_only_ciphertext_and_responses_never_echo_the_key(service):
    text, private = _config()
    response = await service.service.update(VpnUpdateRequest(config=text))
    stored = service.store["vpn"]
    assert stored.encrypted_config and stored.encrypted_config.startswith("v1:")
    assert private not in stored.model_dump_json()
    assert private not in response.model_dump_json(by_alias=True)
    assert response.has_config and not response.enabled
    assert response.metadata is not None and response.metadata.endpoint == "192.0.2.10:51820"
    assert len(service.manager.applied) == 1


async def test_invalid_config_is_rejected_without_echoing_it(service):
    text, private = _config()
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(config=text.replace("[Peer]", "[Nope]")))
    assert raised.value.status_code == 400
    assert private not in str(raised.value.detail)
    assert service.store["vpn"].encrypted_config is None


async def test_enabling_requires_a_config_and_disabling_requires_confirmation(service):
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(enabled=True))
    assert raised.value.status_code == 400

    text, _ = _config()
    enabled = await service.service.update(VpnUpdateRequest(config=text, enabled=True))
    assert enabled.enabled

    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(enabled=False))
    assert raised.value.status_code == 400
    assert service.store["vpn"].vpn_enabled

    with pytest.raises(HTTPException) as raised:
        await service.service.delete_config()
    assert raised.value.status_code == 409

    disabled = await service.service.update(VpnUpdateRequest(enabled=False, confirm_unprotected=True))
    assert not disabled.enabled
    assert [settings.vpn_enabled for settings in service.manager.applied] == [True, False]
    cleared = await service.service.delete_config()
    assert not cleared.has_config and cleared.metadata is None


def test_vpn_endpoints_require_an_admin_session():
    client = TestClient(app)
    assert client.get("/api/vpn").status_code in {401, 403}
    assert client.put("/api/vpn", json={"enabled": False}).status_code in {401, 403}


def _relay_settings_service(policy):
    settings_service = Mock()
    settings_service.get_relay.return_value = RelaySettings(port=6881, enable_utp=True)
    settings_service.save_relay.side_effect = lambda payload: RelaySettings(port=6881, enable_utp=True)
    relay = SimpleNamespace(network_policy=policy, update_settings=lambda payload: None)
    return RelaySettingsService(settings_service, cast(RelayService, relay)), settings_service


def test_vpn_mode_locks_network_fields_but_allows_the_rest():
    relay_settings, settings_service = _relay_settings_service(VPN)
    with pytest.raises(HTTPException) as raised:
        relay_settings.update_settings(RelaySettingsUpdate(port=7000))
    assert raised.value.status_code == 409
    with pytest.raises(HTTPException):
        relay_settings.update_settings(RelaySettingsUpdate(enable_utp=False))
    settings_service.save_relay.assert_not_called()

    relay_settings.update_settings(RelaySettingsUpdate(port=6881, connections_limit=300))
    response = relay_settings.to_response(relay_settings.get_settings())
    assert response.vpn_managed

    direct, _ = _relay_settings_service(DirectPolicy())
    direct.update_settings(RelaySettingsUpdate(port=7000))
    assert not direct.to_response(direct.get_settings()).vpn_managed


class _File:
    def __init__(self, local: bool):
        self.local = local
        self.size = 1000
        self.is_video = True
        self.is_subtitle = False

    def has_byte(self, position: int) -> bool:
        return self.local


def _stream_service(local: bool) -> StreamService:
    torrents = Mock()
    torrents.find_by_id.return_value = SimpleNamespace(info_hash="ab" * 20)
    relay = Mock()
    relay.get_torrent_file.return_value = _File(local)
    return StreamService(
        torrents_service=torrents,
        torrent_files_service=Mock(),
        indexers_service=Mock(),
        relay_service=relay,
        isolated_torrent_files_service=Mock(),
    )


@pytest.mark.parametrize(
    ("tunnel_up", "local", "blocked"),
    [(False, False, True), (False, True, False), (True, False, False)],
)
async def test_stream_answers_503_only_when_tunnel_is_down_and_data_is_not_local(tunnel_up, local, blocked):
    def provider() -> bool:
        return tunnel_up

    vpn_state.register(provider)
    try:
        service = _stream_service(local)
        if blocked:
            with pytest.raises(HTTPException) as raised:
                await service.prepare_for_stream("bytes=500-", "indexer", "torrent", 0)
            assert raised.value.status_code == 503
            assert raised.value.headers is not None and raised.value.headers["Retry-After"] == "5"
        else:
            parsed, _ = await service.prepare_for_stream("bytes=500-", "indexer", "torrent", 0)
            assert parsed.start_byte == 500
    finally:
        vpn_state.unregister(provider)
