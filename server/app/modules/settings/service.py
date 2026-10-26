from pydantic import TypeAdapter

from app.config import config
from app.modules.settings.enums import NetworkModeEnum, SettingsKeyEnum
from app.modules.settings.repository import SettingsRepository
from app.modules.settings.schemas.internal import (
    ManifestEndpointConfig,
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

        default_app_url = self.get_app_url().rstrip("/")
        default_lan_url = (
            config.lan_url
            or (f"http://{config.host_ip}:{config.port}" if config.host_ip else "")
        ).rstrip("/")

        detected_ts_ip = config.tailscale_ip or _detect_tailscale_ip()
        default_tailscale_url = (
            config.tailscale_url
            or (f"http://{detected_ts_ip}:{config.port}" if detected_ts_ip else "")
        ).rstrip("/")

        env_endpoints: list[ManifestEndpointConfig] = [
            ManifestEndpointConfig(
                id="default",
                name="Nyilvános",
                badge="Nyilvános",
                url_suffix="stremio",
                base_url=default_app_url,
                is_enabled=True,
            )
        ]
        if default_lan_url:
            env_endpoints.append(
                ManifestEndpointConfig(
                    id="lan",
                    name="Helyi hálózat (LAN)",
                    badge="LAN",
                    url_suffix="stremio-lan",
                    base_url=default_lan_url,
                    is_enabled=True,
                )
            )
        if default_tailscale_url:
            env_endpoints.append(
                ManifestEndpointConfig(
                    id="tailscale",
                    name="Tailscale",
                    badge="Tailscale",
                    url_suffix="stremio-tailscale",
                    base_url=default_tailscale_url,
                    is_enabled=True,
                )
            )

        if not saved or (not saved.endpoints and saved.lan_enabled is None and saved.tailscale_enabled is None):
            return ManifestSettings(
                endpoints=env_endpoints,
                lan_enabled=bool(default_lan_url),
                lan_url=default_lan_url,
                tailscale_enabled=bool(default_tailscale_url),
                tailscale_url=default_tailscale_url,
            )

        if saved.endpoints:
            endpoints_map = {ep.id: ep for ep in saved.endpoints}
            for env_ep in env_endpoints:
                if env_ep.id not in endpoints_map:
                    saved.endpoints.append(env_ep)
                else:
                    saved_ep = endpoints_map[env_ep.id]
                    if not saved_ep.base_url:
                        saved_ep.base_url = env_ep.base_url

            lan_ep = next((ep for ep in saved.endpoints if ep.id == "lan"), None)
            ts_ep = next((ep for ep in saved.endpoints if ep.id == "tailscale"), None)
            return ManifestSettings(
                endpoints=saved.endpoints,
                lan_enabled=lan_ep.is_enabled if lan_ep else False,
                lan_url=lan_ep.base_url if lan_ep else "",
                tailscale_enabled=ts_ep.is_enabled if ts_ep else False,
                tailscale_url=ts_ep.base_url if ts_ep else "",
            )

        endpoints = list(env_endpoints)
        for ep in endpoints:
            if ep.id == "lan" and saved.lan_enabled is not None:
                ep.is_enabled = saved.lan_enabled
                if saved.lan_url:
                    ep.base_url = saved.lan_url
            elif ep.id == "tailscale" and saved.tailscale_enabled is not None:
                ep.is_enabled = saved.tailscale_enabled
                if saved.tailscale_url:
                    ep.base_url = saved.tailscale_url

        lan_ep = next((ep for ep in endpoints if ep.id == "lan"), None)
        ts_ep = next((ep for ep in endpoints if ep.id == "tailscale"), None)
        return ManifestSettings(
            endpoints=endpoints,
            lan_enabled=lan_ep.is_enabled if lan_ep else False,
            lan_url=lan_ep.base_url if lan_ep else "",
            tailscale_enabled=ts_ep.is_enabled if ts_ep else False,
            tailscale_url=ts_ep.base_url if ts_ep else "",
        )

    def save_manifests(self, payload: ManifestSettingsUpdate) -> ManifestSettings:
        current = self.get_manifest_settings()
        data = current.model_dump()
        update_data = payload.model_dump(exclude_unset=True)

        if "endpoints" in update_data and update_data["endpoints"] is not None:
            cleaned_endpoints = []
            for ep_dict in update_data["endpoints"]:
                ep_id = ep_dict.get("id") or "custom"
                name = ep_dict.get("name") or ep_id
                base_url = (ep_dict.get("base_url") or "").rstrip("/")
                is_enabled = ep_dict.get("is_enabled", True)
                badge = ep_dict.get("badge") or (name if len(name) <= 12 else ep_id.upper())
                url_suffix = ep_dict.get("url_suffix") or ("stremio" if ep_id == "default" else f"stremio-{ep_id}")
                cleaned_endpoints.append(
                    {
                        "id": ep_id,
                        "name": name,
                        "base_url": base_url,
                        "is_enabled": is_enabled,
                        "badge": badge,
                        "url_suffix": url_suffix,
                    }
                )
            data["endpoints"] = cleaned_endpoints

        if "lan_enabled" in update_data and update_data["lan_enabled"] is not None:
            data["lan_enabled"] = update_data["lan_enabled"]
        if "lan_url" in update_data and update_data["lan_url"] is not None:
            data["lan_url"] = update_data["lan_url"]
        if "tailscale_enabled" in update_data and update_data["tailscale_enabled"] is not None:
            data["tailscale_enabled"] = update_data["tailscale_enabled"]
        if "tailscale_url" in update_data and update_data["tailscale_url"] is not None:
            data["tailscale_url"] = update_data["tailscale_url"]

        self._settings_repository.save(SettingsKeyEnum.MANIFESTS, data)
        return self.get_manifest_settings()

    def get_manifest_endpoints(self) -> list[ManifestEndpointInfo]:
        settings = self.get_manifest_settings()
        result = []
        for ep in settings.endpoints:
            if not ep.is_enabled or not ep.base_url:
                continue
            url_suffix = ep.url_suffix or ("stremio" if ep.id == "default" else f"stremio-{ep.id}")
            badge = ep.badge or (ep.name if len(ep.name) <= 12 else ep.id.upper())
            result.append(
                ManifestEndpointInfo(
                    id=ep.id,
                    name=ep.name,
                    badge=badge,
                    url_suffix=url_suffix,
                    base_url=ep.base_url.rstrip("/"),
                    is_enabled=ep.is_enabled,
                )
            )
        return result

    def get_endpoint_by_id(self, endpoint_id: str) -> ManifestEndpointInfo | None:
        settings = self.get_manifest_settings()
        for ep in settings.endpoints:
            if ep.id == endpoint_id:
                url_suffix = ep.url_suffix or ("stremio" if ep.id == "default" else f"stremio-{ep.id}")
                badge = ep.badge or (ep.name if len(ep.name) <= 12 else ep.id.upper())
                return ManifestEndpointInfo(
                    id=ep.id,
                    name=ep.name,
                    badge=badge,
                    url_suffix=url_suffix,
                    base_url=ep.base_url.rstrip("/"),
                    is_enabled=ep.is_enabled,
                )
        return None

    def get_manifest_url(self, endpoint_type: str) -> str | None:
        ep = self.get_endpoint_by_id(endpoint_type)
        if ep and ep.is_enabled and ep.base_url:
            return ep.base_url
        return None


def _detect_tailscale_ip() -> str | None:
    # 1. Próbálkozás: SIOCGIFADDR ioctl (tiszta Python standard library, független külső parancsoktól)
    try:
        import fcntl
        import socket
        import struct

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = struct.pack("256s", b"tailscale0")
        res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)
        ip = socket.inet_ntoa(res[20:24])
        if ip and ip.startswith("100."):
            return ip
    except Exception:
        pass

    # 2. Próbálkozás: ip binary (ha létezik)
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

