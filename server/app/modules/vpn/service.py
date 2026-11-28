"""A VPN admin-műveletei (VPN-terv Rev. 5, 6.5): import, be/kikapcsolás, törlés, diagnosztika."""

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from dataclasses import asdict

from fastapi import HTTPException

from app.common.async_utils import owned_to_thread
from app.common.logger import logger
from app.modules.relay.network_policy import VpnProxyPolicy
from app.modules.settings.schemas.internal import (
    MAX_VPN_CONFIGS,
    VpnConfigMetadata,
    VpnSettings,
    VpnSettingsUpdate,
    VpnStoredConfig,
)
from app.modules.vpn.manager import VpnManager, VpnState
from app.modules.vpn.schemas import (
    MAX_VPN_CONFIG_NAME,
    VpnCheck,
    VpnConfigResponse,
    VpnMetadataResponse,
    VpnResponse,
    VpnSpeedTestResponse,
    VpnStatusResponse,
    VpnTestResponse,
    VpnUpdateRequest,
)
from app.modules.vpn.secret_store import SecretStore, SecretStoreError
from app.modules.vpn.speedtest import SocksAuth, SpeedTestError, VpnSpeedTest
from app.modules.vpn.wireguard_config import (
    WireGuardConfigError,
    parse_wireguard_config,
)


def _metadata(metadata: VpnConfigMetadata) -> VpnMetadataResponse:
    return VpnMetadataResponse(**metadata.model_dump())


def _same_config(left: VpnConfigMetadata, right: VpnConfigMetadata) -> bool:
    return (
        left.client_public_key_fingerprint == right.client_public_key_fingerprint
        and left.peer_public_key == right.peer_public_key
        and left.endpoint == right.endpoint
    )


