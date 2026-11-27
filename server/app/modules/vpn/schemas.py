import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.modules.vpn.manager import VpnState
from app.modules.vpn.wireguard_config import MAX_CONFIG_BYTES


class _CamelModel(BaseModel):
    model_config = ConfigDict(validate_by_name=True, alias_generator=to_camel)


class VpnMetadataResponse(_CamelModel):
    addresses: list[str]
    dns: list[str]
    endpoint: str
    peer_public_key: str
    client_public_key_fingerprint: str


class VpnStatusResponse(_CamelModel):
    state: VpnState
    error_code: str | None = None
    connected_at: datetime.datetime | None = None


class VpnResponse(VpnStatusResponse):
    enabled: bool
    has_config: bool
    metadata: VpnMetadataResponse | None = None


class VpnUpdateRequest(_CamelModel):
    # A nyílt WireGuard-konfiguráció; soha nem kerül vissza válaszba vagy naplóba.
    config: str | None = Field(default=None, max_length=MAX_CONFIG_BYTES, repr=False)
    enabled: bool | None = None
    confirm_unprotected: bool = False


class VpnCheck(_CamelModel):
    name: str
    ok: bool | None
    code: str | None = None


class VpnTestResponse(_CamelModel):
    state: VpnState
    checks: list[VpnCheck]
