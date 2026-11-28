"""A VPN-kezelő: hálózati szabály, állapotgép, a wireproxy felügyelete (VPN-terv Rev. 5, 6.4)
és az automatikus átállás a mentett konfigurációk között (F5).

Bekapcsolt VPN mellett a relay mindig proxymódban van, akkor is, ha a
konfiguráció hibás, a wireproxy nem fut, vagy a kezelő éppen másik konfigurációra
vált: ilyenkor a torrentforgalom áll, de soha nem vált közvetlen kapcsolatra
(fail-closed). Átálláskor a SOCKS-port és a hitelesítés nem változik, ezért a
relay szabálya sem.
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
    VpnStoredConfig,
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
    active_config_id: str | None = None
    failover_at: datetime.datetime | None = None


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
        failover_after: float = 60.0,
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
        self._failover_after = failover_after
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

    def _set(
        self, state: VpnState, error_code: str | None = None, *, stored: VpnStoredConfig | None = None
    ) -> None:
        connected_at = (
            datetime.datetime.now(datetime.UTC) if state == VpnState.CONNECTED else None
        )
        self._status = replace(
            self._status, state=state, error_code=error_code, connected_at=connected_at
        )
        if stored is not None:
            self._status = replace(self._status, metadata=stored.metadata, active_config_id=stored.id)

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
        active = settings.active_config()
        self._status = VpnStatus(
            state=VpnState.STARTING,
            metadata=active.metadata if active else None,
            active_config_id=active.id if active else None,
            failover_at=self._status.failover_at,
        )
        self._task = asyncio.create_task(self._run(active, policy), name="vpn-supervisor")

    async def _stop_supervision(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(task, return_exceptions=True)
        await self._supervisor.stop()

    def _decrypt_config(self, stored: VpnStoredConfig) -> WireGuardConfig:
        store = self._secret_store_factory()
        text, needs_rotation = store.decrypt(stored.encrypted_config)
        config = parse_wireguard_config(text)
        if needs_rotation:
            try:
                self._reencrypt(stored.id, store.encrypt(text))
            except Exception as error:
                logger.warning("operation=vpn-key-rotation kind=%s", type(error).__name__)
        return config

    def _reencrypt(self, config_id: str, envelope: str) -> None:
        current = self._load_settings()
        configs = [
            stored.model_copy(update={"encrypted_config": envelope}) if stored.id == config_id else stored
            for stored in current.configs
        ]
        self._save_settings(VpnSettingsUpdate(configs=configs))

    async def _render(self, stored: VpnStoredConfig, policy: VpnProxyPolicy) -> str:
        """A wireproxy futási konfigurációja; hibás kulcsnál vagy konfignál kivételt dob."""
        config = await owned_to_thread(self._decrypt_config, stored)
        targets = check_alive_targets(config, self._check_alive_override)
        self._check_alive_configured = bool(targets)
        return render_wireproxy_config(
            config,
            policy.port,
            policy.username,
            policy.password,
            check_alive=targets,
            check_alive_interval=self._check_alive_interval,
        )

    async def _next_config(self, current_id: str) -> VpnStoredConfig | None:
        """A következő konfiguráció friss beállításokból: a felhasználó közben bővíthette
        a listát vagy kikapcsolhatta az átállást. None, ha nincs hova váltani."""
        try:
            settings = await owned_to_thread(self._load_settings)
        except Exception as error:
            logger.warning("operation=vpn-failover reason=settings-unavailable kind=%s", type(error).__name__)
            return None
        if not settings.auto_failover:
            return None
        return settings.config_after(current_id)

    async def _record_failover(self, previous_id: str, stored: VpnStoredConfig, reason: str) -> None:
        logger.warning("operation=vpn-failover from=%s to=%s reason=%s", previous_id, stored.id, reason)
        self._status = replace(
            self._status,
            state=VpnState.RECONNECTING,
            error_code=None,
            connected_at=None,
            metadata=stored.metadata,
            active_config_id=stored.id,
            failover_at=datetime.datetime.now(datetime.UTC),
        )
        # Újraindulás után is a működő konfigurációval folytatja (nincs visszaugrálás).
        try:
            await owned_to_thread(self._save_settings, VpnSettingsUpdate(active_config_id=stored.id))
        except Exception as error:
            logger.warning("operation=vpn-failover-save kind=%s", type(error).__name__)

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_max, self._backoff_base * 2 ** max(0, attempt - 1))

    async def _run(self, stored: VpnStoredConfig | None, policy: VpnProxyPolicy) -> None:
        if stored is None:
            logger.error("operation=vpn-config reason=missing-config")
            self._set(VpnState.ERROR, "missing-config")
            return

        unusable: set[str] = set()
        fresh = True
        while not self._closing:
            try:
                runtime_config = await self._render(stored, policy)
            except (SecretStoreError, WireGuardConfigError) as error:
                logger.error("operation=vpn-config reason=%s config=%s", error.code, stored.id)
                unusable.add(stored.id)
                following = await self._next_config(stored.id)
                if following is None or following.id in unusable:
                    self._set(VpnState.ERROR, error.code, stored=stored)
                    return
                await self._record_failover(stored.id, following, error.code)
                stored, fresh = following, False
                continue
            except ValueError:
                logger.error("operation=vpn-config reason=invalid-check-alive")
                self._set(VpnState.ERROR, "invalid-check-alive", stored=stored)
                return

            switch = await self._serve(stored, runtime_config, policy, fresh=fresh)
            if switch is None:
                return
            following, reason = switch
            await self._record_failover(stored.id, following, reason)
            stored, fresh = following, False

    async def _serve(
        self, stored: VpnStoredConfig, runtime_config: str, policy: VpnProxyPolicy, *, fresh: bool
    ) -> tuple[VpnStoredConfig, str] | None:
        """Egy konfiguráció futtatása; visszatér, ha másikra kell váltani, vagy None leállításkor."""
        attempt = 0
        while not self._closing:
            self._set(VpnState.STARTING if fresh and attempt == 0 else VpnState.RECONNECTING, stored=stored)
            try:
                await self._supervisor.start(runtime_config, policy.port)
            except ProxyStartError as error:
                attempt += 1
                logger.warning("operation=vpn-proxy-start reason=%s attempt=%s", error.code, attempt)
                if attempt >= self._fast_retries:
                    following = await self._next_config(stored.id)
                    if following is not None:
                        return following, error.code
                    self._set(VpnState.ERROR, error.code, stored=stored)
                await asyncio.sleep(self._backoff(attempt))
                continue

            fresh = False
            attempt = 0
            while True:
                exit_code = await self._watch()
                if self._closing:
                    return None
                if exit_code is not None:
                    break
                following = await self._next_config(stored.id)
                if following is not None:
                    await self._supervisor.stop()
                    return following, "no-handshake"
                # Nincs másik konfiguráció, vagy ki van kapcsolva az átállás: tovább vár.

            attempt = 1
            logger.warning("operation=vpn-proxy-exit code=%s", exit_code)
            self._set(VpnState.RECONNECTING)
            await asyncio.sleep(self._backoff(attempt))
        return None

    async def _watch(self) -> int | None:
        """Az állapotot a wireproxy `/readyz` válasza adja (G6): CONNECTED csak akkor,
        ha a CheckAlive-címek a tunnelen át válaszolnak. A folyamat kilépésekor a
        kilépési kóddal tér vissza; ha `failover_after` ideig nincs válasz, None-nal
        (a hívó dönti el, van-e hova váltani)."""
        loop = asyncio.get_running_loop()
        exit_task = asyncio.ensure_future(self._supervisor.wait())
        last_ok = loop.time()
        try:
            while True:
                ready = await self._supervisor.readiness()
                silent_for = loop.time() - last_ok
                if ready:
                    last_ok = loop.time()
                    if self._status.state != VpnState.CONNECTED:
                        self._set(VpnState.CONNECTED)
                elif silent_for > self._failover_after:
                    return None
                elif silent_for > self._handshake_timeout:
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
