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
    MAX_VPN_CONFIGS,
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


def _config(endpoint: str = "192.0.2.10:51820") -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    raw = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    peer = base64.b64encode(
        X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    text = (
        f"[Interface]\nPrivateKey = {raw}\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n"
        f"[Peer]\nPublicKey = {peer}\nAllowedIPs = 0.0.0.0/0\nEndpoint = {endpoint}\n"
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
    key = secrets.token_bytes(32)

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
        secret_store_factory=lambda: SecretStore([key]),
        network_settings=lambda: {},
    )
    return SimpleNamespace(service=vpn_service, manager=manager, store=store)


async def test_import_stores_only_ciphertext_and_responses_never_echo_the_key(service):
    text, private = _config()
    response = await service.service.update(VpnUpdateRequest(config=text, name="  Budapest\n#35 "))
    stored = service.store["vpn"]
    assert len(stored.configs) == 1 and stored.configs[0].encrypted_config.startswith("v1:")
    assert stored.active_config_id == stored.configs[0].id
    assert private not in stored.model_dump_json()
    assert private not in response.model_dump_json(by_alias=True)
    assert response.has_config and not response.enabled
    assert response.configs[0].name == "Budapest #35"
    assert response.active_config_id == stored.configs[0].id
    assert response.metadata is not None and response.metadata.endpoint == "192.0.2.10:51820"
    # Kikapcsolt VPN mellett a feltöltés nem indítja el a tunnelt.
    assert service.manager.applied == []


async def test_invalid_config_is_rejected_without_echoing_it(service):
    text, private = _config()
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(config=text.replace("[Peer]", "[Nope]")))
    assert raised.value.status_code == 400
    assert private not in str(raised.value.detail)
    assert service.store["vpn"].configs == []


async def test_enabling_requires_a_config_and_disabling_requires_confirmation(service):
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(enabled=True))
    assert raised.value.status_code == 400

    text, _ = _config()
    enabled = await service.service.update(VpnUpdateRequest(config=text, enabled=True))
    assert enabled.enabled
    config_id = enabled.configs[0].id

    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(enabled=False))
    assert raised.value.status_code == 400
    assert service.store["vpn"].vpn_enabled

    with pytest.raises(HTTPException) as raised:
        await service.service.delete_config(config_id)
    assert raised.value.status_code == 409

    disabled = await service.service.update(VpnUpdateRequest(enabled=False, confirm_unprotected=True))
    assert not disabled.enabled
    assert [settings.vpn_enabled for settings in service.manager.applied] == [True, False]
    cleared = await service.service.delete_config(config_id)
    assert not cleared.has_config and cleared.metadata is None and cleared.active_config_id is None


async def test_more_configs_extend_the_failover_list_without_dropping_the_tunnel(service):
    first, _ = _config()
    await service.service.update(VpnUpdateRequest(config=first, enabled=True))
    service.manager.status = VpnStatus(state=VpnState.CONNECTED, active_config_id=service.store["vpn"].configs[0].id)
    applied = len(service.manager.applied)

    second, _ = _config("192.0.2.11:51820")
    response = await service.service.update(VpnUpdateRequest(config=second))
    assert [config.name for config in response.configs] == ["Konfiguráció 1", "Konfiguráció 2"]
    assert response.active_config_id == response.configs[0].id
    assert len(service.manager.applied) == applied  # működő kapcsolat nem szakad meg

    # Ugyanaz a konfiguráció újra: a helyén frissül, nem lesz duplikátum.
    again = await service.service.update(VpnUpdateRequest(config=second, name="Bécs"))
    assert [config.name for config in again.configs] == ["Konfiguráció 1", "Bécs"]

    ids = [config.id for config in again.configs]
    reordered = await service.service.update(VpnUpdateRequest(order=list(reversed(ids)), auto_failover=False))
    assert [config.id for config in reordered.configs] == list(reversed(ids))
    assert not reordered.auto_failover
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(order=ids[:1]))
    assert raised.value.status_code == 400

    switched = await service.service.update(VpnUpdateRequest(active_config_id=ids[1]))
    assert service.store["vpn"].active_config_id == ids[1]
    assert service.manager.applied[-1].active_config_id == ids[1]
    assert len(service.manager.applied) == applied + 1
    assert switched.configs[0].id == ids[1]
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(active_config_id="nincs"))
    assert raised.value.status_code == 404


async def test_upload_during_an_error_becomes_active_at_once(service):
    first, _ = _config()
    await service.service.update(VpnUpdateRequest(config=first, enabled=True))
    service.manager.status = VpnStatus(state=VpnState.ERROR, error_code="decrypt-failed")
    second, _ = _config("192.0.2.11:51820")
    response = await service.service.update(VpnUpdateRequest(config=second))
    assert service.store["vpn"].active_config_id == response.configs[1].id
    assert service.manager.applied[-1].active_config_id == response.configs[1].id


async def test_deleting_the_active_config_moves_to_the_next(service):
    first, _ = _config()
    second, _ = _config("192.0.2.11:51820")
    await service.service.update(VpnUpdateRequest(config=first, enabled=True))
    response = await service.service.update(VpnUpdateRequest(config=second))
    first_id, second_id = (config.id for config in response.configs)
    applied = len(service.manager.applied)

    remaining = await service.service.delete_config(first_id)
    assert [config.id for config in remaining.configs] == [second_id]
    assert service.store["vpn"].active_config_id == second_id
    assert len(service.manager.applied) == applied + 1
    with pytest.raises(HTTPException) as raised:
        await service.service.delete_config("nincs")
    assert raised.value.status_code == 404


async def test_the_number_of_saved_configs_is_limited(service):
    for index in range(MAX_VPN_CONFIGS):
        text, _ = _config(f"192.0.2.{index + 1}:51820")
        await service.service.update(VpnUpdateRequest(config=text))
    text, _ = _config("198.51.100.1:51820")
    with pytest.raises(HTTPException) as raised:
        await service.service.update(VpnUpdateRequest(config=text))
    assert raised.value.status_code == 400
    assert len(service.store["vpn"].configs) == MAX_VPN_CONFIGS


def test_vpn_endpoints_require_an_admin_session():
    client = TestClient(app)
    assert client.get("/api/vpn").status_code in {401, 403}
    assert client.put("/api/vpn", json={"enabled": False}).status_code in {401, 403}
    assert client.delete("/api/vpn/configs/abc").status_code in {401, 403}


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
