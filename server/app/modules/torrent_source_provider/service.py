import asyncio
from collections.abc import Awaitable
from typing import overload

from app.common.keyed_lock import KeyedLock
from app.common.logger import logger
from app.common.singleflight import Singleflight
from app.modules.indexers.schemas.internal import IndexerTorrent
from app.modules.indexers.service import IndexersService
from app.modules.torrent_files.exceptions import InvalidTorrentFileException
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService
from app.modules.torrent_files.models import TorrentFileModel
from app.modules.torrent_files.schemas import TorrentFileIdentifier, TorrentFilesFilter
from app.modules.torrent_files.service import TorrentFilesService
from app.modules.torrent_source_provider.schemas import TorrentSource

_torrent_provider_locks = KeyedLock()
_sources_singleflight: Singleflight[tuple[list[TorrentSource], list[str]]] = Singleflight()
_single_source_singleflight: Singleflight[TorrentSource | None] = Singleflight()
_torrent_download_singleflight: Singleflight[tuple[str, str, bytes] | None] = Singleflight()


class TorrentSourceProviderService:
    def __init__(
        self,
        indexers_service: IndexersService,
        torrent_files_service: TorrentFilesService,
        isolated_torrent_files_service: IsolatedTorrentFilesService,
    ):
        self._indexers_service = indexers_service
        self._torrent_files_service = torrent_files_service
        self._isolated_torrent_files_service = isolated_torrent_files_service

    async def find_by_imdb_id(
        self,
        imdb_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        return await _sources_singleflight.run(
            f"imdb:{imdb_id}",
            lambda: self._find_by_imdb_id(imdb_id),
        )

    async def _find_by_imdb_id(
        self,
        imdb_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._indexers_service.get_torrents_by_imdb_id(imdb_id)

        torrent_files = await self._sync_torrent_files(indexer_torrents)

        return torrent_files, indexer_errors

    async def find_by_query(
        self,
        query: str,
        media_type: str | None = None,
    ) -> tuple[list[TorrentSource], list[str]]:
        return await _sources_singleflight.run(
            f"query:{query}:{media_type or 'all'}",
            lambda: self._find_by_query(query, media_type=media_type),
        )

    async def _find_by_query(
        self,
        query: str,
        media_type: str | None = None,
    ) -> tuple[list[TorrentSource], list[str]]:
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._indexers_service.get_torrents_by_query(
            query, media_type=media_type
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

        return torrent_files, indexer_errors

    async def find_by_torrent_id(
        self,
        torrent_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        return await _sources_singleflight.run(
            f"torrent:{torrent_id}",
            lambda: self._find_by_torrent_id(torrent_id),
        )

    async def _find_by_torrent_id(
        self,
        torrent_id: str,
    ) -> tuple[list[TorrentSource], list[str]]:
        (
            indexer_torrents,
            indexer_errors,
        ) = await self._indexers_service.get_torrents_by_torrent_id(torrent_id)

        torrent_files = await self._sync_torrent_files(indexer_torrents)

        return torrent_files, indexer_errors

    async def find_one_by_indexer(
        self,
        indexer_id: str,
        torrent_id: str,
    ) -> TorrentSource | None:
        return await _single_source_singleflight.run(
            f"indexer:{indexer_id}:{torrent_id}",
            lambda: self._find_one_by_indexer(indexer_id, torrent_id),
        )

    async def _find_one_by_indexer(
        self,
        indexer_id: str,
        torrent_id: str,
    ) -> TorrentSource | None:
        indexer_torrent = await self._indexers_service.get_torrent_by_torrent_id(
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

        current_torrent_files = await asyncio.to_thread(
            self._torrent_files_service.find_list,
            filter=TorrentFilesFilter(identifiers=torrent_file_ids),
        )

        self._isolated_torrent_files_service.touch(torrent_file_ids)

        current_torrent_files_map: dict[tuple[str, str], TorrentFileModel] = {
            (
                current_torrent_file.indexer_id,
                current_torrent_file.torrent_id,
            ): current_torrent_file
            for current_torrent_file in current_torrent_files
        }

        download_tasks: list[Awaitable[tuple[str, str, bytes] | None]] = []
        for indexer_torrent in indexer_torrents:
            if (
                indexer_torrent.indexer_account.indexer_id,
                indexer_torrent.torrent_id,
            ) in current_torrent_files_map:
                continue

            download_tasks.append(self._download_torrent_bytes(indexer_torrent))

        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        downloaded_items: list[tuple[str, str, bytes]] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.exception("⚠️ Hiba a torrent feldolgozása során", exc_info=result)
                continue
            if result is not None:
                downloaded_items.append(result)

        created_torrent_files: list[TorrentFileModel] = []
        if downloaded_items:
            created_torrent_files = await asyncio.to_thread(
                self._isolated_torrent_files_service.create_many,
                downloaded_items,
            )

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

    async def _download_torrent_bytes(
        self, indexer_torrent: IndexerTorrent
    ) -> tuple[str, str, bytes] | None:
        key = f"{indexer_torrent.indexer_account.indexer_id}:{indexer_torrent.torrent_id}"

        async def _do_download() -> tuple[str, str, bytes] | None:
            try:
                downloaded_torrent_file = await self._indexers_service.download_torrent(
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

    async def _download_and_save_torrent(
        self, indexer_torrent: IndexerTorrent
    ) -> TorrentFileModel | None:
        async with _torrent_provider_locks(
            f"{indexer_torrent.indexer_account.indexer_id}:{indexer_torrent.torrent_id}"
        ):
            existing_torrent = await asyncio.to_thread(
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

                return await asyncio.to_thread(
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
