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

> [!NOTE]
> A StremHU Source egy szabadidős projekt! Hibajelentéseket és fejlesztési ötleteket szívesen fogadunk kulturáltan a StremHU Discord szerverén!
