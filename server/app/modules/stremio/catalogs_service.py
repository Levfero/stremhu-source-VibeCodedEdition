import asyncio

from app.common.logger import logger
from app.modules.stremio.constants import (
    SEARCH_ID,
)
from app.modules.stremio.enums import (
    ContentType,
    MediaType,
)
from app.modules.stremio.schemas import (
    MetaDetail,
    MetaPreview,
    ParsedExtra,
    StremioCatalogResponse,
)
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService
from app.modules.torrent_files.schemas import TorrentFileIdentifier
from app.modules.torrent_files.service import TorrentFilesService
from app.modules.torrent_source_provider.service import TorrentSourceProviderService


class StremioCatalogsService:
    def __init__(
        self,
        torrent_files_service: TorrentFilesService,
        torrent_source_provider_service: TorrentSourceProviderService,
        isolated_torrent_files_service: IsolatedTorrentFilesService,
    ):
        self._torrent_files_service = torrent_files_service
        self._torrent_source_provider_service = torrent_source_provider_service
        self._isolated_torrent_files_service = isolated_torrent_files_service

    async def get_catalog(
        self,
        media_type: MediaType,
        catalog_id: str,
        extra: ParsedExtra = ParsedExtra(),
    ):
        if (
            media_type not in (MediaType.MOVIE, MediaType.SERIES)
            or catalog_id != SEARCH_ID
            or not extra.search
        ):
            return StremioCatalogResponse(metas=[])

        parts = extra.search.split("-", 1)
        if len(parts) >= 2 and parts[0] == "t":
            torrent_id = parts[1]
            try:
                meta_previews = await self.get_metas(torrent_id, media_type=media_type)
            except Exception as e:
                logger.error("A lista lekérésénél hiba történt: %s", e)
                meta_previews = []
            return StremioCatalogResponse(metas=meta_previews)

        # Ha nem t-<torrent_id> formátumú, közvetlen név/cím szerinti keresést futtatunk a trackereken
        try:
            torrent_sources, _ = (
                await self._torrent_source_provider_service.find_by_query(extra.search)
            )
            content_type = (
                ContentType.SERIES if media_type == MediaType.SERIES else ContentType.MOVIE
            )
            meta_previews = [
                MetaPreview.from_torrent_file(
                    torrent_source.torrent_file,
                    content_type=content_type,
                )
                for torrent_source in torrent_sources
            ]
        except Exception as e:
            logger.error(
                "Katalógus név szerinti keresési hiba (%s): %s", extra.search, e
            )
            meta_previews = []

        return StremioCatalogResponse(metas=meta_previews)

    async def get_metas(
        self,
        torrent_id: str,
        media_type: MediaType = MediaType.MOVIE,
    ) -> list[MetaPreview]:
        (
            torrent_sources,
            _,
        ) = await self._torrent_source_provider_service.find_by_torrent_id(torrent_id)

        content_type = (
            ContentType.SERIES if media_type == MediaType.SERIES else ContentType.MOVIE
        )
        return [
            MetaPreview.from_torrent_file(
                torrent_source.torrent_file,
                content_type=content_type,
            )
            for torrent_source in torrent_sources
        ]

    async def get_meta(
        self,
        indexer_id: str,
        torrent_id: str,
        media_type: MediaType = MediaType.MOVIE,
    ) -> MetaDetail | None:

        self._isolated_torrent_files_service.touch(
            TorrentFileIdentifier(
                indexer_id=indexer_id,
                torrent_id=torrent_id,
            ),
        )

        torrent_file = await asyncio.to_thread(
            self._torrent_files_service.find_by_id,
            indexer_id=indexer_id,
            torrent_id=torrent_id,
        )

        if not torrent_file:
            torrent_source = (
                await self._torrent_source_provider_service.find_one_by_indexer(
                    indexer_id=indexer_id,
                    torrent_id=torrent_id,
                )
            )

            if torrent_source:
                torrent_file = torrent_source.torrent_file

        if not torrent_file:
            return None

        content_type = (
            ContentType.SERIES if media_type == MediaType.SERIES else ContentType.MOVIE
        )
        return MetaDetail.from_torrent_file(torrent_file, content_type=content_type)
