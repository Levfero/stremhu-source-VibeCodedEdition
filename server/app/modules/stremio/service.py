import asyncio
import re

from app.common.schemas.internal import SeriesInfo
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
    KitsuStreamId,
    Manifest,
    ManifestBehaviorHints,
    ManifestCatalog,
    ManifestExtra,
    StreamId,
    StremioStream,
    TorrentStreamId,
    TvdbStreamId,
)
from app.modules.stremio.tvdb_resolver import TvdbResolver, tvdb_resolver
from app.modules.torrent_streams.service import TorrentStreamsService
from app.modules.users.models import UserModel


class StremioService:
    def __init__(
        self,
        torrent_streams_service: TorrentStreamsService,
        settings_service: SettingsService,
        tvdb_resolver_service: TvdbResolver | None = None,
    ):
        self._torrent_streams_service = torrent_streams_service
        self._settings_service = settings_service
        self._tvdb_resolver = tvdb_resolver_service or tvdb_resolver

    def manifest(self, user: UserModel) -> Manifest:
        app_url = self._settings_service.get_app_url()

        addon_id = "hu.stremhu-source.addon"
        name = "StremHU Source"
        version = config.version.removeprefix("v")

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
            types=[ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME],
            id_prefixes=[
                "tt",
                "tvdb:",
                "tvdb",
                "kitsu:",
                "kitsu",
                "mal:",
                "anime:",
                ADDON_APP_PREFIX_ID,
            ],
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

        if isinstance(parsed_id, TvdbStreamId):
            resolved_imdb_id = await self._tvdb_resolver.resolve_tvdb_to_imdb(
                parsed_id.tvdb_id
            )
            if not resolved_imdb_id:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] TVDB feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót találni a TVDB ID-hoz ({parsed_id.tvdb_id}).",
                        external_url=app_url,
                    )
                ]
            imdb_id = resolved_imdb_id
            series_info = parsed_id.series_info
        elif isinstance(parsed_id, KitsuStreamId):
            resolved_imdb_id, season, ep, alt_eps, alt_seasons = (
                await self._tvdb_resolver.resolve_kitsu_to_imdb(
                    parsed_id.kitsu_id,
                    parsed_id.episode,
                )
            )
            if not resolved_imdb_id:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] Kitsu feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót találni a Kitsu ID-hoz ({parsed_id.kitsu_id}).",
                        external_url=app_url,
                    )
                ]
            imdb_id = resolved_imdb_id
            series_info = (
                SeriesInfo(
                    season=season or 1,
                    episode=ep,
                    alternate_episodes=alt_eps,
                    alternate_seasons=alt_seasons,
                )
                if ep is not None
                else None
            )
        else:
            imdb_id = parsed_id.imdb_id
            series_info = parsed_id.series_info

        # Dinamikus epizód leképezés (abszolút és alternatív epizódszámok feloldása külső metaadatokból)
        if series_info and series_info.episode is not None and not series_info.alternate_episodes:
            dyn_alt_eps, dyn_alt_seasons = await self._tvdb_resolver.get_episode_mappings(
                imdb_id=imdb_id,
                season=series_info.season,
                episode=series_info.episode,
            )
            if dyn_alt_eps:
                series_info.alternate_episodes = dyn_alt_eps
            if dyn_alt_seasons:
                series_info.alternate_seasons = dyn_alt_seasons

        # Alternatív/hasított IMDb azonosítók feloldása (pl. Dragon Ball Z tt0121220 <-> tt0214341)
        alternate_imdb_ids: list[str] | None = None
        if hasattr(self._tvdb_resolver, "get_alternate_imdb_ids"):
            alt_res = await self._tvdb_resolver.get_alternate_imdb_ids(imdb_id)
            if isinstance(alt_res, list):
                alternate_imdb_ids = alt_res

        # IMDb alapú stream lekérdezés kereséssel és feloldással
        # A másodlagos név szerinti kereséshez a cím csak akkor kerül lekérésre, ha IMDb-re 0 torrent érkezett
        torrent_streams, errors = await self._torrent_streams_service.find_by_imdb(
            user=user,
            imdb_id=imdb_id,
            series=series_info,
            fallback_title=lambda: self._tvdb_resolver.get_title_by_imdb(imdb_id),
            alternate_imdb_ids=alternate_imdb_ids,
        )

        stremio_streams = [
            StremioStream.from_imdb_torrent_stream(
                torrent_stream=torrent_stream,
                is_series=series_info is not None,
            )
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

        return stremio_streams
