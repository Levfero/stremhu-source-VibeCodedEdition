from unittest.mock import MagicMock

from app.modules.indexer_accounts.models import IndexerAccountModel
from app.modules.indexer_definitions.models import IndexerDefinitionModel
from app.modules.media_attributes.models import MediaAttributeModel
from app.modules.relay.entities import File
from app.modules.stremio.schemas import StremioStream
from app.modules.torrent_streams.schemas import TorrentStream
from app.modules.torrent_streams.service import TorrentStreamsService


def _create_mock_indexer(indexer_id: str, indexer_name: str) -> IndexerAccountModel:
    definition = IndexerDefinitionModel(
        id=indexer_id,
        name=indexer_name,
        url=f"https://{indexer_id}.invalid",
        details_path="/details",
    )
    account = IndexerAccountModel(
        indexer_id=indexer_id,
        username="teszt",
        password="teszt",
    )
    account.indexer_definition = definition
    return account


def test_deduplicate_torrent_streams_by_info_hash():
    ncore_acc = _create_mock_indexer("ncore", "nCore")
    huntorrent_acc = _create_mock_indexer("huntorrent", "HunTorrent")

    stream1 = TorrentStream(
        indexer_account=ncore_acc,
        torrent_id="t1",
        info_hash="aabbcc112233",
        seeders=45,
        torrent_name="Inception.2010.1080p.BluRay.x264.HUN-TRiNiTY",
        attributes=[],
        file_name="inception.mkv",
        file_size=10_000_000_000,
        file_index=0,
        play_url="http://localhost:7070/play/1",
        is_persisted_torrent=False,
    )

    stream2 = TorrentStream(
        indexer_account=huntorrent_acc,
        torrent_id="t2",
        info_hash="aabbcc112233",  # ugyanaz a hash
        seeders=30,
        torrent_name="Inception.2010.1080p.BluRay.x264.HUN-TRiNiTY",
        attributes=[],
        file_name="inception.mkv",
        file_size=10_000_000_000,
        file_index=0,
        play_url="http://localhost:7070/play/2",
        is_persisted_torrent=True,  # HunTorrenten letöltve/cache-elve van
    )

    service = TorrentStreamsService(
        db=MagicMock(),
        torrent_source_provider_service=MagicMock(),
        torrents_service=MagicMock(),
        settings_service=MagicMock(),
        preferences_service=MagicMock(),
    )

    deduped = service._deduplicate_torrent_streams([stream1, stream2])

    assert len(deduped) == 1
    result = deduped[0]
    assert result.info_hash == "aabbcc112233"
    assert "nCore" in result.extra_indexer_names
    assert "HunTorrent" in result.extra_indexer_names
    assert result.combined_seeders == 75  # 45 + 30
    assert result.is_persisted_torrent is True


def test_deduplicate_torrent_streams_by_name_and_size_fallback():
    ncore_acc = _create_mock_indexer("ncore", "nCore")
    huntorrent_acc = _create_mock_indexer("huntorrent", "HunTorrent")

    stream1 = TorrentStream(
        indexer_account=ncore_acc,
        torrent_id="t1",
        info_hash="",  # Nincs info_hash
        seeders=20,
        torrent_name="Oppenheimer.2023.1080p.HUN.mkv",
        attributes=[],
        file_name="Oppenheimer.2023.1080p.HUN.mkv",
        file_size=8_589_934_592,  # 8 GB
        file_index=0,
        play_url="http://localhost:7070/play/1",
    )

    stream2 = TorrentStream(
        indexer_account=huntorrent_acc,
        torrent_id="t2",
        info_hash="",  # Nincs info_hash
        seeders=15,
        torrent_name="Oppenheimer.2023.1080p.HUN.mkv",
        attributes=[],
        file_name="Oppenheimer.2023.1080p.HUN.mkv",
        file_size=8_589_934_592,
        file_index=0,
        play_url="http://localhost:7070/play/2",
    )

    service = TorrentStreamsService(
        db=MagicMock(),
        torrent_source_provider_service=MagicMock(),
        torrents_service=MagicMock(),
        settings_service=MagicMock(),
        preferences_service=MagicMock(),
    )

    deduped = service._deduplicate_torrent_streams([stream1, stream2])

    assert len(deduped) == 1
    result = deduped[0]
    assert "nCore" in result.extra_indexer_names
    assert "HunTorrent" in result.extra_indexer_names
    assert result.combined_seeders == 35


