import asyncio
import re

from app.config import NodeEnv, config
from app.modules.settings.service import SettingsService
from app.modules.stremio.constants import (
    ADDON_APP_PREFIX_ID,
    SEARCH_ID,
)
from app.modules.stremio.enums import (
    ContentType,
    ExtraName,
    ShortManifestResource,
)
from app.modules.stremio.schemas import (
    Manifest,
    ManifestBehaviorHints,
    ManifestCatalog,
    ManifestExtra,
    StreamId,
    StremioStream,
    TorrentStreamId,
)
from app.modules.torrent_streams.service import TorrentStreamsService
from app.modules.users.models import UserModel


class StremioService:
    def __init__(
        self,
        torrent_streams_service: TorrentStreamsService,
        settings_service: SettingsService,
    ):
        self._torrent_streams_service = torrent_streams_service
        self._settings_service = settings_service

    def manifest(self, user: UserModel, endpoint_type: str = "default") -> Manifest:
        app_url = self._settings_service.get_app_url()

        addon_id = "hu.stremhu-source.addon"
        name = "StremHU Source"
        version = config.version.removeprefix("v")

        if endpoint_type == "lan":
            name = f"{name} (LAN)"
            addon_id = f"{addon_id}.lan"
            target_url = self._settings_service.get_manifest_url("lan")
            if target_url:
                app_url = target_url.rstrip("/")
        elif endpoint_type == "tailscale":
            name = f"{name} (Tailscale)"
            addon_id = f"{addon_id}.tailscale"
            target_url = self._settings_service.get_manifest_url("tailscale")
            if target_url:
                app_url = target_url.rstrip("/")

        if not re.match(r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?$", version):
            version = "0.0.0"
            addon_id = f"{addon_id}.beta"
            name = f"{name} (Beta)"

        if config.node_env != NodeEnv.PROD:
            version = "0.0.0"
            addon_id = f"{addon_id}.dev"
            name = f"{name} (DEV)"

        catalogs: list[ManifestCatalog] = [
            ManifestCatalog(
                id=SEARCH_ID,
                name="🔍 Torrent - StremHU",
                type=ContentType.MOVIE,
                extra=[
                    ManifestExtra(
                        name=ExtraName.SEARCH,
                        is_required=True,
                    )
                ],
            ),
        ]

        return Manifest(
            id=addon_id,
            version=version,
            name=name,
            description=config.description,
            resources=[
                ShortManifestResource.STREAM,
                ShortManifestResource.CATALOG,
                ShortManifestResource.META,
            ],
            types=[ContentType.MOVIE, ContentType.SERIES],
            id_prefixes=["tt", ADDON_APP_PREFIX_ID],
            catalogs=catalogs,
            behavior_hints=ManifestBehaviorHints(
                configurable=True,
                configuration_required=False,
            ),
            logo=f"{app_url}/logo.png",
        )

    async def get_streams(
        self,
        user: UserModel,
        parsed_id: StreamId,
        endpoint_type: str = "default",
    ) -> list[StremioStream]:
        """Lekéri a lejátszható streameket az adatbázisból vagy indexerekből."""
        app_url = await asyncio.to_thread(self._settings_service.get_app_url)
        if isinstance(parsed_id, TorrentStreamId):
            torrent_streams = await self._torrent_streams_service.find_by_torrent_id(
                indexer_id=parsed_id.indexer_id,
                torrent_id=parsed_id.torrent_id,
                user=user,
            )

            return [
                StremioStream.from_id_torrent_stream(torrent_stream=torrent_stream)
                for torrent_stream in torrent_streams
            ]

        # IMDb alapú stream lekérdezés kereséssel és feloldással
        torrent_streams, errors = await self._torrent_streams_service.find_by_imdb(
            user=user,
            imdb_id=parsed_id.imdb_id,
            series=parsed_id.series_info,
        )

        stremio_streams = [
            StremioStream.from_imdb_torrent_stream(torrent_stream=torrent_stream)
            for torrent_stream in torrent_streams
        ]

        for error in errors:
            stremio_streams.append(
                StremioStream(
                    name="⚠️ [StremHU] Hiba",
                    description=error,
                    external_url=app_url,
                )
            )

        if endpoint_type != "default":
            target_url = self._settings_service.get_manifest_url(endpoint_type)
            if target_url:
                main_app_url = app_url.rstrip("/")
                clean_target = target_url.rstrip("/")
                for s in stremio_streams:
                    if s.url and main_app_url in s.url:
                        s.url = s.url.replace(main_app_url, clean_target)

        return stremio_streams