class VpnService:
    def __init__(
        self,
        manager: VpnManager,
        *,
        load_settings: Callable[[], VpnSettings],
        save_settings: Callable[[VpnSettingsUpdate], VpnSettings],
        secret_store_factory: Callable[[], SecretStore],
        network_settings: Callable[[], dict[str, object]],
        speed_test_factory: Callable[[], VpnSpeedTest] | None = None,
        speed_test_lock: asyncio.Lock | None = None,
    ):
        self._manager = manager
        self._load = load_settings
        self._save = save_settings
        self._secret_store_factory = secret_store_factory
        self._network_settings = network_settings
        self._speed_test_factory = speed_test_factory
        self._speed_test_lock = speed_test_lock or asyncio.Lock()

    def _status(self) -> VpnStatusResponse:
        status = self._manager.status
        return VpnStatusResponse(
            state=status.state,
            error_code=status.error_code,
            connected_at=status.connected_at,
            active_config_id=status.active_config_id,
            failover_at=status.failover_at,
        )

    def _response(self, settings: VpnSettings) -> VpnResponse:
        status = self._status()
        saved_active = settings.active_config()
        # Bekapcsolt VPN mellett a ténylegesen futó konfiguráció, különben amelyikkel indulna.
        active_id = (status.active_config_id if settings.vpn_enabled else None) or (
            saved_active.id if saved_active else None
        )
        active = next((stored for stored in settings.configs if stored.id == active_id), None)
        return VpnResponse(
            **status.model_dump(exclude={"active_config_id"}),
            active_config_id=active.id if active else None,
            enabled=settings.vpn_enabled,
            has_config=bool(settings.configs),
            metadata=_metadata(active.metadata) if active else None,
            configs=[
                VpnConfigResponse(id=stored.id, name=stored.name, metadata=_metadata(stored.metadata))
                for stored in settings.configs
            ],
            auto_failover=settings.auto_failover,
        )

    async def get(self) -> VpnResponse:
        return self._response(await owned_to_thread(self._load))

    def status(self) -> VpnStatusResponse:
        return self._status()

    def _encrypt(self, config_text: str) -> tuple[str, VpnConfigMetadata]:
        try:
            config = parse_wireguard_config(config_text)
        except WireGuardConfigError as error:
            raise HTTPException(400, f"Érvénytelen WireGuard-konfiguráció ({error}).") from None
        try:
            envelope = self._secret_store_factory().encrypt(config_text)
        except SecretStoreError as error:
            logger.error("operation=vpn-config-store reason=%s", error.code)
            raise HTTPException(500, "A VPN-konfiguráció titkosított tárolása nem sikerült.") from None
        return envelope, config.metadata()

    def _add_config(
        self, configs: list[VpnStoredConfig], config_text: str, name: str | None
    ) -> VpnStoredConfig:
        """Hozzáadja a listához, vagy ugyanazt a konfigurációt (kulcs, szerver) frissíti a helyén:
        így egy elveszett titkosítókulcs után az újrafeltöltés helyreállítja a régit."""
        envelope, metadata = self._encrypt(config_text)
        clean_name = " ".join((name or "").split())[:MAX_VPN_CONFIG_NAME]
        for index, stored in enumerate(configs):
            if _same_config(stored.metadata, metadata):
                configs[index] = stored.model_copy(
                    update={"encrypted_config": envelope, "metadata": metadata, "name": clean_name or stored.name}
                )
                return configs[index]
        if len(configs) >= MAX_VPN_CONFIGS:
            raise HTTPException(400, f"Legfeljebb {MAX_VPN_CONFIGS} VPN-konfiguráció menthető.")
        ids = {stored.id for stored in configs}
        config_id = secrets.token_hex(4)
        while config_id in ids:
            config_id = secrets.token_hex(4)
        stored = VpnStoredConfig(
            id=config_id,
            name=clean_name or f"Konfiguráció {len(configs) + 1}",
            encrypted_config=envelope,
            metadata=metadata,
        )
        configs.append(stored)
        return stored

    async def update(self, payload: VpnUpdateRequest) -> VpnResponse:
        current = await owned_to_thread(self._load)
        configs = list(current.configs)
        saved_active = current.active_config()
        active_id = saved_active.id if saved_active else None
        update = VpnSettingsUpdate()
        restart = False

        if payload.config is not None:
            added = self._add_config(configs, payload.config, payload.name)
            update.configs = configs
            # Első konfiguráció, vagy hibaállapotból (pl. elveszett kulcs, elérhetetlen
            # szerver) az új azonnal aktív lesz; működő kapcsolatot nem szakít meg.
            if active_id is None or (current.vpn_enabled and self._manager.status.state == VpnState.ERROR):
                active_id = added.id
                update.active_config_id = added.id
                restart = True

        if payload.order is not None:
            by_id = {stored.id: stored for stored in configs}
            if len(payload.order) != len(by_id) or set(payload.order) != set(by_id):
                raise HTTPException(400, "A sorrendnek pontosan a mentett konfigurációkat kell tartalmaznia.")
            configs = [by_id[config_id] for config_id in payload.order]
            update.configs = configs

        if payload.active_config_id is not None and payload.active_config_id != active_id:
            if payload.active_config_id not in {stored.id for stored in configs}:
                raise HTTPException(404, "Nincs ilyen VPN-konfiguráció.")
            update.active_config_id = payload.active_config_id
            restart = True

        if payload.auto_failover is not None:
            update.auto_failover = payload.auto_failover

        if payload.enabled is True and not configs:
            raise HTTPException(400, "Előbb tölts fel egy WireGuard-konfigurációt.")
        if payload.enabled is False and current.vpn_enabled and not payload.confirm_unprotected:
            raise HTTPException(
                400,
                "A kikapcsolás után a torrentforgalom VPN nélkül megy; ehhez megerősítés kell.",
            )
        if payload.enabled is not None:
            update.vpn_enabled = payload.enabled

        saved = await owned_to_thread(self._save, update)
        if saved.vpn_enabled != current.vpn_enabled or (saved.vpn_enabled and restart):
            await self._manager.apply_settings(saved)
        return self._response(saved)

    async def delete_config(self, config_id: str) -> VpnResponse:
        current = await owned_to_thread(self._load)
        if config_id not in {stored.id for stored in current.configs}:
            raise HTTPException(404, "Nincs ilyen VPN-konfiguráció.")
        remaining = [stored for stored in current.configs if stored.id != config_id]
        if current.vpn_enabled and not remaining:
            raise HTTPException(409, "Bekapcsolt VPN mellett az utolsó konfiguráció nem törölhető.")

        update = VpnSettingsUpdate(configs=remaining)
        saved_active = current.active_config()
        restart = False
        if saved_active is not None and saved_active.id == config_id:
            following = current.config_after(config_id)
            update.active_config_id = following.id if following else None
            restart = current.vpn_enabled
        saved = await owned_to_thread(self._save, update)
        if restart:
            await self._manager.apply_settings(saved)
        return self._response(saved)

    async def speed_test(self) -> VpnSpeedTestResponse:
        """Letöltési sebesség, késleltetés és kilépő IP a tunnelen át (F6)."""
        policy = self._manager.policy
        status = self._manager.status
        if policy is None or status.state != VpnState.CONNECTED or self._speed_test_factory is None:
            raise HTTPException(409, "A sebességteszthez kapcsolódott VPN kell.")
        if self._speed_test_lock.locked():
            raise HTTPException(409, "Már fut egy sebességteszt.")
        async with self._speed_test_lock:
            try:
                result = await self._speed_test_factory().run(
                    SocksAuth(port=policy.port, username=policy.username, password=policy.password)
                )
            except SpeedTestError as error:
                logger.warning("operation=vpn-speedtest reason=%s", error.code)
                raise HTTPException(502, f"A sebességteszt nem sikerült ({error.code}).") from None
        logger.info("operation=vpn-speedtest mbps=%s", result.download_mbps)
        return VpnSpeedTestResponse(config_id=status.active_config_id, **asdict(result))

    async def test(self) -> VpnTestResponse:
        settings = await owned_to_thread(self._load)
        checks = [VpnCheck(name="vpn-enabled", ok=settings.vpn_enabled)]
        policy = self._manager.policy
        checks.append(VpnCheck(name="relay-proxy-mode", ok=isinstance(policy, VpnProxyPolicy)))

        values = self._network_settings()
        expected = {
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_utp": False,
            "enable_incoming_utp": False,
            "listen_interfaces": "127.0.0.1:0",
        }
        mismatched = sorted(key for key, value in expected.items() if values.get(key) != value)
        checks.append(
            VpnCheck(
                name="relay-settings",
                ok=settings.vpn_enabled and not mismatched,
                code=",".join(mismatched) or None,
            )
        )

        reachable: bool | None = None
        if policy is not None:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", policy.port), timeout=1.0
                )
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                reachable = True
            except (OSError, TimeoutError):
                reachable = False
        checks.append(VpnCheck(name="proxy-listening", ok=reachable))
        # G6: a wireproxy /readyz-e csak akkor 200, ha a CheckAlive-címek a tunnelen át
        # válaszolnak; CheckAlive nélkül nem bizonyít kapcsolatot.
        if policy is None:
            checks.append(VpnCheck(name="handshake", ok=None))
        elif not self._manager.check_alive_configured:
            checks.append(VpnCheck(name="handshake", ok=None, code="check-alive-not-configured"))
        else:
            ready = await self._manager.readiness()
            checks.append(
                VpnCheck(name="handshake", ok=ready is True, code=None if ready else "no-reply")
            )
        return VpnTestResponse(state=self._manager.status.state, checks=checks)

