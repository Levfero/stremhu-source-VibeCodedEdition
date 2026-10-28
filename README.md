<p align="center">
  <a href="https://stremhu.app" target="_blank"><img src="./docs/assets/logo.png" width="100" alt="StremHU Logo" />
  </a>
</p>

<p align="center">Self-hosted streaming szerver, amellyel <b>közvetlenül a torrent oldalakról</b> nézheted a tartalmat várakozás nélkül!</p>
<p align="center">További információkért és útmutatókért látogass el a <a href="https://stremhu.app" target="_blank">https://stremhu.app</a> oldalra!</p>

<p align="center">
  <a href="https://discord.gg/jRSPPY5XaN" target="_blank">
    <img src="https://img.shields.io/badge/Discord-Csatlakozz-5865F2?logo=discord&logoColor=white" alt="StremHU Discord" />
  </a>&nbsp;
  <a href="https://hub.docker.com/r/s4pp1/stremhu-source" target="_blank">
    <img src="https://img.shields.io/badge/Docker-Repository-2496ED?logo=docker&logoColor=white" alt="StremHU Docker Hub" />
  </a>
</p>

<p align="center">
  Ha hasznosnak találod a projektet, támogasd egy ⭐-gal, és ha teheted, hívd meg az eredeti készítőt egy kávéra! ☕<br><br>
  <a href='https://ko-fi.com/s4pp1' target='_blank'>
    <img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi5.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' />
  </a>
</p>

