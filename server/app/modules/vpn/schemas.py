import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.modules.settings.schemas.internal import MAX_VPN_CONFIGS
from app.modules.vpn.manager import VpnState
from app.modules.vpn.wireguard_config import MAX_CONFIG_BYTES

MAX_VPN_CONFIG_NAME = 64


class _CamelModel(BaseModel):
    model_config = ConfigDict(validate_by_name=True, alias_generator=to_camel)


class VpnMetadataResponse(_CamelModel):
    addresses: list[str]
    dns: list[str]
    endpoint: str
    peer_public_key: str
    client_public_key_fingerprint: str


class VpnConfigResponse(_CamelModel):
    id: str
    name: str
    metadata: VpnMetadataResponse


class VpnStatusResponse(_CamelModel):
    state: VpnState
    error_code: str | None = None
    connected_at: datetime.datetime | None = None
    active_config_id: str | None = None
    failover_at: datetime.datetime | None = None


class VpnResponse(VpnStatusResponse):
    enabled: bool
    has_config: bool
    # Az aktív konfiguráció metaadatai (kompatibilitás az egykonfigos felülettel).
    metadata: VpnMetadataResponse | None = None
    configs: list[VpnConfigResponse] = Field(default_factory=list)
    auto_failover: bool = True


class VpnUpdateRequest(_CamelModel):
    # Új konfiguráció a listához; a nyílt szöveg soha nem kerül vissza válaszba vagy naplóba.
    config: str | None = Field(default=None, max_length=MAX_CONFIG_BYTES, repr=False)
    name: str | None = Field(default=None, max_length=MAX_VPN_CONFIG_NAME)
    enabled: bool | None = None
    confirm_unprotected: bool = False
    auto_failover: bool | None = None
    # Kézi váltás egy mentett konfigurációra.
    active_config_id: str | None = Field(default=None, max_length=32)
    # A mentett konfigurációk új sorrendje (az átállási sorrend).
    order: list[str] | None = Field(default=None, max_length=MAX_VPN_CONFIGS)


class VpnSpeedTestResponse(_CamelModel):
    config_id: str | None = None
    download_mbps: float
    downloaded_bytes: int
    duration_seconds: float
    latency_ms: float | None = None
    # A VPN kilépő címe; a saját címet a szerver soha nem adja vissza.
    exit_ip: str | None = None
    exit_country: str | None = None
    exit_differs_from_direct: bool | None = None


class VpnCheck(_CamelModel):
    name: str
    ok: bool | None
    code: str | None = None


class VpnTestResponse(_CamelModel):
    state: VpnState
    checks: list[VpnCheck]
