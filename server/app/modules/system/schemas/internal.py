from pydantic import BaseModel, Field

from app.modules.settings.schemas.internal import ManifestEndpointInfo


class SystemStatus(BaseModel):
    configured: bool

    version: str

    app_url: str

    host_ip: str

    port: int

    is_reverse_proxy: bool

    manifest_endpoints: list[ManifestEndpointInfo] = Field(default_factory=list)

