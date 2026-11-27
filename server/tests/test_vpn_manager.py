import asyncio
import base64
import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.modules.relay.network_policy import DirectPolicy, VpnProxyPolicy
from app.modules.settings.schemas.internal import VpnSettings, VpnSettingsUpdate
from app.modules.vpn.manager import VpnManager, VpnState
from app.modules.vpn.proxy_supervisor import ProxyStartError
from app.modules.vpn.secret_store import SecretStore

pytestmark = pytest.mark.anyio


def _wireguard_text() -> str:
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    peer = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return (
        f"[Interface]\nPrivateKey = {base64.b64encode(raw).decode()}\nAddress = 10.2.0.2/32\n"
        f"DNS = 10.2.0.1\n\n[Peer]\nPublicKey = {base64.b64encode(peer).decode()}\n"
        "AllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\n"
    )


class Relay:
    def __init__(self):
        self.policies = []

    def apply_network_policy(self, policy):
        self.policies.append(policy)


class Supervisor:
    def __init__(self, failures=0, ready=True):
        self.failures = failures
        self.configs = []
        self.exit = asyncio.Event()
        self.stopped = False
        self.ready: bool | None = ready

    async def readiness(self):
        return self.ready

    async def start(self, config_text, socks_port):
        if self.failures:
            self.failures -= 1
            raise ProxyStartError("exited")
        self.exit = asyncio.Event()
        self.configs.append((config_text, socks_port))

    async def wait(self):
        await self.exit.wait()
        return 1

    async def stop(self):
        self.stopped = True
        self.exit.set()


def _manager(settings, supervisor, store, saved=None, fast_retries=3, handshake_timeout=30.0):
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
    settings = VpnSettings(vpn_enabled=True, encrypted_config=store.encrypt(_wireguard_text()))
    manager, relay = _manager(settings, supervisor, store)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
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
    settings = VpnSettings(vpn_enabled=True, encrypted_config=store.encrypt(_wireguard_text()))
    manager, relay = _manager(settings, supervisor, store, fast_retries=2)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.ERROR)
    assert manager.status.error_code == "exited"
    assert all(isinstance(policy, VpnProxyPolicy) for policy in relay.policies)
    await manager.aclose()


async def test_config_under_previous_key_is_reencrypted_with_current_key():
    old, new = secrets.token_bytes(32), secrets.token_bytes(32)
    text = _wireguard_text()
    settings = VpnSettings(vpn_enabled=True, encrypted_config=SecretStore([old]).encrypt(text))
    saved: list[VpnSettingsUpdate] = []
    supervisor = Supervisor()
    manager, relay = _manager(settings, supervisor, SecretStore([new, old]), saved=saved)
    await manager.start()
    await _until(lambda: manager.status.state == VpnState.CONNECTED)
    assert len(saved) == 1 and saved[0].encrypted_config is not None
    assert SecretStore([new]).decrypt(saved[0].encrypted_config) == (text, False)
    await manager.aclose()


async def test_runtime_toggle_switches_policy_and_stops_the_proxy(store):
    supervisor = Supervisor()
    enabled = VpnSettings(vpn_enabled=True, encrypted_config=store.encrypt(_wireguard_text()))
    manager, relay = _manager(VpnSettings(vpn_enabled=False), supervisor, store)
    await manager.start()
    assert relay.policies == [DirectPolicy()]

    await manager.apply_settings(enabled)
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
    settings = VpnSettings(vpn_enabled=True, encrypted_config=store.encrypt(_wireguard_text()))
    manager, _ = _manager(settings, supervisor, store, handshake_timeout=0.3)
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
