from app.common.schemas.internal import SeriesInfo
from app.common.torrent_info import TorrentFileInfo, TorrentInfo
from app.modules.torrent_streams.utils.resolver_helpers import (
    is_extra_file,
    is_extra_folder,
)
from app.modules.torrent_streams.utils.stream_file_resolver import StreamFileResolver


def make_file(
    path: str, index: int, size: int = 1000, is_video: bool = True
) -> TorrentFileInfo:
    name = path.split("/")[-1]
    return TorrentFileInfo(
        name=name,
        path=path,
        size=size,
        offset=index * 1000,
        start_piece_index=index,
        end_piece_index=index,
        is_video=is_video,
        index=index,
    )


def test_extra_folder_and_file_helpers():
    assert is_extra_folder("Extras") is True
    assert is_extra_folder("extras") is True
    assert is_extra_folder("[Extras]") is True
    assert is_extra_folder("00. Extrák") is True
    assert is_extra_folder("00 - Extrák") is True
    assert is_extra_folder("BD Menü") is True
    assert is_extra_folder("DVD Menu") is True
    assert is_extra_folder("Borítók") is True
    assert is_extra_folder("Specials") is True
    assert is_extra_folder("Különkiadások") is True
    assert is_extra_folder("Season 2 Extras") is True
    assert is_extra_folder("S02 Extras") is True
    assert is_extra_folder("SP") is True
    assert is_extra_folder("NCED") is True
    assert is_extra_folder("NCOP") is True
    assert is_extra_folder("OAD") is True
    assert is_extra_folder("OVA") is True
    assert is_extra_folder("Bonus") is True
    assert is_extra_folder("Bónuszok") is True
    assert is_extra_folder("Featurettes") is True
    assert is_extra_folder("Season 1") is False
    assert is_extra_folder("Season 2") is False
    assert is_extra_folder("01. Noragami") is False
    assert is_extra_folder("02. Noragami Aragoto") is False
    assert is_extra_folder("Noragami Aragoto") is False
    assert is_extra_folder("Special A") is False
    assert is_extra_folder("Special Ops") is False

    assert is_extra_file("Noragami Aragoto - Extra 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - SP 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - SP01.mkv") is True
    assert is_extra_file("Noragami Aragoto - OAD 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - OVA 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - NCED 01.mkv") is True
    assert is_extra_file("Noragami Aragoto - NCED.mkv") is True
    assert is_extra_file("Noragami Aragoto - NCOP.mkv") is True
    assert is_extra_file("Noragami Aragoto Menu.mkv") is True
    assert is_extra_file("Noragami Aragoto Menü.mkv") is True
    assert is_extra_file("Noragami Aragoto - 01.mkv") is False
    assert is_extra_file("Special A - 01.mkv") is False


def test_noragami_extras_folder_excluded_for_season_2():
    t_noragami = TorrentInfo(
        info_hash="hash_noragami",
        name="[AnimeRG] Noragami Aragoto S02 [1080p]",
        size=8000,
        piece_size=1000,
        files=[
            make_file(
                "Noragami Aragoto/Extras/Noragami Aragoto Menu.mkv", 0, size=2000
            ),
            make_file("Noragami Aragoto/Extras/01.mkv", 1, size=2500),
            make_file(
                "Noragami Aragoto/Extras/Noragami Aragoto OAD 01.mkv", 2, size=2200
            ),
            make_file(
                "Noragami Aragoto/Noragami Aragoto - 01 [1080p].mkv", 3, size=1500
            ),
            make_file(
                "Noragami Aragoto/Noragami Aragoto - 02 [1080p].mkv", 4, size=1500
            ),
        ],
    )

    resolver_s2e1 = StreamFileResolver(
        t_noragami, series=SeriesInfo(season=2, episode=1)
    )
    resolved_file = resolver_s2e1.resolve()

    assert resolved_file is not None
    assert resolved_file.name == "Noragami Aragoto - 01 [1080p].mkv"

    resolver_s0e1 = StreamFileResolver(
        t_noragami, series=SeriesInfo(season=0, episode=1)
    )
    resolved_special = resolver_s0e1.resolve()
    assert resolved_special is not None
    assert resolved_special.name in (
        "Noragami Aragoto OAD 01.mkv",
        "01.mkv",
    )


def test_noragami_bdbox_real_structure():
    files = [
        make_file("Noragami S01-S02 BDBOX/00. Extrák/BD Menü/S02/Vol 2.mkv", 0, size=2000),
        make_file("Noragami S01-S02 BDBOX/01. Noragami/[ReinForce] Noragami - 01 (BDRip).mkv", 1, size=1500),
        make_file("Noragami S01-S02 BDBOX/01. Noragami/[ReinForce] Noragami OAD1 (DVDRip).mkv", 2, size=1200),
        make_file("Noragami S01-S02 BDBOX/02. Noragami Aragoto/[ReinForce] Noragami Aragoto - 01 (BDRip).mkv", 3, size=1600),
        make_file("Noragami S01-S02 BDBOX/02. Noragami Aragoto/[ReinForce] Noragami Aragoto - 02 (BDRip).mkv", 4, size=1600),
        make_file("Noragami S01-S02 BDBOX/02. Noragami Aragoto/[ReinForce] Noragami Aragoto - 13 (BDRip).mkv", 5, size=1700),
    ]

    t_box = TorrentInfo(
        info_hash="hash_noragami_box",
        name="Noragami S01-S02 BDBOX (BD_1920x1080)",
        size=10000,
        piece_size=1000,
        files=files,
    )

    # Season 2 Episode 1 (alt_episodes=[13])
    r_s2e1 = StreamFileResolver(t_box, series=SeriesInfo(season=2, episode=1, alternate_episodes=[13]))
    v_s2e1 = r_s2e1.resolve()
    assert v_s2e1 is not None
    assert v_s2e1.name == "[ReinForce] Noragami Aragoto - 01 (BDRip).mkv"

    # Season 2 Episode 2 (alt_episodes=[14])
    r_s2e2 = StreamFileResolver(t_box, series=SeriesInfo(season=2, episode=2, alternate_episodes=[14]))
    v_s2e2 = r_s2e2.resolve()
    assert v_s2e2 is not None
    assert v_s2e2.name == "[ReinForce] Noragami Aragoto - 02 (BDRip).mkv"

    # Season 1 Episode 1
    r_s1e1 = StreamFileResolver(t_box, series=SeriesInfo(season=1, episode=1))
    v_s1e1 = r_s1e1.resolve()
    assert v_s1e1 is not None
    assert v_s1e1.name == "[ReinForce] Noragami - 01 (BDRip).mkv"

    # Specials
    r_s0e1 = StreamFileResolver(t_box, series=SeriesInfo(season=0, episode=1))
    v_s0e1 = r_s0e1.resolve()
    assert v_s0e1 is not None
    assert v_s0e1.name == "[ReinForce] Noragami OAD1 (DVDRip).mkv"
