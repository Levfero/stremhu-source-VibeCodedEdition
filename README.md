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

### 🚀 Új funkciók összefoglalása
1. **TMDB Stream ID támogatás**: `tmdb:{id}` és `tmdb:{id}:{season}:{episode}` azonosítók fogadása és feloldása a TheMovieDb API-ból.
2. **Többjelöltes másodlagos szöveges keresés**: Automatikus címfeldolgozás, ékezetmentesítés és névelő-eltávolítás az IMDb azonosító nélküli tartalmak megtalálásához.
3. **nCore Boolean IMDb ID javítás**: Megelőzi a `ValidationError` hibákat, ha az nCore scraping `false` értéket ad vissza IMDb link hiányában.
4. **Extrák és menük szűrése**: Menük (`BD Menü`, `DVD Menu`) és extra mappák (`00. Extrák`) kizárása normál évadoknál, valamint a Specials (0. évad) és OAD/OVA/SP fájlok feloldása.

---

## 🔄 Hogyan működik a gyakorlatban? (End-to-End Workflow & Példák)

### 🎬 Gyakorlati példa 1: *"A visszavágó - kapj el, ha tudsz!"* (Kivételes eset: Magyar műsor IMDb ID nélkül)

Gyakori probléma, hogy a hazai gyártású sorozatoknak, vetélkedőknek vagy reality műsoroknak egyáltalán nincs IMDb adatlapjuk, a trackereken (pl. nCore) ékezet nélkül töltik fel őket, az oldal pedig `imdb_id: false` értéket ad vissza.

```mermaid
sequenceDiagram
    autonumber
    actor User as Felhasználó (Nuvio / Stremio)
    participant Router as StremioRouter
    participant TMDB as TvdbResolver (TMDB feloldó)
    participant Service as TorrentStreamsService
    participant Provider as Tracker Kereső (nCore)
    participant Resolver as StreamFileResolver (Fájlillesztés)

    User->>Router: Kérés indítása: tmdb:273867:1:1
    Router->>TMDB: resolve_tmdb("273867", season=1, episode=1)
    TMDB-->>Router: IMDb ID: None (nincs!), Cím: "A visszavágó - kapj el, ha tudsz!"
    Router->>Service: find_by_imdb(imdb_id=None, fallback_title="A visszavágó - kapj el, ha tudsz!")
    
    Note over Service: Nincs IMDb azonosító -> Másodlagos jelöltgenerálás!
    Service->>Service: 1. Alcím levágása: "A visszavágó"<br/>2. Ékezetmentesítés: "A visszavago"<br/>3. Névelő eltávolítása: "visszavago"
    
    Service->>Provider: Keresés trackeren: find_by_query("visszavago")
    Note over Provider: nCore scraping: imdb_id: false -> sanitize_imdb_id() -> None
    Provider-->>Service: Találat: "A.visszavago.S01E01.720p.HDTV.x264.HUN"
    
    Service->>Resolver: Fájlstruktúra vizsgálata: S01E01 keresése
    Resolver-->>Service: Kiválasztott videófájl: "A.visszavago.S01E01.mkv"
    Service-->>User: Lejátszható stream válasz (azonnali indítás)
```

#### A folyamat részletei:
1. **A kérés beérkezése**: A Nuvio / Stremio a TheMovieDb alapján küldi a kérést: `tmdb:273867:1:1`.
2. **TMDB metaadat-lekérdezés**: A `TvdbResolver.resolve_tmdb` lekéri a TMDB API-t. Kiderül, hogy a sorozatnak nincs nemzetközi IMDb azonosítója (`imdb_id=None`), de megvan a hivatalos címe: *"A visszavágó - kapj el, ha tudsz!"*.
3. **Másodlagos jelöltgenerálás**: A `TorrentStreamsService` felismeri, hogy nincs IMDb ID, ezért intelligens jelöltlistát állít össze a magyar trackerek keresési sajátosságaihoz igazodva:
   - 1. jelölt: `"A visszavágó"` (kötőjel utáni alcím levágása)
   - 2. jelölt: `"A visszavago"` (ékezetmentesítés, mert a trackereken szinte minden cím ékezet nélkül szerepel)
   - 3. jelölt: `"visszavago"` (kezdő névelő eltávolítása, mivel a szigorú keresők elhasalnak az "A " névelőn)
4. **Tracker lekérdezés & nCore javítás**: A tracker kereső lekérdezi a `"visszavago"` kulcsszót. Az nCore felülete `imdb_id: false` mezőt ad vissza, amit a `sanitize_imdb_id` validátor `None`-ra tisztít, megelőzve az 500-as hibát.
5. **Fájlillesztés és lejátszás**: A megtalált torrentből az 1. évad 1. része sikeresen feloldódik és várakozás nélkül elindul.

---

### 🎬 Gyakorlati példa 2: *Noragami Aragoto* (Extrák mappa kiszűrése vs. Különkiadások megnyitása)

Sok anime kiadás tartalmaz menüfájlokat és extrákat (pl. BDBOX kiadások):
- Mappastruktúra:
  - `00. Extrák/BD Menü/S02/Vol 2.mkv` (2 GB-os BD menü videó)
  - `02. Noragami Aragoto/[ReinForce] Noragami Aragoto - 01 (BDRip).mkv` (Valódi 1. rész)
  - `01. Noragami/[ReinForce] Noragami OAD1 (DVDRip).mkv` (Különkiadás / OVA)

```mermaid
graph TD
    Request[Beérkező kérés] --> Decision{Milyen évadot keresünk?}
    
    Decision -->|Normál évad pl. S02E01| FilterNormal[Extrák mappák szűrése]
    FilterNormal --> ExcludeExtras[00. Extrák & BD Menü KIZÁRVA]
    ExcludeExtras --> MatchReal[Valódi epizód kiválasztása: Noragami Aragoto - 01.mkv]
    
    Decision -->|Különkiadás pl. S00E01 / Specials| FilterSpecials[Különkiadás feloldása]
    FilterSpecials --> AllowExtras[Extrák és OAD/OVA vizsgálata engedélyezve]
    AllowExtras --> MatchSpecial[Különkiadás kiválasztása: Noragami OAD1.mkv]
```

- **Normál 2. évad 1. rész kérésekor (`S02E01`)**: Az `is_extra_folder` azonnal felismeri a `00. Extrák` és `BD Menü` mappákat, így kizárja őket. A lejátszó a valódi epizódot kapja meg a menüvideó helyett.
- **Különkiadás kérésekor (`S00E01` / Specials)**: A feloldó engedélyezi a belépést az extrák közé, a `SeriesParser` felismeri az `OAD1` jelölést, és pontosan a különkiadást adja vissza.

---

### 🎬 Gyakorlati példa 3: *One Piece* (Anime abszolút epizódszámozás feloldása)

A `main` branch-ben bevezetett alap anime feloldási logika működése a Wano arc 1089. részén keresztül:

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

---

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
