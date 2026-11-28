import asyncio
import base64
import secrets
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.modules.relay.network_policy import DirectPolicy, VpnProxyPolicy
from app.modules.settings.schemas.internal import (
    VpnSettings,
    VpnSettingsUpdate,
    VpnStoredConfig,
)
from app.modules.vpn.manager import VpnManager, VpnState
from app.modules.vpn.proxy_supervisor import ProxyStartError
from app.modules.vpn.secret_store import SecretStore
from app.modules.vpn.wireguard_config import parse_wireguard_config

pytestmark = pytest.mark.anyio


def _wireguard_text(endpoint: str = "192.0.2.10:51820") -> str:
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    peer = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return (
        f"[Interface]\nPrivateKey = {base64.b64encode(raw).decode()}\nAddress = 10.2.0.2/32\n"
        f"DNS = 10.2.0.1\n\n[Peer]\nPublicKey = {base64.b64encode(peer).decode()}\n"
        f"AllowedIPs = 0.0.0.0/0\nEndpoint = {endpoint}\n"
    )


def _stored(store: SecretStore, config_id: str, endpoint: str = "192.0.2.10:51820", text: str | None = None):
    text = text or _wireguard_text(endpoint)
    return VpnStoredConfig(
        id=config_id,
        name=config_id,
        encrypted_config=store.encrypt(text),
        metadata=parse_wireguard_config(text).metadata(),
    )


def _enabled(*configs: VpnStoredConfig, auto_failover: bool = True) -> VpnSettings:
    return VpnSettings(
        vpn_enabled=True,
        configs=list(configs),
        active_config_id=configs[0].id if configs else None,
        auto_failover=auto_failover,
    )


class Relay:
    def __init__(self):
        self.policies = []

    def apply_network_policy(self, policy):
        self.policies.append(policy)


class Supervisor:
    """Hamis wireproxy: a `ready` lehet érték, vagy a futó konfigurációból döntő függvény."""

    def __init__(
        self,
        failures: int = 0,
        ready: bool | None | Callable[[str], bool] = True,
        fail_when: Callable[[str], bool] | None = None,
    ):
        self.failures = failures
        self.fail_when = fail_when
        self.configs = []
        self.exit = asyncio.Event()
        self.stopped = False
        self.ready = ready

    async def readiness(self):
        if callable(self.ready):
            return self.ready(self.configs[-1][0]) if self.configs else None
        return self.ready

    async def start(self, config_text, socks_port):
        if self.failures:
            self.failures -= 1
            raise ProxyStartError("exited")
        if self.fail_when is not None and self.fail_when(config_text):
            raise ProxyStartError("exited")
        self.exit = asyncio.Event()
        self.configs.append((config_text, socks_port))

    async def wait(self):
        await self.exit.wait()
        return 1

    async def stop(self):
        self.stopped = True
        self.exit.set()


def _manager(settings, supervisor, store, saved=None, fast_retries=3, handshake_timeout=30.0, failover_after=60.0):
    def load():
        if isinstance(settings, Exception):
            raise settings
        return settings

    relay = Relay()
    manager = VpnManager(
        relay,
        load_settings=load,
        save_settings=(saved.append if saved is not None else lambda _update: None),
        secret_store_factory=lambda: store,
        supervisor=supervisor,
        fast_retries=fast_retries,
        backoff_base=0.01,
        backoff_max=0.02,
        health_interval=0.01,
        handshake_timeout=handshake_timeout,
        failover_after=failover_after,
    )
    return manager, relay


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture
def store():
    return SecretStore([secrets.token_bytes(32)])


async def test_disabled_vpn_applies_direct_policy_and_never_starts_proxy(store):
    supervisor = Supervisor()
    manager, relay = _manager(VpnSettings(vpn_enabled=False), supervisor, store)
    await manager.start()
    assert relay.policies == [DirectPolicy()]
    assert manager.status.state == VpnState.DISABLED
    assert supervisor.configs == []
    await manager.aclose()


async def test_unreadable_settings_leave_relay_without_any_policy(store):
    manager, relay = _manager(RuntimeError("db down"), Supervisor(), store)
    await manager.start()
    assert relay.policies == []
    assert manager.status.state == VpnState.ERROR
    assert manager.status.error_code == "settings-unavailable"
    await manager.aclose()


async def test_enabled_without_config_holds_proxy_policy_in_error(store):
    supervisor = Supervisor()
    manager, relay = _manager(VpnSettings(vpn_enabled=True), supervisor, store)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.ERROR)
    assert len(relay.policies) == 1
    assert isinstance(relay.policies[0], VpnProxyPolicy)
    assert manager.status.error_code == "missing-config"
    assert supervisor.configs == []
    await manager.aclose()


