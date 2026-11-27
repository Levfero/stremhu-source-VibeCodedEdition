"""A VPN-kezelő: hálózati szabály, állapotgép és a wireproxy felügyelete (VPN-terv Rev. 5, 6.4).

Bekapcsolt VPN mellett a relay mindig proxymódban van, akkor is, ha a
konfiguráció hibás vagy a wireproxy nem fut: ilyenkor a torrentforgalom áll,
de soha nem vált közvetlen kapcsolatra (fail-closed).
"""

import asyncio
import contextlib
import datetime
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from app.common.async_utils import owned_to_thread
from app.common.logger import logger
from app.modules.relay.network_policy import DirectPolicy, NetworkPolicy, VpnProxyPolicy
from app.modules.settings.schemas.internal import (
    VpnConfigMetadata,
    VpnSettings,
    VpnSettingsUpdate,
)
from app.modules.vpn import state as vpn_state
from app.modules.vpn.proxy_supervisor import ProxyStartError, reserve_loopback_port
from app.modules.vpn.secret_store import SecretStore, SecretStoreError
from app.modules.vpn.wireguard_config import (
    WireGuardConfig,
    WireGuardConfigError,
    check_alive_targets,
    parse_wireguard_config,
    render_wireproxy_config,
)


class VpnState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass(frozen=True)
class VpnStatus:
    state: VpnState
    error_code: str | None = None
    connected_at: datetime.datetime | None = None
    metadata: VpnConfigMetadata | None = None


class NetworkPolicyTarget(Protocol):
    def apply_network_policy(self, policy: NetworkPolicy) -> None: ...


class Supervisor(Protocol):
    async def start(self, config_text: str, socks_port: int) -> None: ...

    async def wait(self) -> int: ...

    async def readiness(self) -> bool | None: ...

    async def stop(self) -> None: ...


