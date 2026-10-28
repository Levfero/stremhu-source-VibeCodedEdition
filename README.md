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
>
> A jelenlegi fork célja az anime, TVDB, Kitsu támogatás és fejlett feloldási logikák biztosítása a Stremio, Nuvio és Kodi felhasználók számára.

---

## 🌸 Anime, TheTVDB & Kitsu támogatás (`feature/anime-tvdb-kitsu-support` branch)

Ez a kiadás kiterjeszti a StremHU Source működését teljes körű anime és alternatív metaadat-kezeléssel, megoldva az anime rajongók legnagyobb problémáját: az anime címek, évadok és az abszolút vs. relatív epizódszámozások közötti eltéréseket a magyar és nemzetközi trackereken.

### 🌟 Főbb új funkciók és működés

#### 1. Kitsu és TheTVDB azonosítók natív kezelése
- **Kitsu Stream ID-k**: `kitsu:{kitsu_id}:{episode}` formátumú kérések natív támogatása (pl. Nuvio, Stremio Anime Kitsu addonok).
  - Lekéri a Kitsu API-ból az anime kanonikus és alternatív címeit, valamint a valódi évadszámot.
- **TheTVDB Stream ID-k**: `tvdb:{tvdb_id}:{season}:{episode}` formátumú kérések automatikus feloldása és leképezése IMDb azonosítóra.
- **Anime-Kitsu leképezési adatbázis**:
  - Automatikusan feloldja a Kitsu bejegyzéseket a hivatalos IMDb ID-ra és kiszámítja a globális abszolút epizódszámokat (pl. *Attack on Titan Season 3 Ep 2* -> IMDb `tt2560140`, Abszolút Epizód: `39`).

#### 2. Alternatív IMDb azonosítók párhuzamos lekérdezése (Aliasing)
- Bizonyos klasszikus és hosszú sorozatok (különösen animék) több külön IMDb bejegyzéssel is futnak a magyar torrent trackereken.
  - **Példa**: *Dragon Ball Z* esetén a trackereken megtalálható az eredeti 1989-es bejegyzés (`tt0121220`) és az 1996-os nemzetközi bejegyzés (`tt0214341`) is.
  - A rendszer felismeri az ilyen kapcsolatokat, és párhuzamosan lekéri az összes alternatív IMDb azonosító forrásait a trackerekről, majd a seederszám és minőség szerint egyesíti és rangsorolja a találatokat.

#### 3. Intelligens Anime Fájlfeloldó (`StreamFileResolver` & `SeriesParser`)
- **Abszolút számozású animék támogatása**: Ha a torrentben a fájlok folyamatos sorszámozással vannak ellátva (pl. `One Piece - 1089.mkv`), a rendszer a Wano arc évad/epizód kérését (pl. S21E01) automatikusan hozzáilleszti az abszolút sorszámhoz (`1089`).
- **Több évados pakkok és szezongyűjtemények**:
  - Helyesen kezeli a `Season 1`, `S01-S03`, `S01-S09 BDRip`, vagy számozott mappa-struktúrákat (pl. `01. Noragami`, `02. Noragami Aragoto`).
  - **Relatív prioritás későbbi évadoknál**: Megakadályozza, hogy a 2. évad mappájában lévő `13.mkv` fájlt tévesen a 2. évad 1. részének higgye pusztán az abszolút epizód indexelés miatt.

#### 4. Párhuzamosított metaadat-feloldás és gyorsítótárazás
- Aszinkron, szálbiztos és memóriakorlátos (bounded) cache védi a szervert a túlterheléstől.
- **Negatív gyorsítótár védelem**: átmeneti hálózati kimaradás vagy timeout esetén a rendszer nem gyorsítótáraz hibás üres választ, így a következő lekérés azonnal megkísérli az újrapróbálkozást.

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
   - A `parse_stream_id` felismeri az azonosító típusát (`KITSU`, `TVDB` vagy `IMDB`).

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