async def test_connects_and_reconnects_after_proxy_exit_without_leaving_proxy_mode(store):
    supervisor = Supervisor()
    manager, relay = _manager(_enabled(_stored(store, "a")), supervisor, store)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert manager.status.active_config_id == "a"
    policy = relay.policies[0]
    config_text, port = supervisor.configs[0]
    assert port == policy.port
    assert f"BindAddress = 127.0.0.1:{policy.port}" in config_text
    assert f"Password = {policy.password}" in config_text

    supervisor.exit.set()
    await _until(lambda: len(supervisor.configs) == 2 and manager.status.state == VpnState.CONNECTED)
    assert relay.policies == [policy]
    await manager.aclose()
    assert supervisor.stopped


async def test_repeated_start_failures_end_in_error_but_never_direct(store):
    supervisor = Supervisor(failures=10)
    manager, relay = _manager(_enabled(_stored(store, "a")), supervisor, store, fast_retries=2)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.ERROR)
    assert manager.status.error_code == "exited"
    assert all(isinstance(policy, VpnProxyPolicy) for policy in relay.policies)
    await manager.aclose()


async def test_config_under_previous_key_is_reencrypted_with_current_key():
    old, new = secrets.token_bytes(32), secrets.token_bytes(32)
    text = _wireguard_text()
    settings = _enabled(_stored(SecretStore([old]), "a", text=text))
    saved: list[VpnSettingsUpdate] = []
    supervisor = Supervisor()
    manager, relay = _manager(settings, supervisor, SecretStore([new, old]), saved=saved)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert len(saved) == 1 and saved[0].configs is not None
    assert SecretStore([new]).decrypt(saved[0].configs[0].encrypted_config) == (text, False)
    await manager.aclose()


async def test_runtime_toggle_switches_policy_and_stops_the_proxy(store):
    supervisor = Supervisor()
    manager, relay = _manager(VpnSettings(vpn_enabled=False), supervisor, store)
    await manager.start()
    assert relay.policies == [DirectPolicy()]

    await manager.apply_settings(_enabled(_stored(store, "a")))
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert isinstance(relay.policies[-1], VpnProxyPolicy)
    assert manager.torrent_traffic_available()

    await manager.apply_settings(VpnSettings(vpn_enabled=False))
    assert supervisor.stopped
    assert relay.policies[-1] == DirectPolicy()
    assert manager.status.state == VpnState.DISABLED
    await manager.aclose()


async def test_connected_only_after_check_alive_replies_and_follows_the_tunnel(store):
    supervisor = Supervisor(ready=False)
    manager, _ = _manager(_enabled(_stored(store, "a")), supervisor, store, handshake_timeout=0.3)
    await manager.start()
    await _until(lambda: len(supervisor.configs) == 1)
    config_text = supervisor.configs[0][0]
    assert "CheckAlive = 10.2.0.1" in config_text
    assert manager.check_alive_configured
    # A proxy fut, de a tunnelen át még nincs válasz: nem CONNECTED, a forgalom áll.
    await asyncio.sleep(0.1)
    assert manager.status.state == VpnState.STARTING
    assert not manager.torrent_traffic_available()

    supervisor.ready = True
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert manager.torrent_traffic_available()

    supervisor.ready = False
    await _until(lambda: manager.status.state == VpnState.RECONNECTING)
    await _until(lambda: manager.status.state == VpnState.ERROR)
    assert manager.status.error_code == "no-handshake"

    supervisor.ready = True
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert len(supervisor.configs) == 1  # a wireproxy nem indult újra, csak az állapot követte
    await manager.aclose()


async def test_silent_server_fails_over_to_the_next_config_on_the_same_proxy(store):
    # F5: az "a" szervere nem válaszol, a "b"-é igen.
    supervisor = Supervisor(ready=lambda text: "192.0.2.11" in text)
    saved: list[VpnSettingsUpdate] = []
    settings = _enabled(_stored(store, "a"), _stored(store, "b", "192.0.2.11:51820"))
    manager, relay = _manager(settings, supervisor, store, saved=saved, handshake_timeout=0.1, failover_after=0.3)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)

    assert manager.status.active_config_id == "b"
    assert manager.status.failover_at is not None
    assert manager.status.metadata is not None and manager.status.metadata.endpoint == "192.0.2.11:51820"
    assert [update.active_config_id for update in saved] == ["b"]
    # Ugyanaz a proxyszabály (port, jelszó): a relay nem vált, közvetlen ablak nincs.
    assert len(relay.policies) == 1 and isinstance(relay.policies[0], VpnProxyPolicy)
    first, second = supervisor.configs[0][0], supervisor.configs[1][0]
    assert "Endpoint = 192.0.2.10:51820" in first and "Endpoint = 192.0.2.11:51820" in second
    assert supervisor.configs[0][1] == supervisor.configs[1][1] == relay.policies[0].port
    assert f"Password = {relay.policies[0].password}" in second
    await manager.aclose()


