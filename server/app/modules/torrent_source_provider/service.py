import asyncio
from collections.abc import Awaitable, Callable
from typing import overload

from app.common.async_utils import owned_to_thread
from app.common.keyed_lock import KeyedLock
from app.common.log_safety import network_error_fields
from app.common.logger import logger
from app.common.singleflight import AdmissionGate, Singleflight
from app.config import config
from app.modules.indexers.schemas.internal import IndexerTorrent
from app.modules.indexers.search import IndexerSearchService
from app.modules.stremio.cache import stream_cache_state
from app.modules.torrent_files.exceptions import InvalidTorrentFileException
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService
from app.modules.torrent_files.models import TorrentFileModel
from app.modules.torrent_files.schemas import TorrentFileIdentifier, TorrentFileSnapshot
from app.modules.torrent_source_provider.schemas import TorrentSource

_torrent_provider_locks = KeyedLock()
_sources_singleflight: Singleflight[tuple[list[TorrentSource], list[str]]] = Singleflight()
_single_source_singleflight: Singleflight[TorrentSource | None] = Singleflight()
_torrent_download_singleflight: Singleflight[tuple[str, str, bytes] | None] = Singleflight()
# CR15/c: egyedi forráskeresés csak szabad slottal indul; a csatlakozó várakozó nem foglal.
_source_search_admission = AdmissionGate(config.source_search_max_parallel)
DEADLINE_ERROR = "A keresés elérte az időkorlátot; a találatok részlegesek."


def open_source_searches() -> None:
    global _sources_singleflight, _single_source_singleflight, _torrent_download_singleflight
    if _sources_singleflight.closed:
        _sources_singleflight = Singleflight()
    if _single_source_singleflight.closed:
        _single_source_singleflight = Singleflight()
    if _torrent_download_singleflight.closed:
        _torrent_download_singleflight = Singleflight()


async def close_source_searches() -> None:
    await asyncio.gather(_sources_singleflight.aclose(), _single_source_singleflight.aclose(), _torrent_download_singleflight.aclose())


