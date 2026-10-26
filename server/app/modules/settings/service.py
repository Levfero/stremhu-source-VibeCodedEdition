from pydantic import TypeAdapter

from app.config import config
from app.modules.settings.enums import NetworkModeEnum, SettingsKeyEnum
from app.modules.settings.repository import SettingsRepository
from app.modules.settings.schemas.internal import (
    ManifestEndpointInfo,
    ManifestSettings,
    ManifestSettingsUpdate,
    NetworkManualSettings,
    NetworkSettings,
    RelaySettings,
    RelaySettingsUpdate,
    SystemSettings,
    SystemSettingsUpdate,
)


class SettingsService:
    def __init__(
        self,
        settings_repository: SettingsRepository,
    ):
        self._settings_repository = settings_repository

    # System Settings

    def save_system(self, payload: SystemSettingsUpdate) -> SystemSettings:
        system_settings = self.find_system()
        if not system_settings:
            system_settings = SystemSettings()

        data = payload.model_dump(exclude_unset=True)
        updated_data = system_settings.model_copy(update=data).model_dump()

        self._settings_repository.save(SettingsKeyEnum.SYSTEM, updated_data)

        return SystemSettings.model_validate(updated_data)

    def find_system(self) -> SystemSettings | None:
        record = self._settings_repository.find_one(SettingsKeyEnum.SYSTEM.value)
        if not record or not record.value:
            return None
        return SystemSettings.model_validate(record.value)

    def get_system(self) -> SystemSettings:
        system_settings = self.find_system()
        if not system_settings:
            raise ValueError("A rendszerbeállítások nem léteznek.")
        return system_settings

    # Relay Settings

    def save_relay(self, payload: RelaySettingsUpdate) -> RelaySettings:
        relay_settings = self.find_relay()
        if not relay_settings:
            relay_settings = RelaySettings()

        data = payload.model_dump(exclude_unset=True)
        updated_data = relay_settings.model_copy(update=data).model_dump()

        self._settings_repository.save(SettingsKeyEnum.RELAY, updated_data)

        return RelaySettings.model_validate(updated_data)

    def find_relay(self) -> RelaySettings | None:
        record = self._settings_repository.find_one(SettingsKeyEnum.RELAY.value)
        if not record or not record.value:
            return None
        return RelaySettings.model_validate(record.value)

    def get_relay(self) -> RelaySettings:
        relay_settings = self.find_relay()
        if not relay_settings:
            raise ValueError("A Relay beállítások nem léteznek.")
        return relay_settings

    # Network Settings

    def save_network(self, payload: NetworkSettings) -> NetworkSettings:
        if config.reverse_proxy_domain:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=403,
                detail="A hálózati beállításokat környezeti változó vezérli, így nem módosíthatóak.",
            )
        self._settings_repository.save(SettingsKeyEnum.NETWORK, payload.model_dump())
        return payload

    def find_network(self) -> NetworkSettings | None:
        if config.reverse_proxy_domain:
            return NetworkManualSettings(
                mode=NetworkModeEnum.MANUAL, host=config.reverse_proxy_domain
            )

        record = self._settings_repository.find_one(SettingsKeyEnum.NETWORK.value)
        if not record or not record.value:
            return None
        return TypeAdapter(NetworkSettings).validate_python(record.value)

    def get_network(self) -> NetworkSettings:
        network_settings = self.find_network()
        if not network_settings:
            raise ValueError("Nincs beállítva hálózati elérés")
        return network_settings

    def get_app_url(self) -> str:
        network_settings = self.get_network()
        if network_settings.mode == NetworkModeEnum.MANUAL:
            return f"https://{network_settings.host}"

        return f"https://{network_settings.host}:{config.port}"

    # Manifest Settings

    def find_manifests(self) -> ManifestSettings | None:
        record = self._settings_repository.find_one(SettingsKeyEnum.MANIFESTS.value)
        if not record or not record.value:
            return None
        return TypeAdapter(ManifestSettings).validate_python(record.value)

    def get_manifest_settings(self) -> ManifestSettings:
        saved = self.find_manifests()

        default_lan_url = (
            config.lan_url
            or (f"http://{config.host_ip}:{config.port}" if config.host_ip else "")
        )

        detected_ts_ip = config.tailscale_ip or _detect_tailscale_ip()
        default_tailscale_url = (
            config.tailscale_url
            or (f"http://{detected_ts_ip}:{config.port}" if detected_ts_ip else "")
        )

        lan_enabled = (
            saved.lan_enabled
            if saved and saved.lan_enabled is not None
            else bool(default_lan_url)
        )
        lan_url = (saved.lan_url if saved and saved.lan_url else "") or default_lan_url

        tailscale_enabled = (
            saved.tailscale_enabled
            if saved and saved.tailscale_enabled is not None
            else bool(default_tailscale_url)
        )
        tailscale_url = (
            (saved.tailscale_url if saved and saved.tailscale_url else "")
            or default_tailscale_url
        )

        return ManifestSettings(
            lan_enabled=lan_enabled,
            lan_url=lan_url,
            tailscale_enabled=tailscale_enabled,
            tailscale_url=tailscale_url,
        )

    def save_manifests(self, payload: ManifestSettingsUpdate) -> ManifestSettings:
        current = self.get_manifest_settings()
        data = current.model_dump()
        update_data = payload.model_dump(exclude_unset=True)
        data.update({k: v for k, v in update_data.items() if v is not None})
        self._settings_repository.save(SettingsKeyEnum.MANIFESTS, data)
        return self.get_manifest_settings()

    def get_manifest_endpoints(self) -> list[ManifestEndpointInfo]:
        app_url = self.get_app_url().rstrip("/")
        endpoints = [
            ManifestEndpointInfo(
                id="default",
                name="Nyilvános (Cloudflare)" if config.reverse_proxy_domain else "Alapértelmezett",
                badge="Cloudflare" if config.reverse_proxy_domain else "Nyilvános",
                url_suffix="stremio",
                base_url=app_url,
                is_enabled=True,
            )
        ]
        settings = self.get_manifest_settings()
        if settings.lan_enabled and settings.lan_url:
            endpoints.append(
                ManifestEndpointInfo(
                    id="lan",
                    name="Helyi hálózat (LAN)",
                    badge="LAN",
                    url_suffix="stremio-lan",
                    base_url=settings.lan_url.rstrip("/"),
                    is_enabled=True,
                )
            )
        if settings.tailscale_enabled and settings.tailscale_url:
            endpoints.append(
                ManifestEndpointInfo(
                    id="tailscale",
                    name="Tailscale (VPN)",
                    badge="Tailscale",
                    url_suffix="stremio-tailscale",
                    base_url=settings.tailscale_url.rstrip("/"),
                    is_enabled=True,
                )
            )
        return endpoints

    def get_manifest_url(self, endpoint_type: str) -> str | None:
        settings = self.get_manifest_settings()
        if endpoint_type == "lan":
            return settings.lan_url if settings.lan_enabled else None
        elif endpoint_type == "tailscale":
            return settings.tailscale_url if settings.tailscale_enabled else None
        return None


def _detect_tailscale_ip() -> str | None:
    try:
        import subprocess

        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )
        for part in out.split():
            if "/" in part and part.startswith("100."):
                return part.split("/")[0]
    except Exception:
        pass
    return None

