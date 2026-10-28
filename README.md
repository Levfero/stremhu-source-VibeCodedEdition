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

### 🚀 Új funkciók részletesen

#### 1. Torrentben található feliratok felderítése és illesztése
- **Támogatott formátumok**: `.srt`, `.ass`, `.sub`, `.vtt` kiterjesztésű feliratfájlok automatikus érzékelése a letöltött vagy streamelt torrentekben (`is_subtitle`).
- **Nyelv- és típusfelismerés (`detect_subtitle_language`)**:
  - Automatikusan felismeri a feliratfájl nevéből a nyelvet (pl. magyar: `.hun.`, `.hu.`, `[HunSub]`; angol: `.eng.`, `.en.`, `[EngSub]`; egyéb nyelvek).
  - Érzékeli a kényszerített feliratokat (`Hungarian (Forced)`).
  - Ha nincs külön nyelvi jelölés a fájl nevében (pl. `01.ass`), a magyar trackerek jellegéből adódóan alapértelmezetten magyar feliratként jelöli meg (`Hungarian (Torrent)`).
- **Intelligens felirat-illesztés több fájlos pakkoknál (`resolve_subtitles`)**:
  - **Egyedi film / epizód**: Ha a torrentben csak egyetlen videófájl van, a torrent összes feliratát automatikusan hozzárendeli a videóhoz.
  - **Sorozatpakkok (Season Packs)**: Több videófájlt tartalmazó pakkok esetén a feliratokat intelligensen illeszti az éppen lejátszott videóhoz:
    - Fájlnév törzs (stem) egyezés alapján (pl. `Noragami_01.mkv` -> `Noragami_01.hun.ass`).
    - Epizódszám és évad alapján (pl. `Subs/01.ass` vagy `Subs/Noragami Aragoto - 01.ass` a 2. évad 1. részéhez).
  - **Extrák szűrése**: Megakadályozza, hogy az `Extras` mappában lévő menü- vagy bónuszfeliratok bekeveredjenek a valódi epizódokhoz.

#### 2. Felirat Streaming végpont & HTTP Range támogatás
- **Új felirat végpont**: `/api/{user_id}/subtitles/{token}/{filename}`
- **HTTP Range támogatás**: A végpont támogatja a `Range` és `If-Range` fejléceket (`206 Partial Content`), így a kliensek (pl. Stremio, Nuvio, ExoPlayer, VLC) darabokban, akadozásmentesen és gyorsan képesek betölteni a feliratokat közvetlenül a torrentből.
- **URL kódolás javítás**: A felirat URL-ek a `quote()` függvénnyel vannak kódolva, így a szóközöket, ékezeteket és speciális karaktereket (pl. `[AnimeRG] Noragami - 01 [1080p].ass`) tartalmazó fájlnevek is hibátlanul elérhetőek.

#### 3. Valós idejű ASS/SSA -> WebVTT konverter (`ass_to_vtt`)
- Az anime torrent kiadások döntő többsége stílusos `.ass` (Advanced SubStation Alpha) felirattal készül, amit a böngészők, okostévék és natív HTML5 videólejátszók közvetlenül nem képesek megjeleníteni.
- A beépített konverter a felirat lekérésekor menet közben, a memóriában alakítja át az ASS szkriptet tiszta **WebVTT** formátumra:
  - Eltávolítja a formázási és pozicionálási parancsokat (`\pos(...)`, `\c&H...`, `\b1`, `\i1`).
  - Átkonvertálja az időbélyegeket WebVTT formátumra (`00:01:23.450 --> 00:01:27.890`).
  - Megtartja a szöveget és a sortöréseket (`\N` -> `\n`).
- A rendszer minden `.ass` felirathoz automatikusan generál egy `.vtt` kiterjesztésű változatot is, így a kliens a natív feliratot és a kompatibilis WebVTT feliratot is választhatja.

#### 4. Stremio & Nuvio Subtitles API integráció
- **Manifest erőforrás regisztráció**: A Stremio manifestben deklarálásra került a `subtitles` erőforrás (`ShortManifestResource.SUBTITLES`).
- **Közvetlen Stremio felirat végpont**:
  - `/subtitles/{media_type}/{stream_id}.json` végpont, amely lekéri és kiszolgálja a feliratokat.
- **Stream szintű felirat továbbítás**: A Stremio és Nuvio stream válaszban a `subtitles` listában közvetlenül szerepelnek a torrent feliratai, így a lejátszás elindításakor azonnal kiválaszthatóak és bekapcsolhatóak a beépített lejátszóban.

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
