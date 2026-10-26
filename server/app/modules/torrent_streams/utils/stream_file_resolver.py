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
        if not self.series:
            return set()
        seasons = {self.series.season}
        if self.series.alternate_seasons:
            seasons.update(self.series.alternate_seasons)
        return seasons

    def _get_target_episodes(self) -> set[int]:
        if not self.series:
            return set()
        episodes = set()
        if self.series.episode is not None:
            episodes.add(self.series.episode)
        if self.series.alternate_episodes:
            episodes.update(self.series.alternate_episodes)
        return episodes

    def _traverse_tree(
        self,
        node: FolderNode | FileNode,
        inherited_season: int | None,
    ) -> list[TorrentFileInfo]:
        if self.series is None:
            return []

        target_seasons = self._get_target_seasons()
        target_episodes = self._get_target_episodes()

        if isinstance(node, FileNode):
            parser = SeriesParser(node.name)
            seasons, episodes = parser.parse(
                context_seasons=[inherited_season] if inherited_season else None
            )

            file_season = inherited_season

            if seasons and len(seasons) == 1:
                file_season = seasons[0]

            effective_file_season = file_season if file_season is not None else 1

            if self._is_episode_match(
                file_info=node.file_info,
                effective_season=effective_file_season,
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

            if folder_season is not None and folder_season not in target_seasons:
                # Ha vannak alternatív epizódok (pl. anime/hosszú sorozatok abszolút számozása),
                # nem vetjük el a mappát, mert a benne lévő fájlok megfelelhetnek az abszolút epizódszámnak!
                if not self.series.alternate_episodes:
                    return []

            effective_folder_season = folder_season if folder_season is not None else 1

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
            if (
                effective_folder_season in target_seasons
                and episodes
                and len(episodes) == 1
                and any(e in target_episodes for e in episodes)
            ):
                return self._get_all_files_in_folder(node)

            return []

    def _is_episode_match(
        self,
        file_info: TorrentFileInfo,
        effective_season: int,
        seasons: list[int],
        episodes: list[int],
    ) -> bool:
        if not self.series or not episodes:
            return False

        target_seasons = self._get_target_seasons()
        target_episodes = self._get_target_episodes()

        # 1. Évad és epizód közvetlen egyezése (figyelembe véve az alternatív évadokat és epizódokat is)
        season_matches = (
            effective_season in target_seasons
            or any(s in seasons for s in target_seasons)
        )
        if season_matches and any(e in target_episodes for e in episodes):
            return True

        # 2. Ha a fájl abszolút epizódszámozású (seasons üres vagy a szám közvetlenül szerepel az alternatív epizódok között)
        if self.series.alternate_episodes and any(
            alt in episodes for alt in self.series.alternate_episodes
        ):
            return True

        # 3. Ha az 1. évadot keressük, és a fájlnévben nem volt explicit évad, és közvetlenül egyezik
        if (
            not seasons
            and effective_season == 1
            and self.series.season == 1
            and self.series.episode in episodes
        ):
            return True

        return False

    def resolve(self) -> TorrentFileInfo | None:
        if not self.valid_files:
            return None

        # Filmek esetén: csak visszaadjuk a legnagyobbat
        if not self.series:
            return self._get_largest_video_file(self.valid_files)

        target_seasons = self._get_target_seasons()
        target_episodes = self._get_target_episodes()

        # Torrent root feldolgozása (gyökér kontextus) egyszer
        torrent_seasons, torrent_episodes = SeriesParser(self.torrent_info.name).parse()

        if torrent_seasons:
            season_allowed = any(s in torrent_seasons for s in target_seasons)
            # Ha vannak alternatív epizódok (pl. anime/hosszú sorozatok abszolút számozása),
            # nem szűrünk évadra a torrent nevénél, mert a torrent pakk tartalmazhatja a keresett részt!
            if not season_allowed and self.series.alternate_episodes:
                season_allowed = True
            if not season_allowed:
                return None

        # Early optimization: Ha a torrent címe maga egyetlen konkrét epizódot jelöl
        if torrent_episodes and len(torrent_episodes) == 1:
            ep = torrent_episodes[0]
            if ep in target_episodes:
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