class TorrentSourceProviderService:
    def __init__(
        self,
        indexers_service: IndexerSearchService,
        isolated_torrent_files_service: IsolatedTorrentFilesService,
    ):
        self._indexers_service = indexers_service
        self._isolated_torrent_files_service = isolated_torrent_files_service

    def _shared_search(self) -> "_SourceSearch":
        # A megosztott task csak request-független függőséget kap, a providert nem.
        return _SourceSearch(self._indexers_service, self._isolated_torrent_files_service)

    async def find_by_imdb_id(
        self,
        imdb_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        search = self._shared_search()
        return await _sources_singleflight.run(
            f"{stream_cache_state.revision('')[0]}:imdb:{imdb_id}",
            lambda: search.find_by_imdb_id(imdb_id),
            _source_search_admission,
        )

    async def find_by_query(
        self,
        query: str,
        media_type: str | None = None,
    ) -> tuple[list[TorrentSource], list[str]]:
        search = self._shared_search()
        return await _sources_singleflight.run(
            f"{stream_cache_state.revision('')[0]}:query:{query}:{media_type or 'all'}",
            lambda: search.find_by_query(query, media_type=media_type),
            _source_search_admission,
        )

    async def find_by_torrent_id(
        self,
        torrent_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        search = self._shared_search()
        return await _sources_singleflight.run(
            f"{stream_cache_state.revision('')[0]}:torrent:{torrent_id}",
            lambda: search.find_by_torrent_id(torrent_id),
            _source_search_admission,
        )

    async def find_one_by_indexer(
        self,
        indexer_id: str,
        torrent_id: str,
    ) -> TorrentSource | None:
        search = self._shared_search()
        return await _single_source_singleflight.run(
            f"{stream_cache_state.revision('')[0]}:indexer:{indexer_id}:{torrent_id}",
            lambda: search.find_one_by_indexer(indexer_id, torrent_id),
            _source_search_admission,
        )

    async def _download_and_save_torrent(
        self, indexer_torrent: IndexerTorrent
    ) -> TorrentFileModel | None:
        async with _torrent_provider_locks(
            f"{indexer_torrent.indexer_account.indexer_id}:{indexer_torrent.torrent_id}"
        ):
            existing_torrent = await owned_to_thread(
                self._isolated_torrent_files_service.find_by_id,
                indexer_id=indexer_torrent.indexer_account.indexer_id,
                torrent_id=indexer_torrent.torrent_id,
            )
            if existing_torrent:
                return existing_torrent

            try:
                downloaded_torrent_file = await self._indexers_service.download_torrent(
                    indexer_torrent.indexer_account.indexer_id,
                    indexer_torrent.torrent_id,
                    indexer_torrent.download_url,
                )

                return await owned_to_thread(
                    self._isolated_torrent_files_service.create,
                    indexer_id=downloaded_torrent_file.indexer_id,
                    torrent_id=downloaded_torrent_file.torrent_id,
                    torrent_bytes=downloaded_torrent_file.torrent_bytes,
                )
            except InvalidTorrentFileException:
                logger.warning(
                    f"⚠️ Érvénytelen torrent fájl átugorva: indexer: {indexer_torrent.indexer_account.indexer_id}, torrent_id: {indexer_torrent.torrent_id}"
                )
                return None


class _SourceSearch:
    """Egy megosztott keresés teljes munkája; csak request-független függőséget tart."""

    def __init__(
        self,
        indexers: IndexerSearchService,
        files: IsolatedTorrentFilesService,
    ):
        self._indexers = indexers
        self._files = files
        self._deadline: float | None = None
        self._deadline_hit = False

    def _start_deadline(self) -> None:
        # A határ a tulajdonolt task indulásakor kezdődik, nem a várakozónál.
        self._deadline = asyncio.get_running_loop().time() + config.source_search_deadline_seconds

    def _remaining(self) -> float:
        if self._deadline is None:
            return config.source_search_deadline_seconds
        return self._deadline - asyncio.get_running_loop().time()

    def _errors(self, errors: list[str]) -> list[str]:
        return [*errors, DEADLINE_ERROR] if self._deadline_hit else errors

    async def _search_stage(
        self, search: Callable[[], Awaitable[tuple[list[IndexerTorrent], list[str]]]]
    ) -> tuple[list[IndexerTorrent], list[str]]:
        """Indexerkeresés a maradék kereten belül; lejáratkor üres, jelölt részleges eredmény."""
        try:
            async with asyncio.timeout(max(0.0, self._remaining())):
                return await search()
        except TimeoutError:
            self._deadline_hit = True
            return [], []

    async def find_by_imdb_id(
        self,
        imdb_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        self._start_deadline()
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._search_stage(lambda: self._indexers.get_torrents_by_imdb_id(imdb_id))

        torrent_files = await self._sync_torrent_files(indexer_torrents)

        return torrent_files, self._errors(indexer_errors)

    async def find_by_query(
        self,
        query: str,
        media_type: str | None = None,
    ) -> tuple[list[TorrentSource], list[str]]:
        self._start_deadline()
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._search_stage(
            lambda: self._indexers.get_torrents_by_query(query, media_type=media_type)
        )

        # M09: Méltányos jelöltfeldolgozás (fair interleaving) a merev top 30 helyett.
        # Trackerenként rendezünk seeder szerint, majd váltakozva választunk találatokat,
        # hogy egy domináns tracker ne szorítson ki más indexereket.
        by_indexer: dict[str, list[IndexerTorrent]] = {}
        for it in sorted(
            indexer_torrents,
            key=lambda t: getattr(t, "seeders", 0) or 0,
            reverse=True,
        ):
            iid = it.indexer_account.indexer_id
            by_indexer.setdefault(iid, []).append(it)

        fair_selected: list[IndexerTorrent] = []
        max_candidates = 40
        while len(fair_selected) < max_candidates and any(by_indexer.values()):
            for iid in list(by_indexer.keys()):
                items = by_indexer[iid]
                if items:
                    fair_selected.append(items.pop(0))
                    if len(fair_selected) >= max_candidates:
                        break
                else:
                    by_indexer.pop(iid, None)

        torrent_files = await self._sync_torrent_files(fair_selected)

        return torrent_files, self._errors(indexer_errors)

    async def find_by_torrent_id(
        self,
        torrent_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        self._start_deadline()
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._search_stage(lambda: self._indexers.get_torrents_by_torrent_id(torrent_id))

        torrent_files = await self._sync_torrent_files(indexer_torrents)

        return torrent_files, self._errors(indexer_errors)

    async def find_one_by_indexer(
        self,
        indexer_id: str,
        torrent_id: str,
    ) -> TorrentSource | None:
        self._start_deadline()
        async with asyncio.timeout(max(0.0, self._remaining())):
            indexer_torrent = await self._indexers.get_torrent_by_torrent_id(
                indexer_id, torrent_id
            )

        torrent_file = await self._sync_torrent_files(indexer_torrent)

        return torrent_file

    @overload
    async def _sync_torrent_files(
        self,
        indexer_torrents: IndexerTorrent,
    ) -> TorrentSource | None: ...

    @overload
    async def _sync_torrent_files(
        self,
        indexer_torrents: list[IndexerTorrent],
    ) -> list[TorrentSource]: ...
    async def _sync_torrent_files(
        self,
        indexer_torrents: list[IndexerTorrent] | IndexerTorrent,
    ) -> list[TorrentSource] | TorrentSource | None:
        is_single = isinstance(indexer_torrents, IndexerTorrent)
        if is_single:
            indexer_torrents = [indexer_torrents]

        torrent_file_ids: list[TorrentFileIdentifier] = [
            TorrentFileIdentifier(
                indexer_id=indexer_torrent.indexer_account.indexer_id,
                torrent_id=indexer_torrent.torrent_id,
            )
            for indexer_torrent in indexer_torrents
        ]

        current_torrent_files = await owned_to_thread(
            self._find_snapshots, torrent_file_ids
        )

        self._files.touch(torrent_file_ids)

        current_torrent_files_map: dict[tuple[str, str], TorrentFileSnapshot] = {
            (
                current_torrent_file.indexer_id,
                current_torrent_file.torrent_id,
            ): current_torrent_file
            for current_torrent_file in current_torrent_files
        }

        missing = [
            indexer_torrent
            for indexer_torrent in indexer_torrents
            if (
                indexer_torrent.indexer_account.indexer_id,
                indexer_torrent.torrent_id,
            )
            not in current_torrent_files_map
        ]
        created = await self._download_and_store(missing)
        # Determinisztikus sorrend: a jelöltek sorrendje, nem a letöltések befejezési sorrendje.
        created_torrent_files = [
            created[key]
            for key in dict.fromkeys(
                (indexer_torrent.indexer_account.indexer_id, indexer_torrent.torrent_id)
                for indexer_torrent in missing
            )
            if key in created
        ]

        torrent_files = current_torrent_files + created_torrent_files

        indexer_torrent_map: dict[tuple[str, str], IndexerTorrent] = {
            (
                indexer_torrent.indexer_account.indexer_id,
                indexer_torrent.torrent_id,
            ): indexer_torrent
            for indexer_torrent in indexer_torrents
        }

        torrent_sources: list[TorrentSource] = []
        for torrent_file in torrent_files:
            indexer_torrent = indexer_torrent_map.get(
                (
                    torrent_file.indexer_id,
                    torrent_file.torrent_id,
                )
            )
            if indexer_torrent is None:
                continue

            torrent_sources.append(
                TorrentSource(
                    indexer_torrent=indexer_torrent,
                    torrent_file=torrent_file,
                )
            )

        if is_single:
            return torrent_sources[0] if torrent_sources else None

        return torrent_sources

    async def _download_and_store(
        self, missing: list[IndexerTorrent]
    ) -> dict[tuple[str, str], TorrentFileSnapshot]:
        """Rögzített számú worker tölt le; a kész bájtok bájtkorlátos kötegekben kerülnek a DB-be.

        Egyszerre legfeljebb egy köteg mentése fut, a többi worker közben letölthet. A határidő
        után új letöltés nem indul, a folyamatban lévők várakozója megszakad; a közös
        letöltési task és a szálmunka a saját befejezéséig fut.
        """
        created: dict[tuple[str, str], TorrentFileSnapshot] = {}
        if not missing:
            return created
        pending: list[tuple[str, str, bytes]] = []
        pending_bytes = 0
        store_lock = asyncio.Lock()
        candidates = iter(missing)

        async def flush() -> None:
            nonlocal pending, pending_bytes
            async with store_lock:
                items, pending, pending_bytes = pending, [], 0
                if items:
                    for snapshot in await owned_to_thread(self._store_snapshots, items):
                        created[(snapshot.indexer_id, snapshot.torrent_id)] = snapshot

        async def worker() -> None:
            nonlocal pending_bytes
            for indexer_torrent in candidates:
                remaining = self._remaining()
                if remaining <= 0:
                    self._deadline_hit = True
                    return
                try:
                    async with asyncio.timeout(remaining):
                        result = await self._download_torrent_bytes(indexer_torrent)
                except TimeoutError:
                    self._deadline_hit = True
                    return
                except Exception as error:
                    logger.error("operation=torrent-processing %s", network_error_fields(error))
                    continue
                if result is None:
                    continue
                pending.append(result)
                pending_bytes += len(result[2])
                if pending_bytes >= config.source_search_batch_max_bytes:
                    await flush()

        async with asyncio.TaskGroup() as group:
            for _ in range(min(config.source_search_download_workers, len(missing))):
                group.create_task(worker())
        await flush()
        return created

    def _find_snapshots(
        self, identifiers: list[TorrentFileIdentifier]
    ) -> list[TorrentFileSnapshot]:
        # Saját rövid Session: a tulajdonos request lezárása a megosztott taskot nem érinti.
        return self._files.find_snapshots(identifiers)

    def _store_snapshots(
        self, items: list[tuple[str, str, bytes]]
    ) -> list[TorrentFileSnapshot]:
        return [
            TorrentFileSnapshot.from_model(torrent_file)
            for torrent_file in self._files.create_many(items)
        ]

    async def _download_torrent_bytes(
        self, indexer_torrent: IndexerTorrent
    ) -> tuple[str, str, bytes] | None:
        key = f"{indexer_torrent.indexer_account.indexer_id}:{indexer_torrent.torrent_id}"

        async def _do_download() -> tuple[str, str, bytes] | None:
            try:
                downloaded_torrent_file = await self._indexers.download_torrent(
                    indexer_torrent.indexer_account.indexer_id,
                    indexer_torrent.torrent_id,
                    indexer_torrent.download_url,
                )
                return (
                    downloaded_torrent_file.indexer_id,
                    downloaded_torrent_file.torrent_id,
                    downloaded_torrent_file.torrent_bytes,
                )
            except InvalidTorrentFileException:
                logger.warning(
                    f"⚠️ Érvénytelen torrent fájl átugorva: indexer: {indexer_torrent.indexer_account.indexer_id}, torrent_id: {indexer_torrent.torrent_id}"
                )
                return None

        return await _torrent_download_singleflight.run(key, _do_download)
