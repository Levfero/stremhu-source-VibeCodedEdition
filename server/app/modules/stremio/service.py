import asyncio
import re
import time
from urllib.parse import unquote

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
    Manifest,
    ManifestBehaviorHints,
    ManifestCatalog,
    ManifestExtra,
    StreamId,
    StremioStream,
    StremioSubtitle,
    TorrentStreamId,
    TvdbStreamId,
)
from app.modules.stremio.tvdb_resolver import TvdbResolver, tvdb_resolver
from app.modules.torrent_streams.service import TorrentStreamsService
from app.modules.users.models import UserModel

_STREAM_CACHE_TTL_SECONDS = 60.0
_MAX_STREAM_CACHE_ENTRIES = 500
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
                ShortManifestResource.SUBTITLES,
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
        """Lekéri a lejátszható streameket az adatbázisból vagy indexerekből (rövid távú gyorsítótárral és in-flight deduplikációval)."""
        cache_key = f"{user.id}:{parsed_id.model_dump_json()}"
        now = time.monotonic()

        cached = _stream_cache.get(cache_key)
        if cached is not None:
            ts, cached_streams = cached
            if now - ts < _STREAM_CACHE_TTL_SECONDS:
                return cached_streams
            _stream_cache.pop(cache_key, None)

        if cache_key in _ongoing_stream_tasks:
            return await _ongoing_stream_tasks[cache_key]

        task = asyncio.create_task(self._get_streams_uncached(user, parsed_id))
        _ongoing_stream_tasks[cache_key] = task
        try:
            streams = await task
            if streams and any(s.url is not None for s in streams):
                if len(_stream_cache) >= _MAX_STREAM_CACHE_ENTRIES:
                    for k in list(_stream_cache.keys())[:100]:
                        _stream_cache.pop(k, None)
                _stream_cache[cache_key] = (time.monotonic(), streams)
            return streams
        finally:
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

        # Dinamikus epizód leképezés és alternatív IMDb azonosítók feloldása (párhuzamosítva)
        mapping_coro = None
        if series_info and series_info.episode is not None and not series_info.alternate_episodes:
            mapping_coro = self._tvdb_resolver.get_episode_mappings(
                imdb_id=imdb_id,
                season=series_info.season,
                episode=series_info.episode,
            )

        alt_imdb_coro = None
        if hasattr(self._tvdb_resolver, "get_alternate_imdb_ids"):
            alt_imdb_coro = self._tvdb_resolver.get_alternate_imdb_ids(imdb_id)

        alternate_imdb_ids: list[str] | None = None
        if mapping_coro and alt_imdb_coro:
            mapping_res, alt_res = await asyncio.gather(
                mapping_coro, alt_imdb_coro, return_exceptions=True
            )
            if isinstance(mapping_res, tuple) and len(mapping_res) == 2:
                dyn_alt_eps, dyn_alt_seasons = mapping_res
                if dyn_alt_eps:
                    series_info.alternate_episodes = dyn_alt_eps
                if dyn_alt_seasons:
                    series_info.alternate_seasons = dyn_alt_seasons
            elif isinstance(mapping_res, Exception):
                logger.debug(f"[StremioService] Epizód leképezés hiba: {mapping_res}")

            if isinstance(alt_res, list):
                alternate_imdb_ids = alt_res
            elif isinstance(alt_res, Exception):
                logger.debug(f"[StremioService] Alternatív IMDb feloldási hiba: {alt_res}")
        elif mapping_coro:
            try:
                dyn_alt_eps, dyn_alt_seasons = await mapping_coro
                if dyn_alt_eps:
                    series_info.alternate_episodes = dyn_alt_eps
                if dyn_alt_seasons:
                    series_info.alternate_seasons = dyn_alt_seasons
            except Exception as e:
                logger.debug(f"[StremioService] Epizód leképezés hiba: {e}")
        elif alt_imdb_coro:
            try:
                alt_res = await alt_imdb_coro
                if isinstance(alt_res, list):
                    alternate_imdb_ids = alt_res
            except Exception as e:
                logger.debug(f"[StremioService] Alternatív IMDb feloldási hiba: {e}")

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

        # Ha valamelyik stream saját torrentjéhez nincs felirat, tartalékként használjuk a többi stream feliratát
        fallback_subtitles = next(
            (s.subtitles for s in stremio_streams if s.subtitles), None
        )
        if fallback_subtitles:
            for s in stremio_streams:
                if not s.subtitles:
                    s.subtitles = fallback_subtitles

        for error in errors:
            stremio_streams.append(
                StremioStream(
                    name="⚠️ [StremHU] Hiba",
                    description=error,
                    external_url=app_url,
                )
            )

        return stremio_streams

    def _parse_extra_params(
        self,
        extra: str | None = None,
        query_params: dict[str, str] | None = None,
    ) -> dict[str, str]:
        params: dict[str, str] = {}
        if extra:
            decoded_extra = unquote(extra)
            for part in decoded_extra.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k.strip()] = v.strip()
        if query_params:
            params.update(query_params)
        return params

    def _find_played_stream(
        self,
        streams: list[StremioStream],
        extra: str | None = None,
        query_params: dict[str, str] | None = None,
    ) -> StremioStream | None:
        params = self._parse_extra_params(extra=extra, query_params=query_params)
        if not params:
            return None

        video_size_str = params.get("videoSize") or params.get("video_size")
        video_size = (
            int(video_size_str)
            if video_size_str and video_size_str.isdigit()
            else None
        )
        filename = params.get("filename")

        # 1. Egyezés pontos videoSize alapján
        if video_size is not None:
            for s in streams:
                if s.behavior_hints and s.behavior_hints.video_size == video_size:
                    return s

        # 2. Egyezés filename alapján
        if filename:
            filename_lower = filename.lower()
            for s in streams:
                if s.behavior_hints and s.behavior_hints.filename:
                    if s.behavior_hints.filename.lower() == filename_lower:
                        return s
                clean_desc = s.description.replace("📁", "").strip().lower()
                if clean_desc and (
                    clean_desc == filename_lower
                    or filename_lower in clean_desc
                    or clean_desc in filename_lower
                ):
                    return s

        return None

    async def get_subtitles(
        self,
        user: UserModel,
        parsed_id: StreamId,
        extra: str | None = None,
        query_params: dict[str, str] | None = None,
    ) -> list[StremioSubtitle]:
        """Lekéri a feliratokat az adott médiához.
        Elsősorban a lejátszott videó torrentjéből származó feliratot adja vissza,
        ha ahhoz nincs felirat, akkor más streamek feliratait fallback-ként.
        """
        streams = await self.get_streams(user, parsed_id)
        if not streams:
            return []

        # Próbáljuk beazonosítani a lejátszott streamet
        played_stream = self._find_played_stream(
            streams, extra=extra, query_params=query_params
        )

        if played_stream:
            if played_stream.subtitles:
                return played_stream.subtitles
            # Ha a lejátszott streamhez nincs felirat, fallback a többire
            for s in streams:
                if s != played_stream and s.subtitles:
                    return s.subtitles

        # Ha nem sikerült egyértelműen beazonosítani a streamet,
        # visszaadjuk a feliratokat deduplikálva
        all_subs: list[StremioSubtitle] = []
        seen_urls: set[str] = set()
        for s in streams:
            if s.subtitles:
                for sub in s.subtitles:
                    if sub.url not in seen_urls:
                        seen_urls.add(sub.url)
                        all_subs.append(sub)
        return all_subs

