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

## 💬 Torrent Feliratok Kezelése és Streaming (`feature/torrent-subtitles` branch)

Ez a branch a **torrentben található külső feliratok automatikus felderítését, a videókhoz való illesztését, közvetlen streamelését, valamint valós idejű WebVTT konverzióját** valósítja meg Stremio és Nuvio kliensek számára.

### 🚀 Új funkciók összefoglalása
1. **Torrent feliratok felderítése és illesztése**: `.srt`, `.ass`, `.sub`, `.vtt` fájlok automatikus észlelése, nyelvi címkézés és illesztés a videóhoz.
2. **Közvetlen felirat streaming HTTP Range támogatással**: `/api/{token}/subtitles/...` végpont darabolt, pufferelésmentes kiszolgálással.
3. **Valós idejű ASS/SSA -> WebVTT konverzió**: On-the-fly konverter, amely eltávolítja a formázási címkéket, és WebVTT formátumot biztosít a böngészők és Nuvio számára.
4. **Stremio & Nuvio Subtitles API**: A `subtitles` erőforrás regisztrálása a manifestben és átadása a stream válaszban.

---

## 🔄 Hogyan működik a gyakorlatban? (End-to-End Workflow & Példák)

### 🎬 Gyakorlati példa 1: *Noragami Aragoto* S02E03 (Felirat feloldás és valós idejű WebVTT konverzió)

Az anime pakkokban a feliratok gyakran külső `.ass` (Advanced SubStation Alpha) fájlokban vannak (pl. `Subs/Noragami Aragoto - 03.ass`), amit sok kliens (pl. Nuvio beépített böngészős lejátszója) közvetlenül nem tud lejátszani.

```mermaid
sequenceDiagram
    autonumber
    actor Player as Lejátszó (Stremio / Nuvio)
    participant API as StremioRouter (/stream)
    participant Resolver as StreamFileResolver (resolve_subtitles)
    participant SubRouter as SubtitlesRouter (/api/.../subtitles)
    participant Converter as ass_to_vtt Konverter

    Player->>API: Epizód lekérése: Noragami Aragoto S02E03
    API->>Resolver: Videófájl és hozzá tartozó feliratok feloldása
    Note over Resolver: Torrent fájlokban megtalálja: "Noragami Aragoto - 03.ass"
    Resolver-->>API: Felirat hozzárendelve: ID=sub1, Lang="hun", URL="/subtitles/.../03.ass"
    API-->>Player: Stream válasz subtitles listával (eredeti .ass és .vtt változat)
    
    Player->>SubRouter: Felirat lekérése: GET /subtitles/.../03.ass.vtt (Range: bytes=0-)
    SubRouter->>SubRouter: Torrent darabok olvasása P2P memóriából
    SubRouter->>Converter: ass_to_vtt(nyers_ass_adat)
    Note over Converter: Eltávolítja a pozicionáló tageket {\pos(..)}, {\c&H..}<br/>Időzítés átalakítása: 00:01:23.450 --> 00:01:27.890
    Converter-->>SubRouter: Tiszta WebVTT formátum
    SubRouter-->>Player: HTTP 206 Partial Content (WebVTT formátumban)
    Note over Player: A felirat azonnal megjelenik a videón szinkronban!
```

#### A feliratfolyamat lépésről lépésre:
1. **Felirat felderítése a torrentben**: A `StreamFileResolver.resolve_subtitles()` megvizsgálja a torrent fájllistáját. Kiszűri a videófájlhoz tartozó feliratot (epizódszám és fájlnév-stem egyezés alapján: `Noragami Aragoto - 03.ass`), miközben a menük és extrák mappák feliratait elveti.
2. **Nyelvfelismerés**: A `detect_subtitle_language()` felismeri a nyelvet (`hun` / `Hungarian (Torrent)`).
3. **Dupla felirat elérhetőség a kliensnek**: A szerver mind a natív `.ass` formátumot, mind a virtuális `.vtt` kiterjesztést felkínálja a lejátszónak a stream JSON válaszban.
4. **Valós idejű konverzió lekéréskor**: Amikor a lejátszó a WebVTT változatot kéri, az `ass_to_vtt()` a memóriában feldolgozza a párbeszédeket, átalakítja az időbélyegeket, és a `SubtitlesRouter` HTTP Range fejlécekkel darabokban elküldi a lejátszónak.

---

### 🎬 Gyakorlati példa 2: *One Piece* (Anime abszolút epizódszámozás feloldása)

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
    Resolver-->>API: Kiválasztott videófájl és feliratok
    API-->>User: Lejátszható Stremio stream JSON válasz
    User->>Engine: Közvetlen videófolyam indítása (P2P várakozás nélkül)
```

---

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
