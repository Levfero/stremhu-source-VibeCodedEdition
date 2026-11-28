from fastapi import HTTPException

from app.common.logger import logger
from app.modules.relay.network_policy import VpnProxyPolicy
from app.modules.relay.schemas import RelaySettingsUpdate as RelayLibtorrentUpdate
from app.modules.relay.service import RelayService
from app.modules.settings.schemas.api import RelaySettingsResponse
from app.modules.settings.schemas.internal import RelaySettings, RelaySettingsUpdate
from app.modules.settings.service import SettingsService


class RelaySettingsService:
    def __init__(
        self,
        settings_service: SettingsService,
        relay_service: RelayService,
    ):
        self._settings_service = settings_service
        self._relay_service = relay_service

    def get_settings(self) -> RelaySettings:
        return self._settings_service.get_relay()

    def vpn_managed(self) -> bool:
        return isinstance(self._relay_service.network_policy, VpnProxyPolicy)

    def to_response(self, settings: RelaySettings) -> RelaySettingsResponse:
        return RelaySettingsResponse(**settings.model_dump(), vpn_managed=self.vpn_managed())

    def update_settings(self, payload: RelaySettingsUpdate) -> RelaySettings:
        if self.vpn_managed():
            current = self.get_settings()
            changes = payload.model_dump(exclude_unset=True)
            for key in ("port", "enable_upnp_and_natpmp", "enable_utp"):
                if changes.get(key) is not None and changes[key] != getattr(current, key):
                    raise HTTPException(
                        409,
                        "VPN módban a portot, az UPnP/NAT-PMP-t és az uTP-t a VPN kezeli.",
                    )
        settings = self._settings_service.save_relay(payload)
        self.sync_settings()

        return settings

    def sync_settings(self) -> None:
        """Szinkronizálja az adatbázisban tárolt relay beállításokat a futó libtorrent session-nel."""
        settings = self.get_settings()

        libtorrent_update = RelayLibtorrentUpdate(
            download_limit=settings.download_limit,
            upload_limit=settings.upload_limit,
            port=settings.port,
            connections_limit=settings.connections_limit,
            torrent_connections_limit=settings.torrent_connections_limit,
            enable_upnp_and_natpmp=settings.enable_upnp_and_natpmp,
            encryption=settings.encryption,
            enable_utp=settings.enable_utp,
        )
        try:
            self._relay_service.update_settings(libtorrent_update)
        except Exception:
            logger.exception(
                "❌ Hiba történt a Relay beállítások szinkronizálása során."
            )