async def test_without_failover_the_manager_stays_on_the_silent_config(store):
    supervisor = Supervisor(ready=False)
    settings = _enabled(_stored(store, "a"), _stored(store, "b", "192.0.2.11:51820"), auto_failover=False)
    manager, _ = _manager(settings, supervisor, store, handshake_timeout=0.1, failover_after=0.2)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.ERROR)
    await asyncio.sleep(0.5)
    assert len(supervisor.configs) == 1
    assert manager.status.active_config_id == "a"
    assert manager.status.error_code == "no-handshake"
    await manager.aclose()


async def test_all_silent_servers_are_tried_in_turn(store):
    supervisor = Supervisor(ready=False)
    settings = _enabled(_stored(store, "a"), _stored(store, "b", "192.0.2.11:51820"))
    manager, relay = _manager(settings, supervisor, store, handshake_timeout=0.05, failover_after=0.1)
    await manager.start()
    await _until(lambda: len(supervisor.configs) >= 3)
    endpoints = ["192.0.2.11" in text for text, _ in supervisor.configs[:3]]
    assert endpoints == [False, True, False]
    assert not manager.torrent_traffic_available()
    assert all(isinstance(policy, VpnProxyPolicy) for policy in relay.policies)
    await manager.aclose()


async def test_config_that_cannot_start_hands_over_to_the_next(store):
    supervisor = Supervisor(fail_when=lambda text: "192.0.2.10" in text)
    settings = _enabled(_stored(store, "a"), _stored(store, "b", "192.0.2.11:51820"))
    manager, _ = _manager(settings, supervisor, store, fast_retries=2)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert manager.status.active_config_id == "b"
    await manager.aclose()


async def test_undecryptable_config_is_skipped_and_all_broken_ends_in_error(store):
    other = SecretStore([secrets.token_bytes(32)])
    supervisor = Supervisor()
    settings = _enabled(_stored(other, "a"), _stored(store, "b", "192.0.2.11:51820"))
    manager, _ = _manager(settings, supervisor, store)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert manager.status.active_config_id == "b"
    await manager.aclose()

    broken = _enabled(_stored(other, "a"), _stored(other, "b", "192.0.2.11:51820"))
    supervisor = Supervisor()
    manager, relay = _manager(broken, supervisor, store)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.ERROR)
    assert manager.status.error_code == "decrypt-failed"
    assert supervisor.configs == []
    assert all(isinstance(policy, VpnProxyPolicy) for policy in relay.policies)
    await manager.aclose()


def test_single_config_settings_migrate_and_stay_readable_by_older_builds(store):
    stored = _stored(store, "x")
    legacy = {
        "vpn_enabled": True,
        "encrypted_config": stored.encrypted_config,
        "config_metadata": stored.metadata.model_dump(mode="json"),
    }
    migrated = VpnSettings.model_validate(legacy)
    assert [config.id for config in migrated.configs] == ["legacy"]
    assert migrated.active_config() == migrated.configs[0]
    assert migrated.configs[0].encrypted_config == stored.encrypted_config

    two = migrated.model_copy(update={"configs": [*migrated.configs, _stored(store, "b")], "active_config_id": "b"})
    dump = two.storage_dump()
    # Egy korábbi (egykonfigos) build az aktív konfigurációt olvassa a régi mezőkből.
    assert dump["encrypted_config"] == two.configs[1].encrypted_config
    assert VpnSettings.model_validate(dump).configs == two.configs


def test_config_order_wraps_and_needs_a_second_config(store):
    a, b, c = _stored(store, "a"), _stored(store, "b"), _stored(store, "c")
    settings = _enabled(a, b, c)
    assert settings.config_after("a") == b
    assert settings.config_after("c") == a
    assert settings.config_after("missing") is None
    assert _enabled(a).config_after("a") is None
    assert VpnSettings(configs=[a, b], active_config_id="gone").active_config() == a
