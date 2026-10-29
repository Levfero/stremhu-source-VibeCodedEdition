import asyncio
import re
import time
from urllib.parse import unquote

from app.common.logger import logger
from app.common.schemas.internal import SeriesInfo
from app.config import NodeEnv, config
from app.modules.settings.service import SettingsService
from app.modules.stream.active_playback import wait_for_active_binge_group
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
    StremioSubtitle,
    TmdbStreamId,
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

    def manifest(self, user: UserModel, endpoint_type: str = "default") -> Manifest:
        app_url = self._settings_service.get_app_url()

        addon_id = "hu.stremhu-source.addon"
        name = "StremHU Source"
        version = config.version.removeprefix("v")

        if endpoint_type != "default":
            ep = self._settings_service.get_endpoint_by_id(endpoint_type)
            if ep:
                name = f"{name} ({ep.name})"
                addon_id = f"{addon_id}.{ep.id.replace('_', '.')}"
                if ep.base_url:
                    app_url = ep.base_url.rstrip("/")
            else:
                target_url = self._settings_service.get_manifest_url(endpoint_type)
                if target_url:
                    app_url = target_url.rstrip("/")
                name = f"{name} ({endpoint_type})"
                addon_id = f"{addon_id}.{endpoint_type.replace('_', '.')}"

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
                ShortManifestResource.SUBTITLES,
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
        endpoint_type: str = "default",
    ) -> list[StremioStream]:
        """Lekéri a lejátszható streameket az adatbázisból vagy indexerekből."""
        dedup_val = getattr(user, "enable_stream_deduplication", True)
        enable_fallback = bool(getattr(user, "enable_fallback_subtitles", False))
        cache_key = f"{user.id}:{endpoint_type}:{dedup_val}:{enable_fallback}:{user.enable_smart_filter}:{user.smart_filter_limit}:{parsed_id.model_dump_json()}"
        now = time.monotonic()
        cached = _stream_cache.get(cache_key)
        if cached is not None:
            ts, cached_streams = cached
            if now - ts < _STREAM_CACHE_TTL_SECONDS:
                return cached_streams
            _stream_cache.pop(cache_key, None)

        if cache_key in _ongoing_stream_tasks:
            return await _ongoing_stream_tasks[cache_key]

        task = asyncio.create_task(
            self._get_streams_uncached(user, parsed_id, endpoint_type)
        )
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
        endpoint_type: str = "default",
    ) -> list[StremioStream]:
        app_url = await asyncio.to_thread(self._settings_service.get_app_url)
        if isinstance(parsed_id, TorrentStreamId):
            torrent_streams = await self._torrent_streams_service.find_by_torrent_id(
                indexer_id=parsed_id.indexer_id,
                torrent_id=parsed_id.torrent_id,
                user=user,
                file_index=parsed_id.file_index,
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

            is_series = series_info is not None
            if imdb_id and hasattr(self._tvdb_resolver, "get_alternate_imdb_ids"):
                try:
                    # Első körben csak a statikus és gyorsítótárazott aliasokat kérjük le (0 ms késleltetés)
                    alt_res = await self._tvdb_resolver.get_alternate_imdb_ids(
                        imdb_id,
                        is_series=is_series,
                        allow_dynamic_search=False,
                    )
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
                    return self._tvdb_resolver.get_title_by_imdb(
                        imdb_id, is_series=is_series
                    )

                fallback_fn = get_fallback_title

            torrent_streams, errors = await self._torrent_streams_service.find_by_imdb(
                user=user,
                imdb_id=imdb_id,
                series=series_info,
                fallback_title=fallback_fn,
                alternate_imdb_ids=alternate_imdb_ids,
                prefetch_task=prefetch_task,
            )

            # Ha az elsődleges keresés 0 streamet adott, lusta fallbackként megpróbáljuk a dinamikus Kitsu alias feloldást:
            if (
                not torrent_streams
                and imdb_id
                and not alternate_imdb_ids
                and is_series
                and hasattr(self._tvdb_resolver, "get_alternate_imdb_ids")
            ):
                try:
                    dyn_aliases = await self._tvdb_resolver.get_alternate_imdb_ids(
                        imdb_id,
                        is_series=True,
                        allow_dynamic_search=True,
                    )
                    if isinstance(dyn_aliases, list) and dyn_aliases:
                        torrent_streams, new_errors = (
                            await self._torrent_streams_service.find_by_imdb(
                                user=user,
                                imdb_id=imdb_id,
                                series=series_info,
                                fallback_title=fallback_fn,
                                alternate_imdb_ids=dyn_aliases,
                            )
                        )
                        errors.extend(new_errors)
                except Exception as e:
                    logger.debug(
                        f"[StremioService] Lusta dinamikus alias keresési hiba: {e}"
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

        # Ha be van kapcsolva a felirat kölcsönzése más találatból, és az adott forrásnál
        # nincs beállítva a teljes torrent letöltése, tartalékként használjuk a többi stream feliratát
        if getattr(user, "enable_fallback_subtitles", False):
            fallback_subtitles = next(
                (
                    s.subtitles
                    for ts, s in zip(torrent_streams, stremio_streams, strict=False)
                    if s.subtitles and self._can_borrow_subtitle(ts)
                ),
                None,
            )
            if fallback_subtitles:
                for ts, s in zip(torrent_streams, stremio_streams, strict=False):
                    if not s.subtitles and self._can_borrow_subtitle(ts):
                        s.subtitles = fallback_subtitles

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
                    if s.subtitles:
                        for sub in s.subtitles:
                            if sub.url and main_app_url in sub.url:
                                sub.url = sub.url.replace(main_app_url, clean_target)

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

    @staticmethod
    def _can_borrow_subtitle(torrent_stream) -> bool:
        acc = getattr(torrent_stream, "indexer_account", None)
        if not acc:
            return True
        if getattr(acc, "download_full_torrent", False):
            return False
        idx_def = getattr(acc, "indexer_definition", None)
        if idx_def and getattr(idx_def, "requires_full_download", False):
            return False
        return True

    async def get_subtitles(
        self,
        user: UserModel,
        parsed_id: StreamId,
        extra: str | None = None,
        query_params: dict[str, str] | None = None,
        endpoint_type: str = "default",
    ) -> list[StremioSubtitle]:
        """Lekéri a feliratokat az adott médiához.
        Elsősorban a lejátszott videó torrentjéből származó feliratot adja vissza;
        másik torrentből csak akkor kölcsönöz feliratot, ha az engedélyezve van.
        """
        streams = await self.get_streams(user, parsed_id, endpoint_type=endpoint_type)
        if not streams:
            return []
        enable_fallback = bool(getattr(user, "enable_fallback_subtitles", False))

        # Próbáljuk beazonosítani a lejátszott streamet (videoSize / filename alapján)
        played_stream = self._find_played_stream(
            streams, extra=extra, query_params=query_params
        )

        playable_streams = [
            s for s in streams if s.url is not None or s.subtitles is not None
        ]
        distinct_sub_urls = {
            sub.url for s in playable_streams if s.subtitles for sub in s.subtitles
        }
        has_stream_without_subs = any(not s.subtitles for s in playable_streams)

        # Ha több különböző torrent van, és a kliens (pl. Nuvio)
        # nem küldött videoSize/filename paramétert, megnézzük az aktív lejátszást
        if (
            played_stream is None
            and len(playable_streams) > 1
            and (
                len(distinct_sub_urls) > 1
                or (not enable_fallback and has_stream_without_subs)
            )
        ):
            valid_binge_groups = {
                s.behavior_hints.binge_group
                for s in playable_streams
                if s.behavior_hints and s.behavior_hints.binge_group
            }
            if valid_binge_groups:
                active_bg = await wait_for_active_binge_group(
                    api_key=user.api_key,
                    valid_binge_groups=valid_binge_groups,
                    timeout=0.4,
                )
                if active_bg:
                    played_stream = next(
                        (
                            s
                            for s in playable_streams
                            if s.behavior_hints
                            and s.behavior_hints.binge_group == active_bg
                        ),
                        None,
                    )

        if played_stream:
            if played_stream.subtitles:
                return played_stream.subtitles
            # Ha a lejátszott streamhez nincs saját felirat, csak akkor kölcsönzünk másikból, ha be van kapcsolva
            if enable_fallback:
                for s in playable_streams:
                    if s != played_stream and s.subtitles:
                        return s.subtitles
            return []
        # Ha még nem indult el lejátszás, csak akkor adunk vissza feliratot,
        # ha minden streamnek van sajátja, vagy ha a feliratkölcsönzés be van kapcsolva
        if enable_fallback or not has_stream_without_subs:
            for s in playable_streams:
                if s.subtitles:
                    return s.subtitles
        return []
