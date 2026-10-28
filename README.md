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

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
