from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.modules.settings.schemas.internal import ManifestEndpointInfo


class SystemStatus(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )
    configured: bool

    version: str

    app_url: str

    host_ip: str

    port: int

    is_reverse_proxy: bool

    manifest_endpoints: list[ManifestEndpointInfo] = Field(default_factory=list)

