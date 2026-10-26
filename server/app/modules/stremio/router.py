import os
import socket
import struct
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import RedirectResponse

from app.config import config
from app.modules.auth.dependencies import ApiKeyGuard
from app.modules.stremio.catalogs_service import StremioCatalogsService
from app.modules.stremio.dependencies import (
    get_parsed_catalog_id,
    get_parsed_extra,
    get_parsed_stream_id,
    get_stremio_catalogs_service,
    get_stremio_service,
)
from app.modules.stremio.enums import MediaType
from app.modules.stremio.schemas import (
    Manifest,
    MetaResponse,
    ParsedCatalogId,
    ParsedExtra,
    StreamId,
    StremioCatalogResponse,
    StremioStreams,
)
from app.modules.stremio.service import StremioService
from app.modules.users.models import UserModel


@lru_cache(maxsize=16)
def _get_target_base_url(endpoint_type: str) -> str | None:
    if endpoint_type == "lan":
        lan_url = os.environ.get("LAN_URL")
        if lan_url:
            return lan_url.rstrip("/")
        host_ip = os.environ.get("HOST_IP") or config.host_ip
        port = config.port or 7070
        if host_ip:
            return f"http://{host_ip}:{port}"
    elif endpoint_type == "tailscale":
        ts_url = os.environ.get("TAILSCALE_URL")
        if ts_url:
            return ts_url.rstrip("/")
        ts_ip = os.environ.get("TAILSCALE_IP")
        if not ts_ip:
            try:
                import fcntl

                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                ts_ip = socket.inet_ntoa(
                    fcntl.ioctl(
                        s.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack("256s", b"tailscale0"[:15]),
                    )[20:24]
                )
            except Exception:
                ts_ip = None
        if ts_ip:
            port = config.port or 7070
            return f"http://{ts_ip}:{port}"
    return None


def create_stremio_router(endpoint_type: str = "default") -> APIRouter:
    if endpoint_type == "lan":
        prefix = "/{api_key}/stremio-lan"
        tag_name = "Stremio LAN"
        addon_suffix = " (LAN)"
        id_suffix = ".lan"
    elif endpoint_type == "tailscale":
        prefix = "/{api_key}/stremio-tailscale"
        tag_name = "Stremio Tailscale"
        addon_suffix = " (Tailscale)"
        id_suffix = ".tailscale"
    else:
        prefix = "/{api_key}/stremio"
        tag_name = "Stremio"
        addon_suffix = ""
        id_suffix = ""

    r = APIRouter(
        prefix=prefix,
        tags=[tag_name],
    )

    @r.get(
        "/manifest.json",
        response_model=Manifest,
        response_model_exclude_none=True,
    )
    def manifest(
        stremio_service: Annotated[StremioService, Depends(get_stremio_service)],
        user: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> Manifest:
        m = stremio_service.manifest(user)
        if addon_suffix:
            m.name = f"{m.name}{addon_suffix}"
            m.id = f"{m.id}{id_suffix}"
        target_url = _get_target_base_url(endpoint_type)
        if target_url:
            m.logo = f"{target_url}/logo.png"
        return m

    @r.get(
        "/configure",
        status_code=status.HTTP_308_PERMANENT_REDIRECT,
    )
    def configure(
        _: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> RedirectResponse:
        return RedirectResponse(
            url="/",
            status_code=status.HTTP_308_PERMANENT_REDIRECT,
        )

    @r.get(
        "/stream/{media_type}/{stream_id}.json",
        response_model=StremioStreams,
    )
    async def streams(
        media_type: Annotated[MediaType, Path(..., description="A média típusa")],
        parsed_id: Annotated[StreamId, Depends(get_parsed_stream_id)],
        stremio_service: Annotated[StremioService, Depends(get_stremio_service)],
        user: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> StremioStreams:
        stremio_streams = await stremio_service.get_streams(user, parsed_id)
        if endpoint_type != "default":
            target_url = _get_target_base_url(endpoint_type)
            if target_url:
                for s in stremio_streams:
                    if s.url and "/api/" in s.url:
                        idx = s.url.find("/api/")
                        s.url = f"{target_url}{s.url[idx:]}"
        return StremioStreams(streams=stremio_streams)

    @r.get(
        "/catalog/{media_type}/{catalog_id}.json",
        response_model=StremioCatalogResponse,
    )
    async def catalog(
        media_type: MediaType,
        catalog_id: str,
        stremio_catalogs_service: Annotated[
            StremioCatalogsService, Depends(get_stremio_catalogs_service)
        ],
        _: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> StremioCatalogResponse:
        return await stremio_catalogs_service.get_catalog(
            media_type,
            catalog_id,
        )

    @r.get(
        "/catalog/{media_type}/{catalog_id}/{extra}.json",
        response_model=StremioCatalogResponse,
    )
    async def catalog_with_extra(
        media_type: Annotated[MediaType, Path(..., description="A média típusa")],
        catalog_id: Annotated[str, Path(..., description="A katalógus azonosítója")],
        parsed_extra: Annotated[ParsedExtra, Depends(get_parsed_extra)],
        stremio_catalogs_service: Annotated[
            StremioCatalogsService, Depends(get_stremio_catalogs_service)
        ],
        _: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> StremioCatalogResponse:
        return await stremio_catalogs_service.get_catalog(
            media_type,
            catalog_id,
            parsed_extra,
        )

    @r.get(
        "/meta/{media_type}/{meta_id}.json",
        response_model=MetaResponse,
    )
    async def meta(
        media_type: Annotated[MediaType, Path(..., description="A média típusa")],
        parsed_id: Annotated[ParsedCatalogId | None, Depends(get_parsed_catalog_id)],
        stremio_catalogs_service: Annotated[
            StremioCatalogsService, Depends(get_stremio_catalogs_service)
        ],
        _: Annotated[UserModel, Depends(ApiKeyGuard())],
    ) -> MetaResponse:
        if media_type != MediaType.MOVIE or parsed_id is None:
            return MetaResponse(meta={})

        result = await stremio_catalogs_service.get_meta(
            indexer_id=parsed_id.indexer_id,
            torrent_id=parsed_id.torrent_id,
        )

        if not result:
            return MetaResponse(meta={})

        return MetaResponse(meta=result)

    return r


router = create_stremio_router("default")
router_lan = create_stremio_router("lan")
router_tailscale = create_stremio_router("tailscale")
