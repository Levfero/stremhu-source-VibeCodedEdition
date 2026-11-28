from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.config import config
from app.modules.settings.enums import (
    NetworkConnectionEnum,
    NetworkModeEnum,
    RelayEncryptionEnum,
)


class NetworkLocalSettings(BaseModel):
    mode: Literal[NetworkModeEnum.LOCAL]
    host: str
    ip: str
    self_signed: bool
    fullchain: str
    privkey: str
    expires_at: int


class NetworkAutoSettings(BaseModel):
    mode: Literal[NetworkModeEnum.AUTO]
    host: str
    token: str
    email: str
    connection: NetworkConnectionEnum
    provider: str
    ip: str
    account_key: str
    fullchain: str
    privkey: str
    expires_at: int
    last_ip_sync_at: int = 0


class NetworkManualSettings(BaseModel):
    mode: Literal[NetworkModeEnum.MANUAL]
    host: str


NetworkSettings = Annotated[
    NetworkLocalSettings | NetworkAutoSettings | NetworkManualSettings,
    Field(discriminator="mode"),
]


class SystemSettings(BaseModel):
    hit_and_run: bool = True
    keep_seed_seconds: int = 0
    cache_retention_seconds: int = 14 * 24 * 60 * 60  # 14 nap másodpercekben


class SystemSettingsUpdate(BaseModel):
    hit_and_run: bool | None = None
    keep_seed_seconds: int | None = None
    cache_retention_seconds: int | None = None


class RelaySettings(BaseModel):
    port: int = Field(default_factory=lambda: config.libtorrent_port)
    download_limit: int = 0
    upload_limit: int = 0
    connections_limit: int = 200
    torrent_connections_limit: int = 20
    enable_upnp_and_natpmp: bool = False
    encryption: RelayEncryptionEnum = RelayEncryptionEnum.ENABLED
    enable_utp: bool = True


class RelaySettingsUpdate(BaseModel):
    port: int | None = None
    download_limit: int | None = None
    upload_limit: int | None = None
    connections_limit: int | None = None
    torrent_connections_limit: int | None = None
    enable_upnp_and_natpmp: bool | None = None
    encryption: RelayEncryptionEnum | None = None
    enable_utp: bool | None = None


class VpnConfigMetadata(BaseModel):
    """A WireGuard-konfiguráció nem titkos adatai (a GUI és a naplók számára)."""

    addresses: list[str] = Field(default_factory=list)
    dns: list[str] = Field(default_factory=list)
    endpoint: str
    peer_public_key: str
    client_public_key_fingerprint: str


MAX_VPN_CONFIGS = 10
LEGACY_VPN_CONFIG_ID = "legacy"


class VpnStoredConfig(BaseModel):
    """Egy mentett WireGuard-konfiguráció; a nyílt szöveg soha nem kerül ide."""

    id: str
    name: str
    # AES-GCM boríték (secret_store).
    encrypted_config: str = Field(repr=False)
    metadata: VpnConfigMetadata


class VpnSettings(BaseModel):
    vpn_enabled: bool = False
    # Sorrend = átállási sorrend (F5); az aktív után a következőre vált a kezelő.
    configs: list[VpnStoredConfig] = Field(default_factory=list)
    active_config_id: str | None = None
    auto_failover: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_config(cls, data: Any) -> Any:
        """Az F1–G5 egykonfigos formája (encrypted_config + config_metadata) egy listaelem lesz."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        encrypted = data.pop("encrypted_config", None)
        metadata = data.pop("config_metadata", None)
        if encrypted and metadata and not data.get("configs"):
            data["configs"] = [
                {
                    "id": LEGACY_VPN_CONFIG_ID,
                    "name": "Konfiguráció 1",
                    "encrypted_config": encrypted,
                    "metadata": metadata,
                }
            ]
            data.setdefault("active_config_id", LEGACY_VPN_CONFIG_ID)
        return data

    def active_config(self) -> VpnStoredConfig | None:
        """A mentett aktív konfiguráció; ha az azonosító már nem létezik, a lista első eleme."""
        for stored in self.configs:
            if stored.id == self.active_config_id:
                return stored
        return self.configs[0] if self.configs else None

    def config_after(self, config_id: str | None) -> VpnStoredConfig | None:
        """Az átállási sorrendben következő konfiguráció (körbe), vagy None, ha nincs másik."""
        ids = [stored.id for stored in self.configs]
        if config_id not in ids or len(ids) < 2:
            return None
        return self.configs[(ids.index(config_id) + 1) % len(ids)]

    def storage_dump(self) -> dict[str, Any]:
        """Mentési forma; az aktív konfiguráció a régi mezőkben is szerepel, hogy egy korábbi
        (egykonfigos) build visszaváltás után is használni tudja."""
        dump = self.model_dump(mode="json")
        active = self.active_config()
        if active is not None:
            dump["encrypted_config"] = active.encrypted_config
            dump["config_metadata"] = active.metadata.model_dump(mode="json")
        return dump


class VpnSettingsUpdate(BaseModel):
    vpn_enabled: bool | None = None
    configs: list[VpnStoredConfig] | None = None
    active_config_id: str | None = None
    auto_failover: bool | None = None
