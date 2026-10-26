# StremHU Source – TVDB & Kitsu Anime Támogatás Fejlesztői Dokumentáció

## 📌 Áttekintés

A Stremio és Nuvio ökoszisztémában az animék és sorozatok gyakran nem közvetlen IMDb (`tt...`) azonosítókkal szerepelnek, hanem:
1. **TheTVDB azonosítókkal** (pl. `tvdb:348545:1:1` – AIOMetadata TVDB mód esetén)
2. **Kitsu azonosítókkal** (pl. `kitsu:41370:1` – Anime Kitsu, AIOMetadata Kitsu katalógus esetén)

A hazai torrent trackerek (mint az nCore) kizárólag IMDb azonosítók (`tt...`) alapján indexelik a tartalmakat. Ez a fejlesztés lehetővé teszi, hogy a StremHU Source transzparensen fogadja mindkét külső azonosítótípust, feloldja azokat a megfelelő IMDb ID-ra és epizódra, megkeresse a magyar/külföldi torrent forrásokat, és a pontos videófájlt indítsa el.

### 🌟 Generikus, Sorozat-független Architektúra
A rendszerben **nincsenek beégetett, sorozat-specifikus táblázatok vagy arc-nevek** (pl. nincsenek kódba vésve olyanok, hogy *One Piece Wano, Naruto Shippuden, Bleach* vagy fix évad-eltolási szótárak). Helyette a feloldás **teljesen generikus és dinamikus**: külső metaadat-szolgáltatókból (Anime Kitsu és Cinemeta) menet közben olvassa ki az abszolút és alternatív epizódszámokat bármilyen animére vagy sorozatra.

---

## 🛠️ Módosított és Létrehozott Komponensek

### 1. `server/app/modules/stremio/tvdb_resolver.py`
A feloldási logikát és a gyorsítótárazást kezelő központi modul.
- **TVDB $\rightarrow$ IMDb feloldás:**
  - Elsődleges: Publikus, kulcs nélküli **TVMaze API** (`https://api.tvmaze.com/lookup/shows?thetvdb={id}`).
  - Másodlagos (fallback): Opcionális **TMDB API** (`TMDB_API_KEY` a `.env`-ben).
- **Kitsu $\rightarrow$ IMDb és Évad/Rész feloldás:**
  - Elsődleges: A hivatalos **Anime Kitsu Meta API** (`https://anime-kitsu.strem.fun/meta/series/kitsu:{id}.json`), ami közvetlenül tartalmazza az IMDb ID-t és a pontos videónkénti `imdbSeason`, `imdbEpisode` és `absoluteEpisode` leképezést.
  - Másodlagos: Kitsu API (`canonicalTitle`) $\rightarrow$ TVMaze címkeresés.
- **Generikus Dinamikus Epizód Leképezés (`get_episode_mappings`):**
  - Bármilyen sorozathoz vagy animéhez (pl. One Piece, Demon Slayer, Attack on Titan stb.):
    1. **Anime Kitsu meta API:** Lekéri a sorozat IMDb metaadatait (`https://anime-kitsu.strem.fun/meta/series/{imdb_id}.json`), ahol minden részhez szerepel az `imdbSeason`, `imdbEpisode`, `absoluteEpisode` vagy `kitsuEpisode`. Ha a kliens S21E01-et keres, a rendszer dinamikusan kideríti, hogy az abszolút 892. rész, így a `SeriesInfo.alternate_episodes` listába bekerül a `892`.
    2. **Cinemeta kumulatív fallback:** Ha egy sorozat nincs a Kitsu-n, a Cinemeta epizódlistájából kiszámolja az előző évadok kumulatív epizódszámát, így univerzálisan megadja a folytonos epizódszámot.
- **Teljesítmény:** Memóriabeli szótár gyorsítótár (`_cache`) és párhuzamos lekérések elleni védelem (`KeyedLock`).

### 2. `server/app/modules/stremio/utils.py`
- Bővítve a `parse_stream_id` függvény:
  - `tvdb:<id>:<season>:<episode>` és `tvdb:<id>` felismerése $\rightarrow$ `TvdbStreamId`
  - `kitsu:<id>:<episode>` és `kitsu:<id>` felismerése $\rightarrow$ `KitsuStreamId`

### 3. `server/app/modules/stremio/enums.py` & `schemas.py`
- `ContentType.ANIME`, `MediaType.ANIME`
- `StreamIdType.TVDB`, `StreamIdType.KITSU`
- `TvdbStreamId`, `KitsuStreamId` Pydantic modellek felvétele a `StreamId` unióba.
- `StremioStream.from_imdb_torrent_stream`: `is_series: bool = False` paraméter hozzáadása. Sorozatok és animék esetén a leírás legelső sorában megjelenik a konkrét fájlnév (`📁 <file_name>`), sima filmeknél viszont nem.