class VpnManager:
    def __init__(
        self,
        relay: NetworkPolicyTarget,
        *,
        load_settings: Callable[[], VpnSettings],
        save_settings: Callable[[VpnSettingsUpdate], object],
        secret_store_factory: Callable[[], SecretStore],
        supervisor: Supervisor,
        fast_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        check_alive: list[str] | None = None,
        check_alive_interval: int = 5,
        health_interval: float = 2.0,
        handshake_timeout: float = 30.0,
    ):
        self._relay = relay
        self._load_settings = load_settings
        self._save_settings = save_settings
        self._secret_store_factory = secret_store_factory
        self._supervisor = supervisor
        self._fast_retries = fast_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._check_alive_override = check_alive or []
        self._check_alive_interval = check_alive_interval
        self._health_interval = health_interval
        self._handshake_timeout = handshake_timeout
        self._check_alive_configured = False
        self._status = VpnStatus(state=VpnState.DISABLED)
        self._policy: VpnProxyPolicy | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._change_lock = asyncio.Lock()

    def torrent_traffic_available(self) -> bool:
        return self._status.state in (VpnState.DISABLED, VpnState.CONNECTED)

    @property
    def check_alive_configured(self) -> bool:
        """Igaz, ha a CONNECTED állapot a tunnelen át érkező válaszon alapul (G6)."""
        return self._check_alive_configured

    async def readiness(self) -> bool | None:
        return await self._supervisor.readiness()

    @property
    def policy(self) -> VpnProxyPolicy | None:
        return self._policy

    @property
    def status(self) -> VpnStatus:
        return self._status

    def _set(self, state: VpnState, error_code: str | None = None) -> None:
        connected_at = (
            datetime.datetime.now(datetime.UTC) if state == VpnState.CONNECTED else None
        )
        self._status = replace(
            self._status, state=state, error_code=error_code, connected_at=connected_at
        )

    async def start(self) -> None:
        """Alkalmazza a hálózati szabályt; csak utána kerülhet torrent a relaybe."""
        vpn_state.register(self.torrent_traffic_available)
        try:
            settings = await owned_to_thread(self._load_settings)
        except Exception as error:
            # Nem tudjuk, kell-e VPN: a relay szabály nélkül marad, nem indul torrent.
            logger.error("operation=vpn-start reason=settings-unavailable kind=%s", type(error).__name__)
            self._set(VpnState.ERROR, "settings-unavailable")
            return

        await self._apply(settings)

    async def apply_settings(self, settings: VpnSettings) -> None:
        """Futás közbeni váltás (L4): a beállítás mentése után, felhasználói műveletre."""
        async with self._change_lock:
            await self._stop_supervision()
            await self._apply(settings)

    async def _apply(self, settings: VpnSettings) -> None:
        if not settings.vpn_enabled:
            self._relay.apply_network_policy(DirectPolicy())
            self._policy = None
            self._status = VpnStatus(state=VpnState.DISABLED)
            return

        policy = self._policy or VpnProxyPolicy(
            port=reserve_loopback_port(),
            username=secrets.token_urlsafe(16),
            password=secrets.token_urlsafe(24),
        )
        # A proxyszabály még a wireproxy indulása előtt érvényes: addig a forgalom áll.
        self._relay.apply_network_policy(policy)
        self._policy = policy
        self._status = VpnStatus(state=VpnState.STARTING, metadata=settings.config_metadata)
        self._task = asyncio.create_task(self._run(settings, policy), name="vpn-supervisor")

    async def _stop_supervision(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(task, return_exceptions=True)
        await self._supervisor.stop()

    def _decrypt_config(self, settings: VpnSettings) -> WireGuardConfig:
        if not settings.encrypted_config:
            raise WireGuardConfigError("missing-config")
        store = self._secret_store_factory()
        text, needs_rotation = store.decrypt(settings.encrypted_config)
        config = parse_wireguard_config(text)
        if needs_rotation:
            try:
                self._save_settings(VpnSettingsUpdate(encrypted_config=store.encrypt(text)))
            except Exception as error:
                logger.warning("operation=vpn-key-rotation kind=%s", type(error).__name__)
        return config

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_max, self._backoff_base * 2 ** max(0, attempt - 1))

    async def _run(self, settings: VpnSettings, policy: VpnProxyPolicy) -> None:
        try:
            config = await owned_to_thread(self._decrypt_config, settings)
        except (SecretStoreError, WireGuardConfigError) as error:
            logger.error("operation=vpn-config reason=%s", error.code)
            self._set(VpnState.ERROR, error.code)
            return

        try:
            targets = check_alive_targets(config, self._check_alive_override)
        except ValueError:
            logger.error("operation=vpn-config reason=invalid-check-alive")
            self._set(VpnState.ERROR, "invalid-check-alive")
            return
        self._check_alive_configured = bool(targets)
        runtime_config = render_wireproxy_config(
            config,
            policy.port,
            policy.username,
            policy.password,
            check_alive=targets,
            check_alive_interval=self._check_alive_interval,
        )
        attempt = 0
        while not self._closing:
            self._set(VpnState.STARTING if attempt == 0 else VpnState.RECONNECTING)
            try:
                await self._supervisor.start(runtime_config, policy.port)
            except ProxyStartError as error:
                attempt += 1
                logger.warning("operation=vpn-proxy-start reason=%s attempt=%s", error.code, attempt)
                if attempt >= self._fast_retries:
                    self._set(VpnState.ERROR, error.code)
                await asyncio.sleep(self._backoff(attempt))
                continue

            attempt = 0
            exit_code = await self._watch()
            if self._closing:
                break
            attempt = 1
            logger.warning("operation=vpn-proxy-exit code=%s", exit_code)
            self._set(VpnState.RECONNECTING)
            await asyncio.sleep(self._backoff(attempt))

    async def _watch(self) -> int:
        """Az állapotot a wireproxy `/readyz` válasza adja (G6): CONNECTED csak akkor,
        ha a CheckAlive-címek a tunnelen át válaszolnak. Visszatér, ha a folyamat kilép."""
        loop = asyncio.get_running_loop()
        exit_task = asyncio.ensure_future(self._supervisor.wait())
        last_ok = loop.time()
        try:
            while True:
                ready = await self._supervisor.readiness()
                if ready:
                    last_ok = loop.time()
                    if self._status.state != VpnState.CONNECTED:
                        self._set(VpnState.CONNECTED)
                elif loop.time() - last_ok > self._handshake_timeout:
                    if self._status.state != VpnState.ERROR:
                        logger.warning("operation=vpn-health reason=no-handshake")
                        self._set(VpnState.ERROR, "no-handshake")
                elif self._status.state == VpnState.CONNECTED:
                    self._set(VpnState.RECONNECTING)
                done, _ = await asyncio.wait({exit_task}, timeout=self._health_interval)
                if done:
                    return exit_task.result()
        finally:
            if not exit_task.done():
                exit_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await exit_task

    async def aclose(self) -> None:
        """A relay natív sessionje után áll le, így leállításkor sincs közvetlen ablak."""
        self._closing = True
        vpn_state.unregister(self.torrent_traffic_available)
        await self._stop_supervision()
