import httpx

from app.common.keyed_lock import KeyedLock
from app.common.logger import logger
from app.config import config


class TvdbResolver:
    def __init__(self):
        self._cache: dict[str, str | None] = {}
        self._lock = KeyedLock()

    async def resolve_tvdb_to_imdb(self, tvdb_id: str) -> str | None:
        """
        TVDB azonosító feloldása IMDb (tt...) azonosítóra.
        Elsődleges forrásként a TVMaze ingyenes publikus API-ját használja.
        Másodlagos forrásként a TMDB API-t használja, amennyiben meg van adva TMDB API kulcs.
        """
        if not tvdb_id:
            return None

        clean_id = tvdb_id.strip()
        if clean_id in self._cache:
            return self._cache[clean_id]

        async with self._lock(clean_id):
            if clean_id in self._cache:
                return self._cache[clean_id]

            # 1. Próbálkozás: TVMaze API (ingyenes, nem igényel API kulcsot)
            imdb_id = await self._resolve_from_tvmaze(clean_id)
            if imdb_id:
                self._cache[clean_id] = imdb_id
                return imdb_id

            # 2. Próbálkozás: TMDB API (ha van beállítva TMDB API kulcs)
            if config.tmdb_api_key:
                imdb_id = await self._resolve_from_tmdb(clean_id)
                if imdb_id:
                    self._cache[clean_id] = imdb_id
                    return imdb_id

            logger.warning(
                f"[TvdbResolver] Nem sikerült IMDb azonosítót találni a TVDB ID-hoz: {clean_id}"
            )
            self._cache[clean_id] = None
            return None

    async def _resolve_from_tvmaze(self, tvdb_id: str) -> str | None:
        url = f"https://api.tvmaze.com/lookup/shows?thetvdb={tvdb_id}"
        headers = {
            "User-Agent": "StremHU-Source/1.0",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    externals = data.get("externals", {})
                    imdb = externals.get("imdb")
                    if imdb and isinstance(imdb, str) and imdb.startswith("tt"):
                        logger.info(
                            f"[TvdbResolver] TVDB {tvdb_id} sikeresen feloldva IMDb-re: {imdb} (TVMaze)"
                        )
                        return imdb
        except Exception as e:
            logger.debug(f"[TvdbResolver] TVMaze feloldási hiba ({tvdb_id}): {e}")

        return None

    async def _resolve_from_tmdb(self, tvdb_id: str) -> str | None:
        if not config.tmdb_api_key:
            return None

        find_url = (
            f"https://api.themoviedb.org/3/find/{tvdb_id}"
            f"?api_key={config.tmdb_api_key}&external_source=tvdb_id"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(find_url)
                if response.status_code == 200:
                    data = response.json()
                    tv_results = data.get("tv_results", [])
                    if tv_results:
                        tmdb_id = tv_results[0].get("id")
                        ext_url = (
                            f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids"
                            f"?api_key={config.tmdb_api_key}"
                        )
                        ext_resp = await client.get(ext_url)
                        if ext_resp.status_code == 200:
                            imdb = ext_resp.json().get("imdb_id")
                            if imdb and isinstance(imdb, str) and imdb.startswith("tt"):
                                logger.info(
                                    f"[TvdbResolver] TVDB {tvdb_id} feloldva IMDb-re: {imdb} (TMDB)"
                                )
                                return imdb

                    movie_results = data.get("movie_results", [])
                    if movie_results:
                        tmdb_id = movie_results[0].get("id")
                        ext_url = (
                            f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids"
                            f"?api_key={config.tmdb_api_key}"
                        )
                        ext_resp = await client.get(ext_url)
                        if ext_resp.status_code == 200:
                            imdb = ext_resp.json().get("imdb_id")
                            if imdb and isinstance(imdb, str) and imdb.startswith("tt"):
                                logger.info(
                                    f"[TvdbResolver] TVDB {tvdb_id} feloldva IMDb-re: {imdb} (TMDB)"
                                )
                                return imdb
        except Exception as e:
            logger.debug(f"[TvdbResolver] TMDB feloldási hiba ({tvdb_id}): {e}")

        return None

    async def resolve_kitsu_to_imdb(
        self, kitsu_id: str, episode: int | None = None
    ) -> tuple[str | None, int | None, int | None, list[int], list[int]]:
        """
        Kitsu azonosító feloldása IMDb azonosítóra, valamint a megfelelő évad és epizód számra.
        1. Elsődlegesen az Anime Kitsu meta szolgáltatást használja (strem.fun), ami a Stremio hivatalos
           kitsu adatbázisa és közvetlenül tartalmazza az IMDb ID-t és epizód mappinget.
        2. Másodlagosan a hivatalos Kitsu API + TVMaze API segítségével oldja fel a címet IMDb-re.
        """
        if not kitsu_id:
            return None, None, None, [], []

        clean_id = kitsu_id.strip()
        cache_key = f"kitsu_{clean_id}_{episode}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if isinstance(cached, tuple):
                return cached

        async with self._lock(cache_key):
            if cache_key in self._cache:
                cached = self._cache[cache_key]
                if isinstance(cached, tuple):
                    return cached

            # 1. Próbálkozás: Anime Kitsu (strem.fun) - pontos IMDb ID és évad/epizód leképzés
            res = await self._resolve_from_anime_kitsu(clean_id, episode)
            if res[0]:
                self._cache[cache_key] = res
                return res

            # 2. Próbálkozás: Kitsu API -> TVMaze
            res2 = await self._resolve_from_kitsu_tvmaze(clean_id, episode)
            if res2[0]:
                self._cache[cache_key] = res2
                return res2

            logger.warning(
                f"[TvdbResolver] Nem sikerült IMDb azonosítót találni a Kitsu ID-hoz: {clean_id}"
            )
            empty_res = (None, None, None, [], [])
            self._cache[cache_key] = empty_res
            return empty_res

    async def _resolve_from_anime_kitsu(
        self, kitsu_id: str, episode: int | None
    ) -> tuple[str | None, int | None, int | None, list[int], list[int]]:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        for endpoint in [
            f"https://anime-kitsu.strem.fun/meta/series/kitsu:{kitsu_id}.json",
            f"https://anime-kitsu.strem.fun/meta/anime/kitsu:{kitsu_id}.json",
        ]:
            try:
                async with httpx.AsyncClient(
                    timeout=8.0, headers=headers, follow_redirects=True
                ) as client:
                    resp = await client.get(endpoint)
                    if resp.status_code == 200:
                        data = resp.json()
                        meta = data.get("meta", {})
                        imdb_id = meta.get("imdb_id")
                        if imdb_id and isinstance(imdb_id, str) and imdb_id.startswith("tt"):
                            season = 1
                            ep = episode
                            alt_eps = []
                            alt_seasons = []
                            if episode is not None:
                                videos = meta.get("videos", [])
                                for v in videos:
                                    if v.get("episode") == episode or str(v.get("id", "")).endswith(f":{episode}"):
                                        season = v.get("imdbSeason") or v.get("season") or 1
                                        ep = v.get("imdbEpisode") or v.get("episode") or episode
                                        abs_ep = v.get("absoluteEpisode")
                                        if abs_ep and abs_ep != ep and abs_ep not in alt_eps:
                                            alt_eps.append(abs_ep)
                                        if episode and episode != ep and episode not in alt_eps:
                                            alt_eps.append(episode)
                                        v_s = v.get("season") or 1
                                        if v_s != season and v_s not in alt_seasons:
                                            if not (v_s == 1 and abs_ep and abs_ep > 20 and ep != episode):
                                                alt_seasons.append(v_s)
                                        break
                            logger.info(
                                f"[TvdbResolver] Kitsu {kitsu_id} feloldva: {imdb_id} S{season}E{ep} alt_eps={alt_eps} (Anime-Kitsu)"
                            )
                            return imdb_id, season, ep, alt_eps, alt_seasons
            except Exception as e:
                logger.debug(f"[TvdbResolver] Anime-Kitsu hiba ({kitsu_id}): {e}")

        return None, None, None, [], []

    async def _resolve_from_kitsu_tvmaze(
        self, kitsu_id: str, episode: int | None
    ) -> tuple[str | None, int | None, int | None, list[int], list[int]]:
        url = f"https://kitsu.io/api/edge/anime/{kitsu_id}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.api+json"}
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=headers, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    attr = data.get("data", {}).get("attributes", {})
                    candidates = [
                        attr.get("canonicalTitle"),
                        attr.get("titles", {}).get("en"),
                        attr.get("titles", {}).get("en_jp"),
                    ]
                    for c in list(candidates):
                        if c and ":" in c:
                            candidates.append(c.split(":")[0].strip())

                    for title in candidates:
                        if not title:
                            continue
                        tvmaze_url = f"https://api.tvmaze.com/singlesearch/shows?q={title}"
                        try:
                            tvmaze_resp = await client.get(tvmaze_url)
                            if tvmaze_resp.status_code == 200:
                                tvmaze_data = tvmaze_resp.json()
                                imdb = tvmaze_data.get("externals", {}).get("imdb")
                                if imdb and isinstance(imdb, str) and imdb.startswith("tt"):
                                    logger.info(
                                        f"[TvdbResolver] Kitsu {kitsu_id} ({title}) feloldva IMDb-re: {imdb} (TVMaze)"
                                    )
                                    return imdb, 1, episode, [], []
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(f"[TvdbResolver] Kitsu TVMaze feloldási hiba ({kitsu_id}): {e}")

        return None, None, None, [], []

    async def get_title_by_imdb(self, imdb_id: str) -> str | None:
        """Lekéri a média címét IMDb ID alapján Cinemeta API-n keresztül (másodlagos kereséshez)."""
        if not imdb_id or not imdb_id.startswith("tt"):
            return None
        cache_key = f"title_{imdb_id}"
        if cache_key in self._cache:
            val = self._cache[cache_key]
            return val if isinstance(val, str) else None

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        for c_type in ["series", "movie"]:
            url = f"https://v3-cinemeta.strem.io/meta/{c_type}/{imdb_id}.json"
            try:
                async with httpx.AsyncClient(
                    timeout=5.0, headers=headers, follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        name = data.get("meta", {}).get("name")
                        if name:
                            self._cache[cache_key] = name
                            return name
            except Exception:
                pass
        return None

    async def get_episode_mappings(
        self,
        imdb_id: str,
        season: int,
        episode: int,
    ) -> tuple[list[int], list[int]]:
        """
        Dinamikusan lekéri egy adott (imdb_id, season, episode) alternatív epizódszámait
        és évadait Anime-Kitsu és Cinemeta metaadatokból.
        Nincs szükség hardkódolt sorozat- vagy animelista fenntartására.
        """
        if not imdb_id or not imdb_id.startswith("tt"):
            return [], []

        cache_key = f"epmap_{imdb_id}_{season}_{episode}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if isinstance(cached, tuple):
                return cached

        alt_eps: set[int] = set()
        alt_seasons: set[int] = set()

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

        # 1. Próbálkozás: Anime Kitsu API az IMDb ID alapján
        kitsu_url = f"https://anime-kitsu.strem.fun/meta/series/{imdb_id}.json"
        try:
            async with httpx.AsyncClient(timeout=6.0, headers=headers, follow_redirects=True) as client:
                resp = await client.get(kitsu_url)
                if resp.status_code == 200:
                    data = resp.json()
                    videos = data.get("meta", {}).get("videos", [])
                    for v in videos:
                        v_s = v.get("imdbSeason") or v.get("season")
                        v_e = v.get("imdbEpisode") or v.get("episode")
                        v_abs = v.get("absoluteEpisode") or v.get("kitsuEpisode")

                        if v_s == season and v_e == episode:
                            if v_abs and v_abs != episode:
                                alt_eps.add(v_abs)
                            kitsu_e = v.get("kitsuEpisode")
                            if kitsu_e and kitsu_e != episode:
                                alt_eps.add(kitsu_e)

                        if season == 1 and episode > 20 and v_abs == episode:
                            if v_s and v_s != 1:
                                alt_seasons.add(v_s)
                            if v_e and v_e != episode:
                                alt_eps.add(v_e)
        except Exception as e:
            logger.debug(f"[TvdbResolver] Anime Kitsu epmap lekérési hiba ({imdb_id}): {e}")

        # 2. Próbálkozás: Cinemeta kumulatív számítás, ha még nincs alternatív epizód
        if not alt_eps:
            cinemeta_url = f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
            try:
                async with httpx.AsyncClient(timeout=6.0, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(cinemeta_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        vids = [
                            v for v in data.get("meta", {}).get("videos", [])
                            if v.get("season", 0) > 0
                        ]
                        from collections import defaultdict
                        s_counts = defaultdict(int)
                        for v in vids:
                            s_counts[v.get("season")] += 1

                        if len(s_counts) > 1:
                            if season in s_counts:
                                cum = sum(s_counts[s] for s in range(1, season))
                                calc_abs = cum + episode
                                if calc_abs != episode:
                                    alt_eps.add(calc_abs)
                            elif season == 1 and episode > 20:
                                cum = 0
                                for s_num in sorted(s_counts.keys()):
                                    if cum < episode <= cum + s_counts[s_num]:
                                        alt_seasons.add(s_num)
                                        alt_eps.add(episode - cum)
                                        break
                                    cum += s_counts[s_num]
            except Exception as e:
                logger.debug(f"[TvdbResolver] Cinemeta epmap lekérési hiba ({imdb_id}): {e}")

        result = (sorted(list(alt_eps)), sorted(list(alt_seasons)))
        self._cache[cache_key] = result
        return result


tvdb_resolver = TvdbResolver()