> [!IMPORTANT]
> **Fork információk és köszönetnyilvánítás:**
> Ez a repository a hivatalos **[StremHU Source](https://github.com/s4pp1/stremhu-source)** projekt forkja, melynek eredeti készítője és fejlesztője **[s4pp1](https://github.com/s4pp1)**.
> - **Eredeti GitHub repository:** [https://github.com/s4pp1/stremhu-source](https://github.com/s4pp1/stremhu-source)
> - **Hivatalos weboldal & dokumentáció:** [https://stremhu.app](https://stremhu.app)
> - **Hivatalos Docker Hub:** [s4pp1/stremhu-source](https://hub.docker.com/r/s4pp1/stremhu-source)

---

## 🔍 Keresési és Szűrési Továbbfejlesztések (`feature/search-filter-tmdb-tvdb` branch)

Ez a branch a **TMDB azonosítók kezelését**, a **másodlagos többjelöltes szöveges fallback keresést**, az **nCore hibajavítást**, valamint az **Extrák és Különkiadások intelligens szűrését** valósítja meg.

### 🚀 Új funkciók részletesen

#### 1. TMDB Stream ID támogatás (TheMovieDb)
- **Kliens támogatás**: Kifejezetten a Stremio, Nuvio és modern kiegészítők által küldött TMDB azonosítók kezelésére:
  - Filmek: `tmdb:{tmdb_id}` (pl. `tmdb:273867`)
  - Sorozatok: `tmdb:{tmdb_id}:{season}:{episode}` (pl. `tmdb:273867:1:3`)
- **API feloldás (`TvdbResolver.resolve_tmdb`)**:
  - Automatikusan lekérdezi a TheMovieDb API-t (`api.themoviedb.org/3/movie/...` vagy `/tv/...`), hogy megtalálja a tartalomhoz tartozó hivatalos IMDb azonosítót és a kanonikus címet.
  - Ha az adott epizódhoz nincs IMDb ID rendelve, a lekérdezett címmel azonnal átadja a vezérlést a másodlagos szöveges keresőnek.
- **Stremio Series Search Manifest**: A Stremio katalógus keresésben engedélyezve lett a sorozatok keresése TMDB előtaggal is.

#### 2. Többjelöltes másodlagos szöveges keresés (Fallback Title Search)
A magyar torrent trackereken számos tartalom nem rendelkezik IMDb azonosítóval (különösen hazai tévéműsorok, vetélkedők, dokumentumfilmek), vagy az IMDb ID alapján kapott torrentek között hiányzik az adott epizód.
- **Működése**:
  1. **Alap tisztított cím**: Alcímek, kettőspontok és gondolatjelek levágása (pl. *"A visszavágó - kapj el, ha tudsz!"* -> *"A visszavágó"*).
  2. **Ékezetmentesítés**: A hazai trackerek 99%-ban ékezetmentes címkékkel töltik fel a fájlokat, ezért automatikusan generálódik az ékezet nélküli lekérdezés (`A visszavago`).
  3. **Névelők eltávolítása**: Eltávolítja a kezdő névelőket (*A*, *Az*, *The*), így a trackerek szigorú keresője azonnal megtalálja a tartalmat (`visszavago`).
  4. **Kulcsszavas keresés**: Több szóból álló címeknél az első két szóval, illetve az első önálló szóval is kísérletet tesz.
  5. **Címkinyerés meglévő torrentekből**: Ha egy sorozat másik évadának torrentje már megérkezett a trackerről, a rendszer kinyeri a tiszta sorozatcímet a torrent nevéből és azzal indít célzott keresést az elmaradt epizódra.

#### 3. nCore Scraping & Boolean IMDb ID validációs javítás
- Az nCore trackeren, ha egy feltöltéshez nem csatoltak IMDb linket, az oldal adatstruktúrája `false` logikai értéket ad vissza szöveg vagy `None` helyett.
- A modellbe és az nCore scraperbe épített `sanitize_imdb_id` validátor ezt automatikusan `None`-ra tisztítja, megelőzve a Pydantic `ValidationError` miatti 500-as szerverhibákat.

#### 4. Extrák, Menük és Különkiadások (Specials) szűrése
- **Extrák és menük kizárása normál évadoknál**:
  - Automatikusan kiszűri a `00. Extrák`, `Extras`, `Specials`, `BD Menü`, `DVD Menu`, `Borítók`, `NCED`, `NCOP` és hasonló bónusz mappákat és fájlokat normál évadok feloldásakor.
  - Megakadályozza, hogy a lejátszó véletlenül egy BD menüt vagy extra videót válasszon ki a tényleges epizód helyett.
- **Különkiadások (0. évad) natív támogatása**:
  - Ha a felhasználó kifejezetten a Különkiadásokat (Season 0) kéri (pl. Nuvio Specials fül), a feloldó belép az Extrák mappákba.
  - A `SeriesParser` felismeri az `OAD`, `OVA`, `SP`, `Special` jelöléseket (pl. `[ReinForce] Noragami OAD1 (DVDRip).mkv` -> Season 0 Episode 1).

---

## 🌸 Előzmények: Anime, TheTVDB & Kitsu támogatás (a `main` branch-ből)

Ez a kiadás magában foglalja a `main` branch-ben elérhető összes anime fejlesztést:
- **Kitsu & TVDB azonosítók kezelése**: `kitsu:{id}:{ep}` és `tvdb:{id}:{season}:{ep}` formátumok.
- **Alternatív IMDb ID-k párhuzamos lekérdezése**: Pl. Dragon Ball Z 1989 (`tt0121220`) és 1996 (`tt0214341`) egyidejű feloldása.
- **Intelligens fájlfeloldó**: Abszolút epizódszámok és több évados gyűjtemények (pakkok) pontos illesztése.

---

## 🔄 Hogyan működik a kérésfeldolgozás? (End-to-End Workflow)

Amikor a kliensben (pl. **Stremio**, **Nuvio** vagy **Kodi**) kiválasztasz egy anime részt, a StremHU Source a háttérben több összetett lépésen keresztül oldja fel az eltérő számozásokat és trackereket.

### 🎬 Gyakorlati példa: *One Piece* (Wano Arc – 1089. rész)

Tegyük fel, hogy a Stremio vagy Nuvio felületén elindítod a **One Piece** 1089. részét:

```mermaid
sequenceDiagram
    autonumber
    actor User as Felhasználó (Stremio / Nuvio)
    participant API as StremioRouter (/stream)
    participant TVDB as TvdbResolver (Metaadatok)
    participant Indexer as Tracker Kereső (nCore, stb.)
    participant Resolver as StreamFileResolver (Fájlillesztés)
    participant Engine as BitTorrent Streaming Engine

    User->>API: Lekérés: kitsu:12:1089 vagy series:tt0388629:21:1
    API->>TVDB: Metaadat és alternatív számozás lekérése
    TVDB-->>API: IMDb: tt0388629, Évad: 21, Rész: 1, Alt. epizódok: [1089]
    API->>Indexer: Keresés trackereken (IMDb ID és cím alapján)
    Indexer-->>API: Visszaadott torrent találatok listája
    API->>Resolver: Fájlok vizsgálata a torrenten belül
    Note over Resolver: Felismeri: [SubsPlease] One Piece - 1089.mkv == S21E01!
    Resolver-->>API: Kiválasztott videófájl
    API-->>User: Lejátszható Stremio stream JSON válasz
    User->>Engine: Közvetlen videófolyam indítása (P2P várakozás nélkül)
```

#### A folyamat lépésről lépésre:

1. **Beérkező kliens kérés**:
   - A kliens kérést küld a StremHU szervernek:
     - Kitsu kiegészítő esetén: `kitsu:12:1089` (Kitsu sorozat ID `12`, epizód `1089`).
     - IMDb / Cinemeta esetén: `series:tt0388629:21:1` (One Piece Wano Country Arc, Season 21 Episode 1).
   - A `parse_stream_id` felismeri az azonosító típusát (`KITSU`, `TVDB`, `TMDB` vagy `IMDB`).

2. **Metaadat és számozás feloldása (`TvdbResolver`)**:
   - **Kitsu feloldás**: A Kitsu API és az `anime-kitsu` mapping adatbázis segítségével a szerver megtalálja a sorozat hivatalos IMDb azonosítóját (`tt0388629`), a sorozat címét (*One Piece*), és kiszámítja a szezonális leképezést (Season 21 Episode 1).
   - **Abszolút epizódszám csatolása**: Az anime kiadások a trackereken szinte kizárólag folyamatos számozást használnak (`1089`) a szezonális (`S21E01`) helyett. A resolver az alternatív epizódszámok közé felveszi az abszolút számot: `alternate_episodes = [1089]`.
   - **Alternatív IMDb azonosítók (Aliasing)**: Megvizsgálja, tartozik-e másik IMDb ID a sorozathoz (pl. *Dragon Ball Z* esetén az 1989-es és 1996-os kiadás).

3. **Torrent források felkutatása (`TorrentStreamsService`)**:
   - A szerver párhuzamos lekérdezéseket indít a beállított trackerek (pl. nCore, BitHUmen, iNSANE) felé az IMDb azonosító(k) alapján: `find_by_imdb_id("tt0388629")`.
   - *Fallback keresés*: Ha az IMDb ID-ra nincs találat, vagy a letöltött torrentpakkokban hiányzik az adott epizód, automatikus szöveges másodlagos keresés indul (pl. tisztított, ékezetmentes címekkel: `One Piece`).

4. **Intelligens fájlfeloldás a torrenten belül (`StreamFileResolver` & `SeriesParser`)**:
   - A beérkező torrentek fájllistájában a feloldó elemzi a struktúrát:
     - **Heti különálló release**: Pl. `[SubsPlease] One Piece - 1089 (1080p) [98E7B28B].mkv`. A `SeriesParser` azonosítja a `- 1089` epizódszámot. A feloldó egyezteti: `1089` megegyezik a kért `alternate_episodes=[1089]` értékkel -> **Azonnali találat!**
     - **Évadpakk (Season Pack)**: Pl. `One Piece S21 1080p/One Piece - E01.mkv`. A mappa alapján az évad 21, a fájl alapján az epizód 1 -> relatív évad/epizód egyezés -> **Találat!**
     - **Több évados gyűjtemény**: A feloldó kizárja a menüket, extrákat (`00. Extrák`, `BD Menü`) és más évadok számozásait, biztosítva, hogy ne keveredjenek össze a részek.

5. **Szűrés, rangsorolás és közvetlen streamelés**:
   - A találatokat a szerver a felhasználó beállításai szerint rendezi (pl. seederszám, felbontás, minőség, kizárt kulcsszavak).
   - Generálja a közvetlen stream hivatkozást a belső BitTorrent engine felé.
   - A kliens megkapja a JSON választ, és a lejátszás várakozás és előzetes letöltés nélkül, azonnal elindul!

---

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
