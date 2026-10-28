import uuid
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.common.schemas.internal import SeriesInfo
from app.modules.indexer_accounts.models import IndexerAccountModel
from app.modules.indexer_definitions.models import IndexerDefinitionModel
from app.modules.indexers.schemas.internal import IndexerTorrent
from app.modules.media_attributes.models import MediaAttributeModel
from app.modules.media_attributes.parser import parse_torrent_name
from app.modules.stream.schemas import StreamToken
from app.modules.stream.utils.stream_token import generate_stream_token
from app.modules.torrent_files.models import TorrentFileModel
from app.modules.torrent_streams.utils.resolver_helpers import (
    detect_subtitle_language,
    is_sample_or_trash,
)
from app.modules.torrent_streams.utils.stream_file_resolver import StreamFileResolver
from app.modules.users.models import UserModel


class TorrentStreamSubtitle(BaseModel):
    model_config = ConfigDict(
        validate_by_name=True,
        alias_generator=to_camel,
    )

    id: str
    file_name: str
    file_index: int
    file_size: int
    url: str
    lang: str
    label: str | None = None



class TorrentStream(BaseModel):
    model_config = ConfigDict(
        validate_by_name=True,
        alias_generator=to_camel,
        arbitrary_types_allowed=True,
    )

    indexer_account: IndexerAccountModel
    torrent_id: str
    info_hash: str
    torrent_name: str
    file_name: str
    file_size: int
    file_index: int
    play_url: str
    seeders: int | None = None
    attributes: list[MediaAttributeModel | IndexerDefinitionModel] = []
    subtitles: list[TorrentStreamSubtitle] | None = None
    is_persisted_torrent: bool

    @classmethod
    def from_torrent_id(
        cls,
        indexer_torrent: IndexerTorrent,
        torrent_file: TorrentFileModel,
        app_url: str,
        user: UserModel,
    ) -> list["TorrentStream"]:
        indexer_id = indexer_torrent.indexer_account.indexer_id

        torrent_streams: list[TorrentStream] = []

        for file in torrent_file.info.files:
            if not file.is_video:
                continue

            if is_sample_or_trash(file.name):
                continue

            playback_id = str(uuid.uuid4())
            stream_token = generate_stream_token(
                StreamToken(
                    indexer_id=indexer_id,
                    torrent_id=torrent_file.torrent_id,
                    file_index=file.index,
                    playback_id=playback_id,
                )
            )

            matched_subs = StreamFileResolver(torrent_file.info).resolve_subtitles(file)
            subtitles: list[TorrentStreamSubtitle] = []
            for sub_file in matched_subs:
                sub_stream_token = generate_stream_token(
                    StreamToken(
                        indexer_id=indexer_id,
                        torrent_id=torrent_file.torrent_id,
                        file_index=sub_file.index,
                        playback_id=playback_id,
                    )
                )
                lang, label = detect_subtitle_language(sub_file.name, sub_file.path)
                encoded_name = quote(sub_file.name)
                subtitles.append(
                    TorrentStreamSubtitle(
                        id=f"{torrent_file.torrent_id}-{sub_file.index}",
                        file_name=sub_file.name,
                        file_index=sub_file.index,
                        file_size=sub_file.size,
                        url=f"{app_url}/api/{user.api_key}/subtitles/{sub_stream_token}/{encoded_name}",
                        lang=lang,
                        label=label,
                    )
                )
                ext = Path(sub_file.name).suffix.lower()
                if ext in (".ass", ".ssa"):
                    vtt_name = Path(sub_file.name).stem + ".vtt"
                    encoded_vtt = quote(vtt_name)
                    vtt_label = f"{label} (VTT)" if label else "Hungarian (VTT)"
                    subtitles.append(
                        TorrentStreamSubtitle(
                            id=f"{torrent_file.torrent_id}-{sub_file.index}-vtt",
                            file_name=vtt_name,
                            file_index=sub_file.index,
                            file_size=sub_file.size,
                            url=f"{app_url}/api/{user.api_key}/subtitles/{sub_stream_token}/{encoded_vtt}",
                            lang=lang,
                            label=vtt_label,
                        )
                    )

            torrent_streams.append(
                cls(
                    indexer_account=indexer_torrent.indexer_account,
                    torrent_id=torrent_file.torrent_id,
                    info_hash=torrent_file.info_hash,
                    torrent_name=torrent_file.info.name,
                    file_name=file.name,
                    file_size=file.size,
                    file_index=file.index,
                    play_url=f"{app_url}/api/{user.api_key}/stream/{stream_token}",
                    seeders=indexer_torrent.seeders,
                    attributes=[],
                    subtitles=subtitles if subtitles else None,
                    is_persisted_torrent=False,
                )
            )

        return torrent_streams

    @staticmethod
    def resolve_attributes(
        torrent_name: str,
        file_name: str,
        external_fallbacks: list[MediaAttributeModel] | None = None,
    ) -> list[MediaAttributeModel]:
        torrent_attributes = parse_torrent_name(
            name=torrent_name,
            external_fallbacks=external_fallbacks or [],
        )

        file_attributes = parse_torrent_name(
            name=file_name,
            use_fallbacks=False,
        )

        torrent_attr_ids = {attribute.id for attribute in torrent_attributes}
        file_attr_ids = {attribute.id for attribute in file_attributes}

        if not file_attributes:
            return torrent_attributes

        if file_attr_ids == torrent_attr_ids:
            return torrent_attributes

        # Intelligens összefésülés: a fájl attribútumai felülírják a torrent azonos kategóriájú (preference_id) attribútumait
        parsed_attributes = []
        file_pref_ids = {
            file_attribute.preference_id
            for file_attribute in file_attributes
            if file_attribute.preference_id is not None
        }

        for attr in torrent_attributes:
            # Ha a fájl tartalmaz attribútumot ebből a kategóriából, akkor a torrent-szintűt eldobjuk (felülíródik)
            if attr.preference_id is not None and attr.preference_id in file_pref_ids:
                continue
            # Ha nincs kategória, de a fájl már pont tartalmazza ezt az attribútumot, akkor elkerüljük a duplikációt
            if attr.id in file_attr_ids:
                continue
            parsed_attributes.append(attr)

        # Hozzáadjuk a fájl saját attribútumait
        parsed_attributes.extend(file_attributes)

        return parsed_attributes

    @classmethod
    def from_imdb_id(
        cls,
        indexer_torrent: IndexerTorrent,
        torrent_file: TorrentFileModel,
        app_url: str,
        user: UserModel,
        series: SeriesInfo | None = None,
    ) -> "TorrentStream | None":
        resolver = StreamFileResolver(
            torrent_file.info, series, imdb_id=indexer_torrent.imdb_id
        )
        torrent_file_info = resolver.resolve()

        if torrent_file_info is None:
            return None

        parsed_attributes = cls.resolve_attributes(
            torrent_name=torrent_file.info.name,
            file_name=torrent_file_info.name,
            external_fallbacks=indexer_torrent.media_attributes,
        )

        indexer_id = indexer_torrent.indexer_account.indexer_id
        torrent_id = torrent_file.torrent_id
        file_index = torrent_file_info.index
        playback_id = str(uuid.uuid4())

        matched_subs = resolver.resolve_subtitles(torrent_file_info)
        subtitles: list[TorrentStreamSubtitle] = []
        for sub_file in matched_subs:
            sub_stream_token = generate_stream_token(
                StreamToken(
                    indexer_id=indexer_id,
                    torrent_id=torrent_id,
                    file_index=sub_file.index,
                    playback_id=playback_id,
                    imdb_id=indexer_torrent.imdb_id,
                    series_info=series,
                )
            )
            lang, label = detect_subtitle_language(sub_file.name, sub_file.path)
            encoded_name = quote(sub_file.name)
            subtitles.append(
                TorrentStreamSubtitle(
                    id=f"{torrent_id}-{sub_file.index}",
                    file_name=sub_file.name,
                    file_index=sub_file.index,
                    file_size=sub_file.size,
                    url=f"{app_url}/api/{user.api_key}/subtitles/{sub_stream_token}/{encoded_name}",
                    lang=lang,
                    label=label,
                )
            )
            ext = Path(sub_file.name).suffix.lower()
            if ext in (".ass", ".ssa"):
                vtt_name = Path(sub_file.name).stem + ".vtt"
                encoded_vtt = quote(vtt_name)
                vtt_label = f"{label} (VTT)" if label else "Hungarian (VTT)"
                subtitles.append(
                    TorrentStreamSubtitle(
                        id=f"{torrent_id}-{sub_file.index}-vtt",
                        file_name=vtt_name,
                        file_index=sub_file.index,
                        file_size=sub_file.size,
                        url=f"{app_url}/api/{user.api_key}/subtitles/{sub_stream_token}/{encoded_vtt}",
                        lang=lang,
                        label=vtt_label,
                    )
                )

        stream_token = generate_stream_token(
            StreamToken(
                indexer_id=indexer_id,
                torrent_id=torrent_id,
                file_index=file_index,
                playback_id=playback_id,
                imdb_id=indexer_torrent.imdb_id,
                series_info=series,
            )
        )

        return TorrentStream(
            indexer_account=indexer_torrent.indexer_account,
            torrent_id=torrent_id,
            info_hash=torrent_file.info_hash,
            seeders=indexer_torrent.seeders,
            torrent_name=torrent_file.info.name,
            attributes=[
                *parsed_attributes,
                indexer_torrent.indexer_account.indexer_definition,
            ],
            file_name=torrent_file_info.name,
            file_size=torrent_file_info.size,
            file_index=file_index,
            play_url=f"{app_url}/api/{user.api_key}/stream/{stream_token}",
            subtitles=subtitles if subtitles else None,
            is_persisted_torrent=False,
        )