def test_stremio_stream_formatting_and_flags():
    ncore_acc = _create_mock_indexer("ncore", "nCore")

    hun_attr = MediaAttributeModel(
        id="hun",
        preference_id="language",
        name="Magyar",
        short_name="Hun",
    )
    eng_attr = MediaAttributeModel(
        id="eng",
        preference_id="language",
        name="Angol",
        short_name="Eng",
    )
    res_attr = MediaAttributeModel(
        id="1080p",
        preference_id="resolution",
        name="1080p",
        short_name="1080p",
    )

    stream = TorrentStream(
        indexer_account=ncore_acc,
        torrent_id="t1",
        info_hash="11223344",
        seeders=50,
        torrent_name="Interstellar.2014.1080p.HUN.ENG.x264",
        attributes=[res_attr, hun_attr, eng_attr],
        file_name="Interstellar.mkv",
        file_size=12_000_000_000,
        file_index=0,
        play_url="http://localhost:7070/play/interstellar",
        is_persisted_torrent=True,
        extra_indexer_names=["nCore", "HunTorrent"],
        combined_seeders=85,
    )

    stremio_stream = StremioStream.from_imdb_torrent_stream(stream)

    # 1. Címben ott kell legyen a letöltött jelzés, de [StremHU] prefix nélkül
    assert "[StremHU]" not in stremio_stream.name
    assert "⚡ Letöltve" in stremio_stream.name
    assert "1080p" in stremio_stream.name

    # 2. Leírásban: Letöltve státusz legfelül
    assert "⚡ Helyben tárolva (Azonnali lejátszás)" in stremio_stream.description

    # 3. Zászlók a nyelveknél
    assert "🇭🇺 Hun" in stremio_stream.description
    assert "🇬🇧 Eng" in stremio_stream.description

    # 4. Összevont trackerek és seederek
    assert "🧲 nCore, HunTorrent" in stremio_stream.description
    assert "👥 85" in stremio_stream.description


def test_relay_file_boundary_priorities():
    torrent_mock = MagicMock()
    torrent_mock.piece_size = 1024 * 1024  # 1 MB pieces

    # 10 darab 1MB-os darabból álló fájl (start=0, end=9)
    file_info_mock = MagicMock()
    file_info_mock.name = "movie.mkv"
    file_info_mock.size = 10 * 1024 * 1024
    file_info_mock.offset = 0

    file = File(file_info_mock, torrent_mock)
    assert file.start_piece_index == 0
    assert file.end_piece_index == 9

    torrent_mock.torrent_handle = MagicMock()
    # Tegyük fel, hogy a 0. és 9. darab már megvan, de az 1., 2. és 7., 8. még hiányzik
    torrent_mock.torrent_handle.have_piece.side_effect = lambda p: p in (0, 9)

    target_deadlines: dict[int, int] = {}
    torrent_mock._set_file_boundary_priorities = (
        lambda f, d: Torrent._set_file_boundary_priorities(torrent_mock, f, d)
    )

    from app.modules.relay.entities import Torrent

    Torrent._set_file_boundary_priorities(torrent_mock, file, target_deadlines)

    # boundary_count = min(3, 10 // 2) = 3
    # Eleje: 0 (have_piece=True), 1 (deadline=0), 2 (deadline=0)
    # Vége: 9 (have_piece=True), 8 (deadline=0), 7 (deadline=0)
    assert 0 not in target_deadlines
    assert target_deadlines.get(1) == 0
    assert target_deadlines.get(2) == 0
    assert target_deadlines.get(7) == 0
    assert target_deadlines.get(8) == 0
    assert 9 not in target_deadlines
