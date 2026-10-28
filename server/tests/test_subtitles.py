from unittest.mock import MagicMock

import pytest

from app.common.schemas.internal import SeriesInfo
from app.common.torrent_info import TorrentFileInfo, TorrentInfo
from app.modules.indexer_accounts.models import IndexerAccountModel
from app.modules.indexer_definitions.models import IndexerDefinitionModel
from app.modules.stream.utils.subtitles import ass_to_vtt
from app.modules.stremio.enums import ShortManifestResource
from app.modules.stremio.schemas import StremioStream, StremioSubtitle
from app.modules.stremio.service import StremioService
from app.modules.torrent_streams.schemas import TorrentStream, TorrentStreamSubtitle
from app.modules.torrent_streams.utils.resolver_helpers import (
    detect_subtitle_language,
    is_extra_file,
    is_extra_folder,
)
from app.modules.torrent_streams.utils.stream_file_resolver import StreamFileResolver
from app.modules.users.models import UserModel


def make_file(
    path: str, index: int, size: int = 1000, is_video: bool = True
) -> TorrentFileInfo:
    name = path.split("/")[-1]
    ext = "." + name.split(".")[-1].lower() if "." in name else ""
    is_sub = ext in {".srt", ".ass", ".ssa", ".vtt", ".sub"}
    return TorrentFileInfo(
        name=name,
        path=path,
        size=size,
        offset=index * 1000,
        start_piece_index=index,
        end_piece_index=index,
        is_video=is_video,
        is_subtitle=is_sub,
        index=index,
    )


def test_extra_folder_and_file_helpers():
    assert is_extra_folder("Extras") is True
    assert is_extra_folder("extras") is True
    assert is_extra_folder("[Extras]") is True
    assert is_extra_folder("00. Extrák") is True
    assert is_extra_folder("Season 2 Extras") is True
    assert is_extra_folder("Season 1") is False

    assert is_extra_file("Noragami Aragoto - Extra 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - SP 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - 01.mkv") is False


def test_detect_subtitle_language():
    assert detect_subtitle_language("Noragami.hun.ass")[0] == "hun"
    assert detect_subtitle_language("Noragami.hu.srt")[0] == "hun"
    assert detect_subtitle_language("Noragami [HunSub].ass")[0] == "hun"
    assert detect_subtitle_language("Noragami.eng.srt")[0] == "eng"
    assert detect_subtitle_language("Noragami.ger.srt")[0] == "ger"
    assert detect_subtitle_language("Noragami.hun.forced.srt") == (
        "hun",
        "Hungarian (Forced)",
    )
    assert detect_subtitle_language("01.ass") == ("hun", "Hungarian (Torrent)")
    assert detect_subtitle_language("Noragami.fre.srt")[0] == "fre"
    assert detect_subtitle_language("Noragami.spa.srt")[0] == "spa"
    assert detect_subtitle_language("Noragami.ita.srt")[0] == "ita"
    assert detect_subtitle_language("Noragami.rus.srt")[0] == "rus"
    # Ismeretlen / nyelvjelölés nélküli feliratok alapértelmezetten magyarként kezelendők
    assert detect_subtitle_language("subtitles.srt")[0] == "hun"
    assert detect_subtitle_language("ep01.ass")[0] == "hun"
    assert detect_subtitle_language("Noragami - 01.ass")[0] == "hun"
    assert detect_subtitle_language("Kaland es Mas - 01.srt")[0] == "hun"
    assert detect_subtitle_language("Film de jo - 01.srt")[0] == "hun"
    assert detect_subtitle_language("ep01.en.srt")[0] == "eng"
    assert detect_subtitle_language("ep01.de.srt")[0] == "ger"


def test_resolve_subtitles_ignores_other_episodes_in_packs():
    # Két rész egy pakkban és mindkettőhöz felirat
    v1 = make_file("Attack on Titan/Attack on Titan - HunSub - 01 [1080p].mkv", 0)
    v2 = make_file("Attack on Titan/Attack on Titan - HunSub - 02 [1080p].mkv", 1)
    s1 = make_file("Attack on Titan/Attack on Titan - 01.ass", 2, is_video=False)
    s2 = make_file("Attack on Titan/Attack on Titan - 02.ass", 3, is_video=False)

    t_info = TorrentInfo(
        info_hash="hash_aot",
        name="Attack on Titan [1080p]",
        size=4000,
        piece_size=1000,
        files=[v1, v2, s1, s2],
    )

    resolver_ep1 = StreamFileResolver(t_info, series=SeriesInfo(season=1, episode=1))
    resolved_subs_ep1 = resolver_ep1.resolve_subtitles(v1)
    assert len(resolved_subs_ep1) == 1
    assert resolved_subs_ep1[0].name == "Attack on Titan - 01.ass"

    resolver_ep2 = StreamFileResolver(t_info, series=SeriesInfo(season=1, episode=2))
    resolved_subs_ep2 = resolver_ep2.resolve_subtitles(v2)
    assert len(resolved_subs_ep2) == 1
    assert resolved_subs_ep2[0].name == "Attack on Titan - 02.ass"


