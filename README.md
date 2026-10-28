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

## 🌸 Anime, TheTVDB & Kitsu támogatás (`main` branch)

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

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
