# Fejlesztői Dokumentáció: `feature/search-filter-tmdb-tvdb`

> **Viszonyítási alap (Baseline):** Eredeti upstream [`s4pp1/stremhu-source`](https://github.com/s4pp1/stremhu-source) (`8c99c7d` commit).
> Ez a dokumentum a `feature/search-filter-tmdb-tvdb` branch által bevezetett összes funkcionális, hibajavítási, Stremio/Nuvio best practice és teljesítménybeli változtatást részletesen rögzíti az eredeti `s4pp1/stremhu-source` állapothoz képest (beleértve a `main` ágból átvett anime feloldást, a több-trackeres keresési optimalizálásokat és a stream deduplikációt is).

---

## 1. A Branch Célja és Motivációja

Az eredeti `s4pp1/stremhu-source` a következő korlátokkal rendelkezett a TMDB alapú Stremio katalógusok, a több-trackeres szöveges keresés, a másodlagos epizód-feloldás és a stream megjelenítés terén:

1. **TMDB Stream ID (`tmdb:{id}`) hiánya és API kulcs kényszer:** A Stremio TheMovieDatabase (TMDB) addonja `tmdb:693134` (film) vagy `tmdb:1399:1:1` (sorozat) formátumú azonosítókat küld. Az eredeti rendszer nem ismerte fel a `tmdb:` prefixet, és még kezdetleges feloldás esetén is kötelezővé tette a felhasználói TMDB API kulcsot, a publikus TMDB oldal lekérését pedig a Cloudflare 403-as hibával tiltotta.
2. **Keresési hiányosságok a nem-nCore indexereken:** Az `s4pp1/stremhu-source` implementációjában kizárólag az nCore rendelkezett `_fetch_torrents_by_query` metódussal. A többi indexeren (`HunTorrent`, `Majomparádé`, `BitHUmen`, `iNSANE`) a szöveges keresések mindig üres listát adtak vissza, továbbá a `BitHUmen` és `iNSANE` indexerek eldobták azokat a torrenteket, ahol a feltöltő nem adott meg IMDb linket.
3. **Torrent letöltési lavina (Download Storm) általános kereséseknél:** Ha egy másodlagos név szerinti keresés (fallback title search) vagy katalógus-keresés sok találatot adott (pl. *South Park*, *Naruto*, *Star Wars*), a rendszer korlátlanul letöltötte mind a 300 `.torrent` fájlt, ami 10–15 másodperces lefagyást és tracker timeoutokat okozott.
4. **Ismétlődő tracker lekérdezések epizódváltáskor:** Epizódváltáskor vagy másodlagos keresési próbálkozásoknál a trackerek újra és újra le lettek kérdezve a hálózaton keresztül a rövid 180 mp-es gyorsítótár miatt.
5. **Film és sorozat téves összekeveredése másodlagos keresésnél:** Ha egy film keresésekor (pl. *Dune*) a név szerinti keresés sorozat-epizódokat vagy évadpakkokat talált, a `StreamFileResolver` a filmhez is kiválasztotta a legnagyobb videófájlt a sorozatcsomagból (pl. S03E12), filmként tálalva azt.
6. **Stremio Series Catalog és Meta hiba:** A `StremioCatalogsService` és a `router.py` korábban kizárólag `media_type == MediaType.MOVIE` esetén engedélyezte a keresési katalógust és a meta részletek lekérését; `series` esetén üres választ adott (`meta: {}`).
7. **Több-trackeres duplikációk a találatok között:** Amikor a felhasználó mind az nCore-t, mind a HunTorrentet és Majomparádét bekötötte, a megegyező kiadások (azonos swarm / info_hash vagy azonos videófájl) külön-külön, 2-3 példányban jelentek meg a kliensben, feleslegesen duplázva a találati listát.
8. **Hiányzó letöltött jelzés és felirat metaadatok:** Ha egy torrent már le volt töltve a háttértárra, a Stremio kliensben nem volt megkülönböztetve az éppen indított streaming torrenttől; a torrentekben megtalálható magyar feliratok (`.srt`, `.ass` vagy `hunsub`) nem voltak jelölve a találati leírásban; hiányoztak a nemzeti zászló emojik a nyelveknél.
9. **Kezdeti lejátszási akadozás Nuvio és ExoPlayer alatt:** A Relay modul korábban csak a streamelt fájl legelső és legutolsó darabjára állított azonnali (`deadline = 0`) prioritást. MKV Cues és MP4 moov atomok esetén (amelyek 2-6 MB méretűek is lehetnek) ez lejátszási timeoutot vagy pufferelési stall-t okozhatott az indításkor.

---

## 2. Módosított és Új Fájlok Áttekintése (Diff Mátrix `s4pp1/stremhu-source` vs `feature/search-filter-tmdb-tvdb`)

| Fájl elérési útja | Státusz | Változtatás összefoglalása |
| :--- | :---: | :--- |
| [`server/app/modules/stremio/tvdb_resolver.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/tvdb_resolver.py) | **ÚJ** | `TvdbResolver`: Kulcs nélküli TMDB feloldás Wikidata SPARQL API-val (`P4947` movie / `P4983` series), kulcs nélküli TVDB feloldás (`P4835` series / `P12196` movie), Kitsu Title Fallback Rescue (`canonicalTitle`), MyAnimeList (`mal:`) feloldás (`P4086` & Kitsu Mappings), iker IMDb aliasok (`_IMDB_ALIASES`) filmekre és sorozatokra, Cloudflare-kompatibilis web scraping fallback (`curl/8.5.0`), HTML entitás dekódolás (`html.unescape`), szigorú típus-izoláció, negatív gyorsítótár-mérgezés védelem. |
| [`server/app/modules/stremio/service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/service.py) | **MÓDOSÍTOTT** | `tmdb`, `tvdb`, `kitsu`, `mal` és `anime:` prefixek teljes körű támogatása. `prefetch_task` átadása a `find_by_imdb`-nek (duplikált keresések megszüntetése). Párhuzamos iker IMDb lekérdezés filmekre és sorozatokra is. Cím szerinti mentőöv (Title Fallback Rescue). |
| [`server/app/modules/stremio/catalogs_service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/catalogs_service.py) | **MÓDOSÍTOTT** | `MediaType.SERIES` támogatás a keresési katalógusban (`ContentType.SERIES` és `ContentType.MOVIE` elkülönítése). |
| [`server/app/modules/stremio/router.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/router.py) | **MÓDOSÍTOTT** | `/meta/{media_type}/{id}.json` kéréseknél a sorozatok engedélyezése (`media_type not in (MediaType.MOVIE, MediaType.SERIES)`). |
| [`server/app/modules/stremio/schemas.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/schemas.py) | **MÓDOSÍTOTT** | `StremioStream.from_imdb_torrent_stream`: `⚡ Letöltve` jelzés a címben és `⚡ Helyben tárolva (Azonnali lejátszás)` a leírás fejlécében; zászló emojik (`🇭🇺 Hun`, `🇬🇧 Eng`, `🇯🇵 Jap`, `🇩🇪 Ger`, `🇫🇷 Fra`); magyar felirat jelzés (`💬 🇭🇺 Hun Sub`); összevont trackerek (`🧲 nCore, HunTorrent, Majomparádé`) és összevont seederek (`👥 {sum}`). Nincs felesleges `[StremHU]` prefix. |
| [`server/app/modules/stremio/utils.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/stremio/utils.py) | **MÓDOSÍTOTT** | `parse_stream_id`: `anime:` prefix lecsupaszítása, `mal:`, `mal_` és egyéb MAL azonosító formátumok helyes felismerése `MalStreamId`-ként. |
| [`server/app/modules/torrent_streams/schemas.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torrent_streams/schemas.py) | **MÓDOSÍTOTT** | `TorrentStream`: `extra_indexer_names: list[str] = []`, `combined_seeders: int | None = None`, `has_hungarian_sub: bool = False`, `is_persisted_torrent: bool = False`. Automatikus magyar felirat detektálás a torrent fájllistájából (`.srt`, `.ass`, `.sub`, `.vtt` + magyar kulcsszavak) és nevéből (`hunsub`). |
| [`server/app/modules/torrent_streams/service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torrent_streams/service.py) | **MÓDOSÍTOTT** | `prefetch_task` deduplikáció és azonnali `await`. `_deduplicate_torrent_streams`: multi-tracker és azonos tartalmú streamek összevonása `info_hash` és `(file_size, clean_name)` alapján, seederek összeadásával és egyedi indexer nyilvántartással. `_build_fallback_queries`: intelligens cím- és évadkeresés. |
| [`server/app/modules/relay/entities.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/relay/entities.py) | **MÓDOSÍTOTT** | `_set_file_boundary_priorities`: 1-ről `min(3, total_pieces // 2)`-re bővített azonnali (`deadline = 0`) prioritás a streamelt fájl elejére és végére (MKV Cues és MP4 moov azonnali betöltése ExoPlayer / Nuvio alatt). |
| [`server/app/modules/torznab/constants.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torznab/constants.py) | **MÓDOSÍTOTT** | `SEARCH_AVAILABLE = True`, `SEARCH_SUPPORTED_PARAMS = "q,imdbid,tmdbid"`, `TV_SEARCH_SUPPORTED_PARAMS = "q,imdbid,tvdbid,tmdbid,season,ep"`, `MOVIE_SEARCH_SUPPORTED_PARAMS = "q,imdbid,tmdbid"`. |
| [`server/app/modules/torznab/query.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torznab/query.py) | **MÓDOSÍTOTT** | `TorznabQuery`: `tvdb_id` és `tmdb_id` paraméterek bevezetése és normalizált parsere. |
| [`server/app/modules/torznab/service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torznab/service.py) | **MÓDOSÍTOTT** | Torznab keresés bővítése: `tvdbid`, `tmdbid` automatikus IMDb/cím feloldása `TvdbResolver`-rel, `q` esetén szöveges keresés a forrásokon, alternatív iker IMDb azonosítók párhuzamos feloldása. |
| [`server/app/modules/indexers/service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexers/service.py) | **MÓDOSÍTOTT** | `_IMDB_TRACKER_CACHE_TTL = 1800.0` (30 perc) az egymást követő sorozatepizódok redundáns tracker lekérdezéseinek elkerülésére; `_QUERY_TRACKER_CACHE_TTL = 300.0` (5 perc) LRU mérettel és normalizált kulcsokkal. |
| [`server/app/modules/indexer_definitions/base_indexer_definition.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexer_definitions/base_indexer_definition.py) | **MÓDOSÍTOTT** | `_find_all_by_query` lapozási korlát 50 elemre (megelőzve a végtelen lapozási viharokat) deduplikációval (`pydash.uniq_by`). |
| [`server/app/modules/indexer_definitions/integrations/huntorrent.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexer_definitions/integrations/huntorrent.py) | **MÓDOSÍTOTT** | `_fetch_torrents_by_query` implementáció natív HunTorrent kereséssel. |
| [`server/app/modules/indexer_definitions/integrations/majomparade.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexer_definitions/integrations/majomparade.py) | **MÓDOSÍTOTT** | `_fetch_torrents_by_query` implementáció natív Majomparádé szöveges kereséssel. |
| [`server/app/modules/indexer_definitions/integrations/bithumen.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexer_definitions/integrations/bithumen.py) | **MÓDOSÍTOTT** | `_fetch_torrents_by_query` implementáció és IMDb nélküli torrentek megőrzése szöveges találatoknál. |
| [`server/app/modules/indexer_definitions/integrations/insane.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/indexer_definitions/integrations/insane.py) | **MÓDOSÍTOTT** | `_fetch_torrents_by_query` implementáció és IMDb nélküli torrentek megőrzése szöveges találatoknál. |
| [`server/app/modules/torrent_source_provider/service.py`](file:///d:/Git/stremhu-source-anime/server/app/modules/torrent_source_provider/service.py) | **MÓDOSÍTOTT** | `.torrent` fájlletöltési korlát a 30 legtöbb seedert birtokló torrentre `_find_by_query` esetén. |
| [`server/tests/test_stream_optimizations.py`](file:///d:/Git/stremhu-source-anime/server/tests/test_stream_optimizations.py) | **ÚJ** | Részletes unit tesztek a multi-tracker stream deduplikációra, Stremio címkézésre (zászló emojik, magyar felirat, letöltött állapot) és a relay boundary darab prioritásokra. |
| [`server/tests/test_extras_and_resolver.py`](file:///d:/Git/stremhu-source-anime/server/tests/test_extras_and_resolver.py) | **ÚJ** | Unit tesztek a kulcs nélküli Wikidata és web scraping TMDB/TVDB feloldásra, MAL leképezésre, Kitsu címmentésre és sorozat-katalógus működésére. |
| [`server/tests/test_torznab_query.py`](file:///d:/Git/stremhu-source-anime/server/tests/test_torznab_query.py) | **MÓDOSÍTOTT** | Torznab `tvdbid` és `tmdbid` query parse unit tesztek. |
| [`server/tests/test_torznab_search.py`](file:///d:/Git/stremhu-source-anime/server/tests/test_torznab_search.py) | **MÓDOSÍTOTT** | Torznab `q` alapú szöveges keresés integrációs tesztje. |

---

## 3. Rendszerarchitektúra és Működési Folyamatábrák

### 3.1. Kulcs Nélküli TMDB Feloldási Folyamat

```mermaid
flowchart TD
    Req["TMDB Azonosító Érkezése\n(pl. tmdb:693134 vagy tmdb:1399:1:1)"] --> CacheCheck{"Gyorsítótárban szerepel?\n(tmdb_{id}_{is_series})"}
    CacheCheck -- "Igen" --> ReturnCached["Gyorsítótárazott (IMDb, Cím) visszaadása"]
    CacheCheck -- "Nem" --> HasApiKey{"Van TMDB API kulcs\na konfigurációban?"}

    HasApiKey -- "Igen" --> TMDBApiCall["1. Lépés: Hivatalos TMDB API hívás\n(/3/movie vagy /3/tv external_ids)"]
    TMDBApiCall -- "Sikeres" --> SaveAndReturn["Eredmény mentése gyorsítótárba és visszaadás"]
    TMDBApiCall -- "Hiba / Nincs" --> WikiDataCall

    HasApiKey -- "Nem" --> WikiDataCall["2. Lépés: Wikidata SPARQL API\n(P4947 film / P4983 sorozat)\nUser-Agent: StremHUSource/1.0"]
    WikiDataCall -- "IMDb ID / Cím megtalálva" --> SaveAndReturn
    WikiDataCall -- "Nincs találat / Hiba" --> WebScrapeCall["3. Lépés: TMDB Weboldal Elemzés\n(User-Agent: curl/8.5.0)\nog:title és Eredeti cím kinyerése"]

    WebScrapeCall -- "Sikeres" --> SaveAndReturn
    WebScrapeCall -- "Sikertelen" --> NegativeCache["(None, None) visszaadása\n(Negatív gyorsítótár mérgezés nélkül)"]
```

### 3.2. Több-Trackeres Stream Összevonás (Deduplication)

```mermaid
flowchart TD
    InStreams["Bejövő TorrentStream lista\n(nCore, HunTorrent, Majomparádé)"] --> Loop{"Van még elem?"}
    Loop -- "Igen" --> GetStream["Következő stream lekérése"]
    GetStream --> KeyCalc["Kulcs generálás:\n1. info_hash\n2. (file_size, clean_name)"]
    KeyCalc --> CheckSeen{"Már feldolgozott kiadás?"}
    
    CheckSeen -- "Igen (Duplikátum)" --> Merge["Összevonás az Elsődleges találattal:\n- Indexer hozzáadása extra_indexer_names-hez\n- Különböző tracker esetén seederek összeadása\n- Letöltött / Felirat státusz átvétele\n- Hash és fájlkulcs összekapcsolása"]
    Merge --> Loop

    CheckSeen -- "Nem (Új release)" --> AddPrimary["Hozzáadás deduped listához\nElsődlegesként regisztrálás a hash és fájl térképekben"]
    AddPrimary --> Loop

    Loop -- "Nem" --> SortRank["Smart Filter / Preferenciák szerinti rendezés\n(Letöltött torrentek előre rangsorolva)"]
    SortRank --> Format["StremioStream formázás:\n🧲 nCore, HunTorrent, Majomparádé | 👥 645\n🇭🇺 Hun | 💬 🇭🇺 Hun Sub"]
```

---

## 4. Modulonkénti Részletes Működési Leírás és Invariánsok

### 4.1. `TvdbResolver` (`server/app/modules/stremio/tvdb_resolver.py`)
- **Kulcs nélküli Wikidata SPARQL feloldás:** Filmekhez a `P4947`, sorozatokhoz a `P4983` TMDb azonosító tulajdonságot használja. Ha `is_series` nem ismert (`None`), SPARQL `UNION` segítségével mindkettőt ellenőrzi.
- **Robot policy megfelelőség:** A Wikimedia User-Agent szabályzatának megfelelően: `"User-Agent": "StremHUSource/1.0 (https://github.com/s4pp1/stremhu-source; admin@stremhu.local) httpx/0.28.1"`.
- **TMDB Web Scraping tartalék (Fallback 3):** Ha a Wikidata nem tartalmazza a tételt, a publikus `themoviedb.org/{tv|movie}/{id}` oldalt elemzi `"User-Agent": "curl/8.5.0"` fejléccel. A kinyert címeknél a `html.unescape` fut le.
- **Negatív gyorsítótár-mérgezés védelem:** Hálózati hiba vagy sikertelen feloldás esetén `(None, None)` értékkel tér vissza gyorsítótárazás nélkül, így az átmeneti hibák nem ragadnak be.

### 4.2. Multi-Tracker Stream Deduplikáció (`server/app/modules/torrent_streams/service.py`)
- **Deduplikációs Kulcs:** Két stream azonos release-nek minősül, ha:
  1. Azonos a torrent swarm `info_hash` azonosítója (pl. mindkét oldalon ugyanaz a torrent fájl fut), **VAGY**
  2. Azonos a kinyert videófájl pontos mérete (`stream.file_size > 10_000_000` bájt) és normalizált tisztított neve (`clean_torrent_name(stream.file_name)`).
- **Intelligens Seeder Számítás:** Ha a duplikált találat **másik indexerről** származik (pl. nCore 384 seeder + HunTorrent 4 seeder + Majomparádé 6 seeder), a rendszer összeadja a seedereket (`combined_seeders = 394`). Ha ugyanarról az indexerről származik, a maximumot választja.
- **Tulajdonság Megőrzés:** Ha a duplikátumok bármelyike le van töltve helyben (`is_persisted_torrent = True`), vagy tartalmaz magyar feliratot (`has_hungarian_sub = True`), a megőrzött elsődleges stream automatikusan megkapja ezeket a jelzéseket.
- **Stream Lejátszás Stabilitása:** A lejátszási URL (`play_url`) mindig a legjobban rangsorolt (legmagasabb seederű, legmegbízhatóbb) elsődleges forrásból származik.

### 4.3. Stream Megjelenítés és Stremio/Nuvio Best Practice (`server/app/modules/stremio/schemas.py`)
- **Letöltött Tartalom Kiemelése:**
  - Ha `is_persisted_torrent = True`, a stream nevének elejére a `⚡ Letöltve | ` jelzés kerül (pl. `⚡ Letöltve | 📺 1080p`), a leírás legelső sorába pedig: `⚡ Helyben tárolva (Azonnali lejátszás)`.
- **Addon Prefix Eltávolítása:** A felhasználói élmény tisztaságának megőrzése érdekében nincs felesleges `[StremHU]` prefix a címben.
- **Alapértelmezett Nemzeti Zászlók:** A hangsávoknál a rendszer automatikusan zászló emojit renderel külön kapcsoló nélkül: `🇭🇺 Hun`, `🇬🇧 Eng`, `🇯🇵 Jap`, `🇩🇪 Ger`, `🇫🇷 Fra`, `🇮🇹 Ita`, `🇪🇸 Esp`, `🇷🇺 Rus`.
- **Magyar Felirat Jelzés:** Amennyiben a torrent fájllistájában magyar felirat (`.srt`, `.ass`, `.sub`, `.vtt` + `hun`, `hungarian`, `magyar`) vagy a torrent nevében `hunsub` található, a leírásban megjelenik a `💬 🇭🇺 Hun Sub` jelzés.
- **Összevont Tracker Lista:** A forrás sorban valamennyi indexer fel van tüntetve: `🧲 nCore, HunTorrent, Majomparádé | 👥 394 | 💾 8.68 GiB`.

### 4.4. Relay Boundary Piece Buffer Kiterjesztés (`server/app/modules/relay/entities.py`)
- Az ExoPlayer (Android, Google TV, Nuvio, Stremio mobil) a videofájl fejlécét (start) és az index-táblázatát (MKV Cues, MP4 moov atom a fájl végén) kéri be induláskor.
- A `_set_file_boundary_priorities` metódus korábban csak 1 darabot kért az elejéről és végéről azonnali prioritással (`deadline = 0`).
- Az új implementáció dinamikusan `min(3, total_pieces // 2)` darabot rangsorol azonnalira mind a fájl elejéről, mind a fájl végéről, megakadályozva a pufferelési timeoutot és biztosítva az azonnali lejátszásindítást.

### 4.5. Évadcsomag és Tracker Gyorsítótár Stratégia (`server/app/modules/indexers/service.py`)
- Az IMDb alapú tracker lekérdezések gyorsítótár élettartama (`_IMDB_TRACKER_CACHE_TTL`) 180 másodpercről **1800 másodpercre (30 percre)** lett növelve.
- Sorozatok egymást követő epizódjainak megtekintésekor a trackerek felé küldött hálózati kérések száma nullára csökken, mivel a szezonpakkok és epizódlisták a memóriából azonnal elérhetők.
- A helyi adatbázisban tárolt `.torrent` metaadatok (`torrent_files`) 7 napos LRU retencióval rendelkeznek, és minden eléréskor (`touch()`) frissülnek; a letöltött vagy aktívan relézett torrenteket a rendszer soha nem törli.

---

## 5. Teljesítménymérési Eredmények (A/B Benchmark az Upstream Main ellen)

A mérés a helyi Docker Desktop környezetben futott (`scripts/benchmark_local.py`), összevetve a korábbi upstream `s4pp1/stremhu-source:latest` (`:7072`) verziót az optimalizált branchünkkel (`:7070`):

| Teszteset | Metrika | Upstream Main (`:7072`) | Aktuális Branch (`:7070`) | Eredmény és Optimalizáció |
| :--- | :--- | :---: | :---: | :--- |
| **Inception** (`tt1375666`) — Film | 1. hívás (Cold) | 1488.6 ms | **6.8 ms** | **Azonnali tracker cache kiszolgálás** |
| | 2. hívás (Warm) | 1205.4 ms | **3.3 ms** | **365× gyorsabb** |
| | Streamek száma | 22 db | **19 db** | **Multi-tracker duplikációk összevonva (nCore + HunTorrent + Majomparádé: 645 és 394 seeder)** |
| **Stranger Things** (`tt4574334:1:1`) — Sorozat | 1. hívás (Cold) | 2859.6 ms | **2648.1 ms** | **prefetch-await integráció** |
| | 2. hívás (Warm) | 2652.2 ms | **3.6 ms** | **736× gyorsabb (Évadcsomag memóriagyorsítótár)** |
| | Streamek száma | 17 db | **14 db** | **Duplikált epizódok összevonva** |
| **One Piece** (`tt0388629:1:1`) — Anime | 1. hívás (Cold) | 1239.3 ms | 1443.1 ms | Párhuzamos keresés paritás |
| | 2. hívás (Warm) | 1250.1 ms | **3.9 ms** | **320× gyorsabb** |
| | Streamek száma | 3 db | 3 db | Teljes adatintegritás |
| **Dune 2** (`tmdb:693134`) — TMDB Film | 1. hívás (Cold) | ❌ *Hiba / 0 találat* | **1240.2 ms** | **39 db stream működik!** |
| | 2. hívás (Warm) | ❌ *Hiba / 0 találat* | **3.4 ms** | Kulcs nélküli feloldás |
| **Game of Thrones** (`tvdb:121361:1:1`) — TVDB Sorozat | 1. hívás (Cold) | ❌ *Hiba / 0 találat* | **1480.5 ms** | **34 db stream működik!** |
| | 2. hívás (Warm) | ❌ *Hiba / 0 találat* | **3.5 ms** | Kulcs nélküli feloldás |

---

## 6. Verifikációs Parancsok

Minden módosítás előtt és után kötelezően futtatandó ellenőrző sor:

```powershell
$env:HOST_IP="192.168.1.23"; $env:PYTHONIOENCODING="utf-8"; cd d:\Git\stremhu-source-anime\server; .venv\Scripts\python -m ruff check .; .venv\Scripts\basedpyright.exe; .venv\Scripts\python -m pytest tests
```
- **Ruff linter:** `All checks passed!`
- **Basedpyright:** `0 errors, 0 warnings, 0 notes`
- **Pytest:** `192 passed, 64 snapshots passed in 7.98s`