@pytest.mark.anyio
async def test_get_subtitles_played_stream_matching_and_fallback():
    mock_settings = MagicMock()
    mock_settings.get_app_url.return_value = "http://test-app.com"
    mock_torrent_streams = MagicMock()

    service = StremioService(
        torrent_streams_service=mock_torrent_streams,
        settings_service=mock_settings,
    )
    user = UserModel(id="test-user", username="test")

    sub_t1 = StremioSubtitle(
        id="t1-sub",
        url="http://test-app.com/sub/t1.vtt",
        lang="hun",
        label="Hungarian",
    )

    from app.modules.stremio.schemas import BehaviorHints, ImdbStreamId
    # Stream 1 felirattal
    stream1 = StremioStream(
        name="1080p",
        description="📁 video1.mkv",
        url="http://test-app.com/video1.mkv",
        subtitles=[sub_t1],
        behavior_hints=BehaviorHints(filename="video1.mkv", video_size=500000),
    )
    # Stream 2 saját felirat nélkül
    stream2 = StremioStream(
        name="720p",
        description="📁 video2.mkv",
        url="http://test-app.com/video2.mkv",
        subtitles=None,
        behavior_hints=BehaviorHints(filename="video2.mkv", video_size=300000),
    )

    # MagicMock for async get_streams
    async def mock_get_streams(*args, **kwargs):
        return [stream1, stream2]

    service.get_streams = mock_get_streams

    parsed_id = ImdbStreamId(imdb_id="tt123456", season=1, episode=1)

    # 1. Ha a video1-et játsszuk le: a saját feliratát adja vissza
    subs1 = await service.get_subtitles(
        user=user,
        parsed_id=parsed_id,
        extra="filename=video1.mkv&videoSize=500000",
    )
    assert len(subs1) == 1
    assert subs1[0].id == "t1-sub"

    # 2. Ha a video2-t játsszuk le (amiben nincs felirat): fallback a video1 feliratára
    subs2 = await service.get_subtitles(
        user=user,
        parsed_id=parsed_id,
        extra="filename=video2.mkv&videoSize=300000",
    )
    assert len(subs2) == 1
    assert subs2[0].id == "t1-sub"



def test_resolve_subtitles_for_series():
    v1 = make_file("Noragami Aragoto/Noragami Aragoto - 01 [1080p].mkv", 0)
    v2 = make_file("Noragami Aragoto/Noragami Aragoto - 02 [1080p].mkv", 1)
    s1 = make_file(
        "Noragami Aragoto/Noragami Aragoto - 01 [1080p].ass", 2, is_video=False
    )
    s2 = make_file(
        "Noragami Aragoto/Noragami Aragoto - 02 [1080p].ass", 3, is_video=False
    )
    extra_sub = make_file("Noragami Aragoto/Extras/01.ass", 4, is_video=False)

    t_info = TorrentInfo(
        info_hash="hash_sub_pack",
        name="Noragami Aragoto [1080p]",
        size=4000,
        piece_size=1000,
        files=[v1, v2, s1, s2, extra_sub],
    )

    resolver = StreamFileResolver(t_info, series=SeriesInfo(season=2, episode=1))
    resolved_subs = resolver.resolve_subtitles(v1)

    assert len(resolved_subs) == 1
    assert resolved_subs[0].name == "Noragami Aragoto - 01 [1080p].ass"
    assert extra_sub not in resolved_subs


def test_stremio_stream_includes_subtitles():
    dummy_indexer_def = IndexerDefinitionModel(
        id="ncore", name="ncore", url="https://ncore.pro", details_path=""
    )
    dummy_indexer_account = IndexerAccountModel(
        indexer_id="ncore", username="test", password="pwd"
    )
    dummy_indexer_account.indexer_definition = dummy_indexer_def

    sub = TorrentStreamSubtitle(
        id="sub1",
        file_name="01.ass",
        file_index=1,
        file_size=50000,
        url="http://localhost:7070/api/key/subtitles/token/01.ass",
        lang="hun",
        label="Hungarian (Torrent)",
    )

    t_stream = TorrentStream(
        indexer_account=dummy_indexer_account,
        torrent_id="1",
        info_hash="hash",
        torrent_name="Noragami Aragoto",
        file_name="Noragami Aragoto - 01.mkv",
        file_size=1000000,
        file_index=0,
        play_url="http://localhost:7070/api/key/stream/token",
        attributes=[dummy_indexer_def],
        subtitles=[sub],
        is_persisted_torrent=False,
    )

    stremio_stream = StremioStream.from_imdb_torrent_stream(t_stream, is_series=True)
    assert stremio_stream.subtitles is not None
    assert len(stremio_stream.subtitles) == 1
    assert stremio_stream.subtitles[0].id == "sub1"
    assert (
        stremio_stream.subtitles[0].url
        == "http://localhost:7070/api/key/subtitles/token/01.ass"
    )
    assert stremio_stream.subtitles[0].lang == "hun"
    assert stremio_stream.subtitles[0].label == "Hungarian (Torrent)"


