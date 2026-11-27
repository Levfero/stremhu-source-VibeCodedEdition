"""A VPN admin-műveletei (VPN-terv Rev. 5, 6.5): import, be/kikapcsolás, törlés, diagnosztika."""

import asyncio
import contextlib
from collections.abc import Callable

from fastapi import HTTPException

from app.common.async_utils import owned_to_thread
from app.common.logger import logger
from app.modules.relay.network_policy import VpnProxyPolicy
from app.modules.settings.schemas.internal import VpnSettings, VpnSettingsUpdate
from app.modules.vpn.manager import VpnManager
from app.modules.vpn.schemas import (
    VpnCheck,
    VpnMetadataResponse,
    VpnResponse,
    VpnStatusResponse,
    VpnTestResponse,
    VpnUpdateRequest,
)
from app.modules.vpn.secret_store import SecretStore, SecretStoreError
from app.modules.vpn.wireguard_config import (
    WireGuardConfigError,
    parse_wireguard_config,
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
    ):
        self._manager = manager
        self._load = load_settings
        self._save = save_settings
        self._secret_store_factory = secret_store_factory
        self._network_settings = network_settings

    def _status(self) -> VpnStatusResponse:
        status = self._manager.status
        return VpnStatusResponse(
            state=status.state, error_code=status.error_code, connected_at=status.connected_at
        )

    def _response(self, settings: VpnSettings) -> VpnResponse:
        metadata = settings.config_metadata
        return VpnResponse(
            **self._status().model_dump(),
            enabled=settings.vpn_enabled,
            has_config=bool(settings.encrypted_config),
            metadata=VpnMetadataResponse(**metadata.model_dump()) if metadata else None,
        )

    async def get(self) -> VpnResponse:
        return self._response(await owned_to_thread(self._load))

    def status(self) -> VpnStatusResponse:
        return self._status()

    def _encrypt(self, config_text: str) -> VpnSettingsUpdate:
        try:
            config = parse_wireguard_config(config_text)
        except WireGuardConfigError as error:
            raise HTTPException(400, f"Érvénytelen WireGuard-konfiguráció ({error}).") from None
        try:
            envelope = self._secret_store_factory().encrypt(config_text)
        except SecretStoreError as error:
            logger.error("operation=vpn-config-store reason=%s", error.code)
            raise HTTPException(500, "A VPN-konfiguráció titkosított tárolása nem sikerült.") from None
        return VpnSettingsUpdate(encrypted_config=envelope, config_metadata=config.metadata())

    async def update(self, payload: VpnUpdateRequest) -> VpnResponse:
        current = await owned_to_thread(self._load)
        update = self._encrypt(payload.config) if payload.config is not None else VpnSettingsUpdate()
        has_config = bool(update.encrypted_config or current.encrypted_config)

        if payload.enabled is True and not has_config:
            raise HTTPException(400, "Előbb tölts fel egy WireGuard-konfigurációt.")
        if payload.enabled is False and current.vpn_enabled and not payload.confirm_unprotected:
            raise HTTPException(
                400,
                "A kikapcsolás után a torrentforgalom VPN nélkül megy; ehhez megerősítés kell.",
            )
        if payload.enabled is not None:
            update.vpn_enabled = payload.enabled

        saved = await owned_to_thread(self._save, update)
        if payload.config is not None or saved.vpn_enabled != current.vpn_enabled:
            await self._manager.apply_settings(saved)
        return self._response(saved)

    async def delete_config(self) -> VpnResponse:
        current = await owned_to_thread(self._load)
        if current.vpn_enabled:
            raise HTTPException(409, "Bekapcsolt VPN mellett a konfiguráció nem törölhető.")
        saved = await owned_to_thread(
            self._save, VpnSettingsUpdate(encrypted_config=None, config_metadata=None)
        )
        return self._response(saved)

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
        # A WireGuard-handshake és a DNS-ellenőrzés a wireproxy /readyz és /metrics végpontjára
        # épül; ez a G6 kapu után kerül be.
        checks.append(VpnCheck(name="handshake", ok=None, code="not-implemented"))
        return VpnTestResponse(state=self._manager.status.state, checks=checks)

