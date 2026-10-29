import asyncio
import re
import time

from app.common.logger import logger
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
    MalStreamId,
    Manifest,
    ManifestBehaviorHints,
    ManifestCatalog,
    ManifestExtra,
    StreamId,
    StremioStream,
    TmdbStreamId,
    TorrentStreamId,
    TvdbStreamId,
)
from app.modules.stremio.tvdb_resolver import TvdbResolver, tvdb_resolver
from app.modules.torrent_streams.service import TorrentStreamsService
from app.modules.users.models import UserModel

_STREAM_CACHE_TTL = 60.0
_STREAM_CACHE_MAX = 500
_stream_cache: dict[str, tuple[float, list[StremioStream]]] = {}
_ongoing_stream_tasks: dict[str, asyncio.Task[list[StremioStream]]] = {}


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
            ManifestCatalog(
                id=SEARCH_ID,
                name="🔍 Torrent - StremHU",
                type=ContentType.SERIES,
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
                "tmdb:",
                "tmdb",
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
        use_cache = type(self._torrent_streams_service) is TorrentStreamsService
        cache_key = f"{user.id}:{parsed_id.model_dump_json()}"
        if use_cache:
            now = time.monotonic()
            cached = _stream_cache.get(cache_key)
            if cached is not None:
                ts, cached_streams = cached
                if now - ts < _STREAM_CACHE_TTL:
                    return cached_streams
                _stream_cache.pop(cache_key, None)

            if cache_key in _ongoing_stream_tasks:
                return await _ongoing_stream_tasks[cache_key]

        task = asyncio.create_task(self._get_streams_uncached(user, parsed_id))
        if use_cache:
            _ongoing_stream_tasks[cache_key] = task
        try:
            result = await task
            if use_cache and result and not any(s.name.startswith("⚠️") for s in result):
                if len(_stream_cache) >= _STREAM_CACHE_MAX:
                    oldest_key = min(_stream_cache, key=lambda k: _stream_cache[k][0])
                    _stream_cache.pop(oldest_key, None)
                _stream_cache[cache_key] = (time.monotonic(), result)
            return result
        finally:
            if use_cache:
                _ongoing_stream_tasks.pop(cache_key, None)

    async def _get_streams_uncached(
        self,
        user: UserModel,
        parsed_id: StreamId,
    ) -> list[StremioStream]:
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

        fallback_title_override: str | None = None

        if isinstance(parsed_id, TvdbStreamId):
            resolved_imdb_id: str | None = None
            resolved_title: str | None = None
            if hasattr(self._tvdb_resolver, "resolve_tvdb_to_imdb"):
                resolved_imdb_id = await self._tvdb_resolver.resolve_tvdb_to_imdb(
                    parsed_id.tvdb_id
                )
            if not resolved_imdb_id and hasattr(self._tvdb_resolver, "resolve_tvdb"):
                tvdb_res = await self._tvdb_resolver.resolve_tvdb(
                    parsed_id.tvdb_id,
                    is_series=parsed_id.series_info is not None,
                )
                if isinstance(tvdb_res, tuple) and len(tvdb_res) >= 2:
                    resolved_imdb_id, resolved_title = tvdb_res[0], tvdb_res[1]
                elif isinstance(tvdb_res, str):
                    resolved_imdb_id = tvdb_res

            imdb_id = resolved_imdb_id
            series_info = parsed_id.series_info
            fallback_title_override = resolved_title
            if not imdb_id and not fallback_title_override:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] TVDB feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót vagy címet találni a TVDB ID-hoz ({parsed_id.tvdb_id}).",
                        external_url=app_url,
                    )
                ]
        elif isinstance(parsed_id, KitsuStreamId):
            (
                resolved_imdb_id,
                season,
                ep,
                alt_eps,
                alt_seasons,
                resolved_title,
            ) = await self._tvdb_resolver.resolve_kitsu(
                parsed_id.kitsu_id,
                parsed_id.episode,
            )
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
            fallback_title_override = resolved_title
            if not imdb_id and not fallback_title_override:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] Kitsu feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót vagy címet találni a Kitsu ID-hoz ({parsed_id.kitsu_id}).",
                        external_url=app_url,
                    )
                ]
        elif isinstance(parsed_id, MalStreamId):
            (
                resolved_imdb_id,
                season,
                ep,
                alt_eps,
                alt_seasons,
                resolved_title,
            ) = await self._tvdb_resolver.resolve_mal(
                parsed_id.mal_id,
                parsed_id.episode,
            )
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
            fallback_title_override = resolved_title
            if not imdb_id and not fallback_title_override:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] MyAnimeList feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót vagy címet találni a MyAnimeList ID-hoz ({parsed_id.mal_id}).",
                        external_url=app_url,
                    )
                ]
        elif isinstance(parsed_id, TmdbStreamId):
            (
                resolved_imdb_id,
                resolved_title,
            ) = await self._tvdb_resolver.resolve_tmdb(
                parsed_id.tmdb_id,
                is_series=parsed_id.series_info is not None,
            )
            imdb_id = resolved_imdb_id
            series_info = parsed_id.series_info
            fallback_title_override = resolved_title
            if not imdb_id and not fallback_title_override:
                return [
                    StremioStream(
                        name="⚠️ [StremHU] TMDB feloldási hiba",
                        description=f"Nem sikerült IMDb azonosítót vagy címet találni a TMDB ID-hoz ({parsed_id.tmdb_id}).",
                        external_url=app_url,
                    )
                ]
        else:
            imdb_id = parsed_id.imdb_id
            series_info = parsed_id.series_info

        # Tracker keresés azonnali előindítása a háttérben, hogy az epizód-leképezéssel
        # (Cinemeta / Anime-Kitsu HTTP lekérdezésekkel) párhuzamosan fusson
        prefetch_task: asyncio.Task | None = None
        provider = getattr(
            self._torrent_streams_service, "_torrent_source_provider_service", None
        )
        if imdb_id and provider is not None and hasattr(provider, "find_by_imdb_id"):
            prefetch_task = asyncio.create_task(provider.find_by_imdb_id(imdb_id))

        try:
            # Dinamikus epizód leképezés (sorozatoknál) és alternatív IMDb azonosítók feloldása
            alternate_imdb_ids: list[str] | None = None
            if imdb_id and series_info is not None:
                if (
                    series_info.episode is not None
                    and not series_info.alternate_episodes
                ):
                    try:
                        dyn_alt_eps, dyn_alt_seasons = (
                            await self._tvdb_resolver.get_episode_mappings(
                                imdb_id=imdb_id,
                                season=series_info.season,
                                episode=series_info.episode,
                            )
                        )
                        if dyn_alt_eps:
                            series_info.alternate_episodes = dyn_alt_eps
                        if dyn_alt_seasons:
                            series_info.alternate_seasons = dyn_alt_seasons
                    except Exception as e:
                        logger.debug(f"[StremioService] Epizód leképezés hiba: {e}")

            if imdb_id and hasattr(self._tvdb_resolver, "get_alternate_imdb_ids"):
                try:
                    alt_res = await self._tvdb_resolver.get_alternate_imdb_ids(imdb_id)
                    if isinstance(alt_res, list) and alt_res:
                        alternate_imdb_ids = alt_res
                except Exception as e:
                    logger.debug(
                        f"[StremioService] Alternatív IMDb feloldási hiba: {e}"
                    )

            # IMDb alapú stream lekérdezés kereséssel és feloldással
            # A másodlagos név szerinti kereséshez a cím csak akkor kerül lekérésre, ha IMDb-re 0 torrent érkezett
            fallback_fn = None
            if fallback_title_override:
                fallback_fn = fallback_title_override
            elif imdb_id:

                def get_fallback_title():
                    return self._tvdb_resolver.get_title_by_imdb(imdb_id)

                fallback_fn = get_fallback_title

            torrent_streams, errors = await self._torrent_streams_service.find_by_imdb(
                user=user,
                imdb_id=imdb_id,
                series=series_info,
                fallback_title=fallback_fn,
                alternate_imdb_ids=alternate_imdb_ids,
                prefetch_task=prefetch_task,
            )
        finally:
            if prefetch_task is not None and not prefetch_task.done():
                prefetch_task.cancel()

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