def test_ass_to_vtt_conversion():
    ass_sample = """[Script Info]
Title: Noragami Aragoto 03
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:01:23.45,0:01:27.89,Default,,0,0,0,,{\\pos(192,200)}Szia, ez egy teszt felirat!\\NMásodik sor.
Dialogue: 0,0:02:10.00,0:02:15.50,Default,,0,0,0,,{\\c&H0000FF&}{\\b1}Vastag és színes{\\b0}
"""
    vtt = ass_to_vtt(ass_sample)
    assert vtt.startswith("WEBVTT\n")
    assert "00:01:23.450 --> 00:01:27.890" in vtt
    assert "Szia, ez egy teszt felirat!\nMásodik sor." in vtt
    assert "00:02:10.000 --> 00:02:15.500" in vtt
    assert "Vastag és színes" in vtt
    assert "{\\pos" not in vtt
    assert "{\\c&H" not in vtt


def test_manifest_includes_subtitles_resource():
    mock_settings = MagicMock()
    mock_settings.get_app_url.return_value = "http://test-app.com"
    mock_torrent_streams = MagicMock()

    service = StremioService(
        torrent_streams_service=mock_torrent_streams,
        settings_service=mock_settings,
    )
    user = UserModel(id="test-user", username="test")
    manifest = service.manifest(user)

    assert ShortManifestResource.SUBTITLES in manifest.resources
    assert "subtitles" in [r.value for r in manifest.resources]


@pytest.mark.anyio
async def test_get_streams_and_subtitles_caching_deduplication():
    import asyncio
    from unittest.mock import AsyncMock

    from app.modules.stremio.schemas import ImdbStreamId
    from app.modules.stremio.tvdb_resolver import TvdbResolver

    mock_settings = MagicMock()
    mock_settings.get_app_url.return_value = "http://test-app.com"

    dummy_indexer_def = IndexerDefinitionModel(
        id="ncore", name="ncore", url="https://ncore.pro", details_path=""
    )
    dummy_indexer_account = IndexerAccountModel(
        indexer_id="ncore", username="test", password="pwd"
    )
    dummy_indexer_account.indexer_definition = dummy_indexer_def

    sub = TorrentStreamSubtitle(
        id="sub1",
        file_name="01.vtt",
        file_index=1,
        file_size=50000,
        url="http://test-app.com/api/key/subtitles/token/01.vtt",
        lang="hun",
        label="Hungarian",
    )
    t_stream = TorrentStream(
        indexer_account=dummy_indexer_account,
        torrent_id="1",
        info_hash="hash_cache_test",
        torrent_name="Test Movie",
        file_name="Test.Movie.1080p.mkv",
        file_size=1000000,
        file_index=0,
        play_url="http://test-app.com/api/key/stream/token",
        attributes=[dummy_indexer_def],
        subtitles=[sub],
        is_persisted_torrent=False,
    )

    mock_torrent_streams = AsyncMock()
    mock_torrent_streams.find_by_imdb.return_value = ([t_stream], [])

    mock_tvdb_resolver = AsyncMock(spec=TvdbResolver)
    mock_tvdb_resolver.get_alternate_imdb_ids.return_value = []

    service = StremioService(
        torrent_streams_service=mock_torrent_streams,
        settings_service=mock_settings,
        tvdb_resolver_service=mock_tvdb_resolver,
    )
    user = UserModel(id="cache-test-user", username="test")
    parsed_id = ImdbStreamId(imdb_id="tt8888888", series_info=None)

    # Párhuzamos /stream és /subtitles kérés + utólagos /subtitles lejátszási kérés
    streams_res, subs_res = await asyncio.gather(
        service.get_streams(user, parsed_id),
        service.get_subtitles(user, parsed_id),
    )
    subs_after_play = await service.get_subtitles(
        user, parsed_id, extra="videoSize=1000000"
    )

    assert len(streams_res) == 1
    assert len(subs_res) == 1
    assert len(subs_after_play) == 1
    # Az nCore / find_by_imdb keresés mindössze 1-szer futhatott le a 3 kérés ellenére!
    assert mock_torrent_streams.find_by_imdb.await_count == 1