### 4. `server/app/modules/stremio/service.py`
- Manifest bővítése:
  - `id_prefixes`: `["tt", "tvdb:", "tvdb", "kitsu:", "kitsu", "mal:", "anime:", ADDON_APP_PREFIX_ID]`
  - `types`: `[ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME]`
- `get_streams`:
  - Automatikus TVDB és Kitsu feloldás keresés előtt.
  - Ha a `series_info`-ban még nincs alternatív/abszolút epizódszám, automatikusan meghívja a generikus `get_episode_mappings(imdb_id, season, episode)` metódust, és feltölti a `series_info.alternate_episodes` mezőt.

### 5. `server/app/modules/torrent_streams/utils/series_parser.py`
- **Generikus szintaktikai felismerés:**
  - Nincsenek beégetett sorozatnevek vagy fejezetnevek.
  - **Számtartományok kezelése (`PATTERN_NUMERIC_RANGE`):** Az olyan pakkoknál, mint pl. `890-895.mkv` vagy `001-050.mkv`, kibontja a teljes epizódtartományt.
  - **Anime kötőjeles formátum (`PATTERN_ANIME_DASH_EPISODE`):** Felismeri a ` - 01 `, ` - 105 `, ` - 892 `, ` - 1089 ` formátumú anime kiadásokat.
  - **Nem-destruktív epizódszámozás:** A 3-4 jegyű számok nincsenek mesterségesen szétvágva évadra és epizódra, így megmarad a pontos abszolút epizódszám.

### 6. `server/app/modules/torrent_streams/utils/stream_file_resolver.py` & `resolver_helpers.py`
- **100% Sorozat-független fájlillesztés:**
  - `StreamFileResolver` nem vizsgál sorozatcímeket, nem tart fenn saját eltolási táblákat.
  - Kizárólag a `SeriesInfo.target_seasons` és `SeriesInfo.target_episodes` halmazokkal dolgozik (melyeket a dinamikus feloldó szolgáltat).
  - Ha a fájl neve tartalmazza a keresett évadot és epizódot (pl. S21E01), VAGY bármely alternatív/abszolút epizódszámot (pl. 892), a resolver tökéletesen és egyértelműen kiválasztja.
- **Gyermek-elsődlegesség és Mappakezelés:** Az anime gyűjtőmappák (pl. `Season 21` vagy `Pack Season 1`) esetén a resolver nem dobja el tévesen a mappát évadszám-eltérés miatt, ha a fájlokban megtalálható a keresett epizód.
- **Menüfájlok szűrése:** A `Disc Menü` és `menu` fájlok automatikusan kiszűrésre kerülnek.

### 7. Másodlagos (Fallback) Név Szerinti Keresés (nCore & Indexerek)
- **`ncore.py`:** Megvalósítva a `_fetch_torrents_by_query` metódus (`miben=nev`).
- **`BaseIndexerDefinition` & `IndexersService` & `TorrentSourceProviderService`:** Támogatják a `find_by_query` hívásokat.
- **`TorrentStreamsService` & `TvdbResolver`:** Ha az IMDb ID-ra 0 torrent érkezik (vagy mert a feltöltő nem adott meg IMDb ID-t az nCore-on), a Cinemeta API-ból automatikusan lekéri a sorozat nevét, és másodlagos keresést futtat név szerint az indexereken.

### 8. `server/tests/test_tvdb_streams.py`
- 13 automatizált unit és integrációs teszt mockolt adatokkal:
  - TVDB & Kitsu feloldás és gyorsítótár
  - Generikus dinamikus `get_episode_mappings` tesztelés
  - Cím alapú másodlagos keresés (fallback)
  - Egyedi anime fájlformátumok (`- 005`, `- 105`, `- 892`, számtartományok `890-895`)
  - Mind a **163 pytest teszt** offline lefut mockolt környezetben, garantálva, hogy a valós nCore fiók nem terhelődik feleslegesen tesztek futtatásakor.

---

## 🚀 Futtatás és Élesítés

### Helyi futtatás Dockerben (Windows / Teszt)
A projekt gyökerében lévő Dockerfile multi-stage build:
```powershell
# Build:
docker build -t stremhu-source:local .

# Indítás (a gép saját LAN IP címével):
docker run -d -p 7070:7070 -p 6881:6881/tcp -p 6881:6881/udp -e HOST_IP=<A_GEPED_LAN_IP_CIME> --name stremhu-test stremhu-source:local
```

### Éles szerverre való frissítés lépései
1. Másold át a módosított fájlokat a szerverre:
   - `server/app/modules/stremio/`
   - `server/app/modules/torrent_streams/`
   - `server/app/modules/indexer_definitions/`
   - `server/app/modules/indexers/`
   - `server/app/modules/torrent_source_provider/`
   - `server/tests/test_tvdb_streams.py`
2. Fordítsd újra vagy indítsd újra a konténert a szerveren:
   ```bash
   docker compose restart
   # vagy
   docker build -t stremhu-source . && docker restart stremhu
   ```
