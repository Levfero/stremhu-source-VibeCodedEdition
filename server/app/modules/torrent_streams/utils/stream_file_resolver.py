from __future__ import annotations

from dataclasses import dataclass, field

from app.common.schemas.internal import SeriesInfo
from app.common.torrent_info import TorrentFileInfo, TorrentInfo
from app.modules.torrent_streams.utils.resolver_helpers import is_sample_or_trash
from app.modules.torrent_streams.utils.series_parser import SeriesParser


@dataclass
class FileNode:
    name: str
    file_info: TorrentFileInfo


@dataclass
class FolderNode:
    name: str
    children: dict[str, FileNode | FolderNode] = field(default_factory=dict)


class StreamFileResolver:
    def __init__(
        self,
        torrent_info: TorrentInfo,
        series: SeriesInfo | None = None,
        imdb_id: str | None = None,
    ):
        self.torrent_info = torrent_info
        self.series = series
        self.imdb_id = imdb_id

        self.valid_files = [
            file for file in torrent_info.files if not is_sample_or_trash(file.name)
        ]

        self.target_seasons: set[int] = set()
        self.target_episodes: set[int] = set()
        self.alt_episodes_set: set[int] = set()
        if self.series:
            self.target_seasons.add(self.series.season)
            if self.series.alternate_seasons:
                self.target_seasons.update(self.series.alternate_seasons)
            if self.series.episode is not None:
                self.target_episodes.add(self.series.episode)
            if self.series.alternate_episodes:
                self.target_episodes.update(self.series.alternate_episodes)
                self.alt_episodes_set.update(self.series.alternate_episodes)

    @staticmethod
    def _get_largest_video_file(
        valid_files: list[TorrentFileInfo],
    ) -> TorrentFileInfo:
        return max(valid_files, key=lambda f: f.size)

    def _build_tree(self) -> FolderNode:
        root = FolderNode(
            name=self.torrent_info.name,
        )

        for file in self.valid_files:
            parts = file.path.split("/")
            current_node = root

            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    current_node.children[part] = FileNode(
                        name=part,
                        file_info=file,
                    )
                else:
                    if part not in current_node.children:
                        current_node.children[part] = FolderNode(name=part)

                    next_node = current_node.children[part]

                    if isinstance(next_node, FolderNode):
                        current_node = next_node
        return root

    def _get_all_files_in_folder(self, folder: FolderNode) -> list[TorrentFileInfo]:
        files: list[TorrentFileInfo] = []
        for child in folder.children.values():
            if isinstance(child, FileNode):
                files.append(child.file_info)
            elif isinstance(child, FolderNode):
                files.extend(self._get_all_files_in_folder(child))
        return files

    def _get_target_seasons(self) -> set[int]:
        return self.target_seasons

    def _get_target_episodes(self) -> set[int]:
        return self.target_episodes

    def _traverse_tree(
        self,
        node: FolderNode | FileNode,
        inherited_season: int | None,
    ) -> list[TorrentFileInfo]:
        if self.series is None:
            return []

        if isinstance(node, FileNode):
            parser = SeriesParser(node.name)
            seasons, episodes = parser.parse(
                context_seasons=[inherited_season] if inherited_season else None
            )

            file_season = inherited_season

            if seasons and len(seasons) == 1:
                file_season = seasons[0]

            if self._is_episode_match(
                file_info=node.file_info,
                file_season=file_season,
                seasons=seasons,
                episodes=episodes,
            ):
                return [node.file_info]
            return []

        elif isinstance(node, FolderNode):
            parser = SeriesParser(node.name)
            seasons, episodes = parser.parse(allow_standalone_episode=False)

            folder_season = inherited_season

            if seasons and len(seasons) == 1:
                folder_season = seasons[0]

            if folder_season is not None and folder_season not in self.target_seasons:
                # Ha vannak alternatív epizódok (pl. anime/hosszú sorozatok abszolút számozása),
                # nem vetjük el a mappát, mert a benne lévő fájlok megfelelhetnek az abszolút epizódszámnak!
                if not self.alt_episodes_set:
                    return []

            # Először megnézzük, hogy az almappákban/fájlokban van-e közvetlen találat
            matches: list[TorrentFileInfo] = []
            for child in node.children.values():
                matches.extend(
                    self._traverse_tree(
                        child,
                        folder_season,
                    )
                )

            if matches:
                return matches

            # Csak ha a gyermekelemek nem adtak konkrét találatot, akkor vizsgáljuk,
            # hogy maga a mappa képviseli-e a keresett epizódot (pl. S01E01 nevű mappa egy videóval)
            folder_season_ok = (
                folder_season in self.target_seasons
                if folder_season is not None
                else (self.series.season == 1 or 1 in self.target_seasons)
            )
            if (
                folder_season_ok
                and episodes
                and len(episodes) == 1
                and (
                    episodes[0] in self.alt_episodes_set
                    or (episodes[0] == self.series.episode and folder_season_ok)
                )
            ):
                return self._get_all_files_in_folder(node)

            return []

    def _is_episode_match(
        self,
        file_info: TorrentFileInfo,
        file_season: int | None,
        seasons: list[int],
        episodes: list[int],
    ) -> bool:
        if not self.series or not episodes:
            return False

        # 1. Abszolút epizódszámozás egyezése (pl. anime: Ep 39)
        # Ez évadtól függetlenül egyezik, mivel az anime abszolút epizódszámok globálisan egyediek.
        if self.alt_episodes_set and any(
            alt in self.alt_episodes_set for alt in episodes
        ):
            return True

        # 2. Relatív epizód közvetlen egyezése (pl. E02)
        if self.series.episode in episodes:
            if file_season is not None:
                if file_season in self.target_seasons:
                    return True
            elif seasons:
                if any(s in self.target_seasons for s in seasons):
                    return True
            else:
                # Nincs megadva évad sehol (pl. "Anime - 02.mkv") -> csak akkor egyezhet,
                # ha az 1. évadot keressük!
                if self.series.season == 1 or 1 in self.target_seasons:
                    return True

        # 3. Alternatív évad és epizód párok egyezése (pl. One Piece S21E01 amikor S1E892 a kérés)
        if self.series.alternate_seasons and self.series.alternate_episodes:
            alt_s_set = set(self.series.alternate_seasons)
            alt_e_set = set(self.series.alternate_episodes)
            if any(e in alt_e_set for e in episodes):
                if file_season is not None:
                    if file_season in alt_s_set:
                        return True
                elif seasons:
                    if any(s in alt_s_set for s in seasons):
                        return True
                else:
                    if 1 in alt_s_set:
                        return True

        return False

    def resolve(self) -> TorrentFileInfo | None:
        if not self.valid_files:
            return None

        # Filmek esetén: csak visszaadjuk a legnagyobbat
        if not self.series:
            return self._get_largest_video_file(self.valid_files)

        # Torrent root feldolgozása (gyökér kontextus) egyszer
        torrent_seasons, torrent_episodes = SeriesParser(self.torrent_info.name).parse(
            allow_standalone_episode=False
        )

        if torrent_seasons:
            season_allowed = any(s in self.target_seasons for s in torrent_seasons)
            # Ha a torrent évada nem egyezik a célévaddal, de a torrent neve konkrétan egy abszolút
            # epizódot jelöl, vagy egy teljes pakk (nincs konkrét epizód a torrent nevében), engedélyezzük.
            # DE ha a torrent címében explicit más évad és más epizód van (pl. S01E02 mikor S03E02-t keresünk),
            # akkor nem engedélyezzük!
            if not season_allowed and self.alt_episodes_set:
                if torrent_episodes and any(
                    ep in self.alt_episodes_set for ep in torrent_episodes
                ):
                    season_allowed = True
                elif not torrent_episodes:
                    season_allowed = True
            if not season_allowed:
                return None

        # Early optimization: Ha a torrent címe maga egyetlen konkrét epizódot jelöl és egyetlen fájl
        if (
            len(self.valid_files) == 1
            and torrent_episodes
            and len(torrent_episodes) == 1
        ):
            ep = torrent_episodes[0]
            is_absolute_match = ep in self.alt_episodes_set
            season_ok = (
                any(s in self.target_seasons for s in torrent_seasons)
                if torrent_seasons
                else (self.series.season == 1 or 1 in self.target_seasons)
            )
            is_relative_match = ep == self.series.episode and season_ok

            is_alt_pair_match = False
            if self.series.alternate_seasons and self.series.alternate_episodes:
                alt_s_set = set(self.series.alternate_seasons)
                alt_e_set = set(self.series.alternate_episodes)
                alt_season_ok = (
                    any(s in alt_s_set for s in torrent_seasons)
                    if torrent_seasons
                    else (1 in alt_s_set)
                )
                if ep in alt_e_set and alt_season_ok:
                    is_alt_pair_match = True

            if is_absolute_match or is_relative_match or is_alt_pair_match:
                return self._get_largest_video_file(self.valid_files)
            else:
                return None

        # Fa felépítése
        root_node = self._build_tree()

        inherited_season = torrent_seasons[0] if len(torrent_seasons) == 1 else None

        exact_matches: list[TorrentFileInfo] = []

        for child in root_node.children.values():
            exact_matches.extend(
                self._traverse_tree(
                    child,
                    inherited_season,
                )
            )

        if exact_matches:
            return self._get_largest_video_file(exact_matches)

        return None
