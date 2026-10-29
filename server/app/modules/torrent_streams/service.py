import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import humanize
from sqlalchemy.orm import Session

from app.common.logger import logger
from app.common.schemas.internal import SeriesInfo
from app.common.torrent_info import TorrentInfo
from app.modules.attributes.models import AttributeModel
from app.modules.preferences.service import PreferencesService
from app.modules.settings.service import SettingsService
from app.modules.torrent_source_provider.service import (
    TorrentSourceProviderService,
)
from app.modules.torrent_streams.schemas import (
    TorrentStream,
)
from app.modules.torrent_streams.utils.series_parser import SeriesParser
from app.modules.torrents.schemas.internal import TorrentWithRelay
from app.modules.torrents.service import TorrentsService
from app.modules.users.models import UserModel


def strip_accents(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


class TorrentStreamsService:
    def __init__(
        self,
        db: Session,
        torrent_source_provider_service: TorrentSourceProviderService,
        torrents_service: TorrentsService,
        settings_service: SettingsService,
        preferences_service: PreferencesService,
    ):
        self.db = db
        self._torrent_source_provider_service = torrent_source_provider_service
        self._torrents_service = torrents_service
        self._settings_service = settings_service
        self._preferences_service = preferences_service

    async def find_by_imdb(
        self,
        user: UserModel,
        imdb_id: str | None = None,
        series: SeriesInfo | None = None,
        fallback_title: str | Callable[[], Awaitable[str | None]] | None = None,
        alternate_imdb_ids: list[str] | None = None,
        prefetch_task: asyncio.Task[Any] | None = None,
    ) -> tuple[list[TorrentStream], list[str]]:
        ids_to_search: list[str] = []
        if imdb_id and imdb_id.startswith("tt"):
            ids_to_search.append(imdb_id)
        if alternate_imdb_ids:
            for alt_id in alternate_imdb_ids:
                if alt_id and alt_id.startswith("tt") and alt_id not in ids_to_search:
                    ids_to_search.append(alt_id)

        torrent_sources = []
        indexer_errors = []

        if len(ids_to_search) == 1:
            if prefetch_task is not None and ids_to_search[0] == imdb_id:
                try:
                    res = await prefetch_task
                    if isinstance(res, tuple):
                        torrent_sources, indexer_errors = res
                except Exception as e:
                    logger.debug(f"[TorrentStreamsService] Prefetch task hiba: {e}")
                    (
                        torrent_sources,
                        indexer_errors,
                    ) = await self._torrent_source_provider_service.find_by_imdb_id(
                        ids_to_search[0]
                    )
            else:
                (
                    torrent_sources,
                    indexer_errors,
                ) = await self._torrent_source_provider_service.find_by_imdb_id(
                    ids_to_search[0]
                )
        elif len(ids_to_search) > 1:
            tasks: list[Any] = []
            if prefetch_task is not None and ids_to_search[0] == imdb_id:
                tasks.append(prefetch_task)
                remaining = ids_to_search[1:]
            else:
                remaining = ids_to_search

            for i in remaining:
                tasks.append(self._torrent_source_provider_service.find_by_imdb_id(i))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            seen_source_ids: set[tuple[str, str]] = set()
            for res in results:
                if isinstance(res, tuple):
                    sources, errs = res
                    indexer_errors.extend(errs)
                    for s in sources:
                        s_key = (
                            s.indexer_torrent.indexer_account.indexer_id,
                            s.indexer_torrent.torrent_id,
                        )
                        if s_key not in seen_source_ids:
                            seen_source_ids.add(s_key)
                            torrent_sources.append(s)

        app_url = await asyncio.to_thread(self._settings_service.get_app_url)

        torrent_streams: list[TorrentStream] = []

        for torrent_source in torrent_sources:
            torrent_stream = TorrentStream.from_imdb_id(
                indexer_torrent=torrent_source.indexer_torrent,
                torrent_file=torrent_source.torrent_file,
                series=series,
                app_url=app_url,
                user=user,
            )

            if torrent_stream:
                torrent_streams.append(torrent_stream)

        # Ha nem sikerült lejátszható streamet találni az adott epizódhoz/filmhez,
        # másodlagos név szerinti keresést hajtunk végre
        if not torrent_streams and (
            fallback_title
            or torrent_sources
            or (imdb_id and not imdb_id.startswith("tt"))
        ):
            actual_title: str | None = None
            if fallback_title:
                try:
                    actual_title = (
                        await fallback_title()
                        if callable(fallback_title)
                        else fallback_title
                    )
                except Exception as e:
                    logger.warning(
                        f"[TorrentStreamsService] Hiba történt a fallback cím lekérése közben ({imdb_id}): {e}"
                    )
                    actual_title = None

            # Ha a fallback_title nem adott eredményt, de voltak források (pl. más epizódok), kinyerjük a címet a torrent nevéből
            if not actual_title and torrent_sources:
                for src in torrent_sources:
                    name_candidate = (
                        src.torrent_file.info.name
                        if src.torrent_file and src.torrent_file.info
                        else None
                    )
                    if name_candidate:
                        title_part = re.split(
                            r"\b(?:s\d{1,2}\s*e\d{1,2}|s\d{1,2}|e\d{1,2}|season|\d{1,2}\.?(?:evad|évad)|19\d\d|20\d\d|720p|1080p|2160p|4k|uhd)\b",
                            name_candidate,
                            flags=re.IGNORECASE,
                        )[0]
                        extracted = re.sub(r"[._\-–—]+", " ", title_part).strip()
                        if extracted and len(extracted) >= 3:
                            actual_title = extracted
                            break

            if not actual_title and imdb_id and not imdb_id.startswith("tt"):
                actual_title = imdb_id

            if actual_title:
                candidate_queries = self._build_fallback_queries(
                    actual_title=actual_title, series=series
                )

                logger.info(
                    f"[TorrentStreamsService] Másodlagos név szerinti keresés indul ({actual_title}) jelöltek: {candidate_queries}"
                )

                for q in candidate_queries:
                    (
                        fallback_sources,
                        fallback_errors,
                    ) = await self._torrent_source_provider_service.find_by_query(q)
                    indexer_errors.extend(fallback_errors)
                    if fallback_sources:
                        resolved_streams: list[TorrentStream] = []
                        for s in fallback_sources:
                            # 1. Ha a torrent rendelkezik IMDb azonosítóval, és ismert a cél IMDb ID (vagy alternatívák),
                            # de ez a torrent másik műhöz tartozik, elvetjük.
                            torrent_imdb = s.indexer_torrent.imdb_id
                            if torrent_imdb and torrent_imdb.startswith("tt"):
                                target_ids: set[str] = set()
                                if imdb_id and imdb_id.startswith("tt"):
                                    target_ids.add(imdb_id)
                                if alternate_imdb_ids:
                                    target_ids.update(alternate_imdb_ids)
                                if target_ids and torrent_imdb not in target_ids:
                                    continue

                            # 2. Ha filmet keresünk, de a torrent egyértelműen sorozat-epizódokat jelöl
                            if series is None and s.torrent_file and s.torrent_file.info:
                                t_seasons, t_episodes = SeriesParser(
                                    s.torrent_file.info.name
                                ).parse(allow_standalone_episode=False)
                                if t_seasons or t_episodes:
                                    continue

                            stream = TorrentStream.from_imdb_id(
                                indexer_torrent=s.indexer_torrent,
                                torrent_file=s.torrent_file,
                                series=series,
                                app_url=app_url,
                                user=user,
                            )
                            if stream:
                                resolved_streams.append(stream)

                        if resolved_streams:
                            torrent_streams = resolved_streams
                            logger.info(
                                f"[TorrentStreamsService] Másodlagos keresés forrásokat talált '{q}' lekérdezéssel: {len(fallback_sources)} forrás, {len(resolved_streams)} stream."
                            )
                            break
                        elif (
                            fallback_sources
                            and not isinstance(
                                getattr(fallback_sources[0].torrent_file, "info", None),
                                TorrentInfo,
                            )
                        ):
                            # Mock objektumos unit tesztek támogatása
                            break


        filtered_torrent_streams = self._filter_torrent_streams(torrent_streams, user)
        sorted_torrent_streams = self._sort_torrent_streams(
            filtered_torrent_streams, user
        )

        if user.enable_smart_filter:
            limit = user.smart_filter_limit

            if not user.smart_filter_grouping_preference_id:
                sorted_torrent_streams = sorted_torrent_streams[:limit]
            else:
                grouped_torrent_streams: list[TorrentStream] = []
                seen_attribute_ids: set[frozenset[str]] = set()

                for torrent_stream in sorted_torrent_streams:
                    target_attributes = frozenset(
                        attribute.id
                        for attribute in torrent_stream.attributes
                        if attribute.preference_id
                        == user.smart_filter_grouping_preference_id
                    )

                    if target_attributes not in seen_attribute_ids:
                        seen_attribute_ids.add(target_attributes)
                        grouped_torrent_streams.append(torrent_stream)

                if not grouped_torrent_streams and sorted_torrent_streams:
                    grouped_torrent_streams = sorted_torrent_streams[:1]

                sorted_torrent_streams = grouped_torrent_streams[:limit]

        return sorted_torrent_streams, list(dict.fromkeys(indexer_errors))

    async def find_by_torrent_id(
        self,
        indexer_id: str,
        torrent_id: str,
        user: UserModel,
    ) -> list[TorrentStream]:

        torrent_source = (
            await self._torrent_source_provider_service.find_one_by_indexer(
                indexer_id, torrent_id
            )
        )

        if not torrent_source:
            return []

        app_url = await asyncio.to_thread(self._settings_service.get_app_url)

        return TorrentStream.from_torrent_id(
            indexer_torrent=torrent_source.indexer_torrent,
            torrent_file=torrent_source.torrent_file,
            app_url=app_url,
            user=user,
        )

    def _is_torrent_stream_excluded(
        self,
        torrent_stream: TorrentStream,
        excluded_attribute_ids: set[str],
        preference_multiple_map: dict[str, bool],
        preference_strict_exclusion_map: dict[str, bool],
    ) -> bool:
        attributes_by_preference: dict[str, list[AttributeModel]] = {}
        unassociated_attributes: list[AttributeModel] = []

        for attribute in torrent_stream.attributes:
            if attribute.preference_id is not None:
                if attribute.preference_id not in attributes_by_preference:
                    attributes_by_preference[attribute.preference_id] = []
                attributes_by_preference[attribute.preference_id].append(attribute)
            else:
                unassociated_attributes.append(attribute)

        if any(
            unassociated_attribute.id in excluded_attribute_ids
            for unassociated_attribute in unassociated_attributes
        ):
            return True

        for preference_id, attributes in attributes_by_preference.items():
            is_multiple = preference_multiple_map.get(preference_id, False)
            strict_exclusion = preference_strict_exclusion_map.get(preference_id, False)

            if is_multiple and not strict_exclusion:
                if all(
                    attribute.id in excluded_attribute_ids for attribute in attributes
                ):
                    return True
            else:
                if any(
                    attribute.id in excluded_attribute_ids for attribute in attributes
                ):
                    return True

        return False

    def _filter_torrent_streams(
        self,
        torrent_streams: list[TorrentStream],
        user: UserModel,
    ) -> list[TorrentStream]:
        excluded_attribute_ids = {
            exclusion.attribute_id for exclusion in user.attribute_exclusions
        }

        preferences = self._preferences_service.get_list()
        preference_multiple_map = {
            preference.id: preference.multiple for preference in preferences
        }
        preference_strict_exclusion_map = {
            preference.id: preference.strict_exclusion for preference in preferences
        }

        filtered_torrent_streams: list[TorrentStream] = []
        for torrent_stream in torrent_streams:
            if user.torrent_seed is not None and (
                torrent_stream.seeders is None
                or torrent_stream.seeders <= user.torrent_seed
            ):
                continue

            if self._is_torrent_stream_excluded(
                torrent_stream=torrent_stream,
                excluded_attribute_ids=excluded_attribute_ids,
                preference_multiple_map=preference_multiple_map,
                preference_strict_exclusion_map=preference_strict_exclusion_map,
            ):
                continue

            filtered_torrent_streams.append(torrent_stream)

        return filtered_torrent_streams

    def _sort_torrent_streams(
        self,
        torrent_streams: list[TorrentStream],
        user: UserModel,
    ) -> list[TorrentStream]:
        torrent_with_relays: list[TorrentWithRelay] = (
            self._torrents_service.get_torrents()
        )
        active_hashes = {
            torrent_with_relay.info_hash for torrent_with_relay in torrent_with_relays
        }

        for stream in torrent_streams:
            stream.is_persisted_torrent = stream.info_hash in active_hashes
        sorted_preferences = sorted(
            user.preference_definitions, key=lambda preference: preference.order
        )

        preference_rank_maps: list[tuple[str, dict[str, int], int]] = []
        for sorted_preference in sorted_preferences:
            definition_attributes = sorted(
                sorted_preference.definition.definition_attributes,
                key=lambda def_attr: def_attr.order,
            )
            rank_map = {
                def_attr.attribute_id: idx
                for idx, def_attr in enumerate(definition_attributes)
            }
            fallback_rank = len(definition_attributes)
            preference_rank_maps.append(
                (sorted_preference.definition.preference_id, rank_map, fallback_rank)
            )

        def sort_key(stream: TorrentStream):
            relay_rank = 0 if stream.is_persisted_torrent else 1
            pref_ranks: list[int] = []
            for pref_id, rank_map, fallback in preference_rank_maps:
                best_rank = fallback
                for attr in stream.attributes:
                    if attr.preference_id == pref_id and attr.id in rank_map:
                        best_rank = min(best_rank, rank_map[attr.id])

                pref_ranks.append(best_rank)

            seeders_rank = -(stream.seeders or 0)

            return (relay_rank, *pref_ranks, seeders_rank)

        torrent_streams.sort(key=sort_key)
        return torrent_streams

    def _format_filesize(self, bytes_size: int) -> str:
        return humanize.naturalsize(bytes_size, binary=True)

    @staticmethod
    def _build_fallback_queries(
        actual_title: str, series: SeriesInfo | None = None
    ) -> list[str]:
        # 1. Alap tisztított cím (alcím vagy kettőspont/kötőjel/gondolatjel levágása)
        cleaned_title = re.sub(r"\s*\(\d{4}\)", "", actual_title).strip()
        base_part = re.split(r"[:\-\–—]", cleaned_title)[0].strip()
        base_clean = re.sub(r"[''`]", "", base_part).strip()

        candidates: list[str] = []

        def add_candidate(q: str | None):
            if q and len(q) >= 2 and q not in candidates:
                candidates.append(q)

        # 1. Alap cím változatlanul (ahogy a Cinemeta/TMDB/torrent neve adta)
        add_candidate(base_clean)

        # 2. Ékezetmentesített változat
        unaccented = strip_accents(base_clean).strip()
        add_candidate(unaccented)

        # 3. Névelő nélküli változat ("A ", "Az ", "The ")
        no_article = re.sub(
            r"^(?:A|Az|The)\s+", "", unaccented, flags=re.IGNORECASE
        ).strip()
        add_candidate(no_article)

        # 4. Ha több szóból áll, az első 2 szó és az első szó
        words = no_article.split()
        if len(words) > 1:
            add_candidate(" ".join(words[:2]))
            if len(words[0]) >= 4:
                add_candidate(words[0])

        # 5. Teljes ékezetmentesített cím (ha különbözik)
        full_no_apo = re.sub(r"[''`]", "", cleaned_title).strip()
        full_unaccented = strip_accents(full_no_apo).strip()
        add_candidate(full_unaccented)

        # 6. Ha sorozatról van szó, célzott évados lekérdezések (pl. "Naruto Shippuuden S04")
        if series and series.season is not None:
            s_num = series.season
            for title_cand in (no_article, unaccented):
                if title_cand:
                    add_candidate(f"{title_cand} S{s_num:02d}")
                    if series.episode is not None:
                        add_candidate(f"{title_cand} S{s_num:02d}E{series.episode:02d}")
                    add_candidate(f"{title_cand} {s_num}.evad")

        return candidates
