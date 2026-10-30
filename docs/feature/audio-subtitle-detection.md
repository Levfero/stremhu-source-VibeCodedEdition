# Fejlesztői és AI Agent Dokumentáció: `feature/audio-subtitle-detection`

> [!IMPORTANT]
> **Viszonyítási alap (Baseline):**
> Ez a dokumentáció az eredeti **[s4pp1/stremhu-source](https://github.com/s4pp1/stremhu-source)** (`8c99c7d` commit) és a jelenlegi **`feature/audio-subtitle-detection`** ág közötti intelligens hang- és felirat-felismerési architektúrát, MediaInfo/Tech Info adatfeldolgozást és Stremio megjelenítést dokumentálja.

---

## 1. A Branch Célja és Motivációja

### 1.1. Upstream és Korábbi Hiányosságok
1. **Japán hangsáv téves felismerése és anime eltűnés:**
   - A korábbi regex kizárólag a `\b(jap(?:[-_. ]?dub)?|japanese|japán)\b` mintákat kereste a torrent- és fájlnevekben. Nem ismerte fel a nemzetközi anime release-eknél szabványos `JPN`, `ja-jp`, `Dual-Audio`, `Multi-Audio` megjelöléseket.
   - Az anime release-ek (pl. SubsPlease, HorribleSubs, ReinForce, Erai-raws, Coalgirls) 90%-ánál a címben **egyáltalán nincs nyelvi kód**, mert az anime natív nyelve magától értetődően a japán.
   - A magyar trackereken (nCore, HunTorrent, Majomparádé) nincs dedikált „Anime” kategória; az animék a nemzetközi sorozat (`hdser`, `xvidser`) vagy film (`hd`, `xvid`) kategóriába kerülnek.
   - Emiatt a korábbi tracker-feloldó (`_resolve_language`) vakon ráerőltette a `MediaAttributeKey.ENG` (`🇬🇧 Eng`) címkét minden külföldi kategóriás animére.
   - **Következmény:** Az animék angolként jelentek meg, a japán nyelvet preferáló felhasználók streamjei rossz sorrendbe kerültek, az angol nyelv kizárásakor (`strict_exclusion`) pedig a japán animék teljesen eltűntek a találatok közül.

2. **Magyar felirat jelenlétének hiánya a találati kártyákon:**
   - A felhasználó a Stremio vagy Nuvio felületén nem látta, hogy az adott release rendelkezik-e magyar felirattal (akár a torrentben lévő különálló `.srt`/`.ass` fájlként, akár az MKV konténerbe ágyazott felirat-sávként).
   - Különösen japán és idegen nyelvű filmeknél/animéknél kritikus információ a magyar felirat megléte.
   - A felhasználók számára az angol vagy egyéb idegen feliratok nem relevánsak, kizárólag a **magyar felirat** megléte határozza meg a választást.

### 1.2. A Megoldás
- **Tech Info & MediaInfo Integráció:** Kihasználjuk az nCore szerverén közvetlenül elérhető Tech Info (MediaInfo) és leírás adatokat (`ajax.php?action=nfo` és `ajax.php?action=torrent_drop`).
- **Valós hangsávok precíz azonosítása:** A MediaInfo `Audio #... Language` mezőiből és a leírás `Hangsáv: ...` fejlécéből kinyerjük a tényleges hangsávokat (`jap`, `hun`, `eng`), felülírva a téves tracker-kategória fallbacket.
- **Magyar felirat badge (`💬 🇭🇺 Felirat`):** A MediaInfo `Text #... / Subtitle #... Language: Hungarian` szekciójából, a leírás `Feliratok: ... Magyar` sorából, valamint a torrentben található különálló magyar `.srt`/`.ass` feliratfájlokból detektáljuk a feliratot, és a találati kártya 2. sorában kiemeljük.
- **Memória LRU gyorsítótár és aszinkron párhuzamosítás:** A letöltött és feldolgozott Tech Info adatokat memóriában gyorsítótárazzuk (`_TECH_INFO_CACHE`), a párhuzamos lekéréseket pedig szigorú 2.0 másodperces határidővel futtatjuk.

---

## 2. Módosított és Új Fájlok Áttekintése (Diff Mátrix)

| Modul / Fájl | Státusz | Változtatás összefoglalása |
| :--- | :---: | :--- |
| `server/app/modules/media_attributes/tech_info_parser.py` | **ÚJ** | `TechInfoMetadata` dataclass és `parse_tech_info_metadata` függvény: HTML entitás-tisztítás, szekció-alapú MediaInfo parser (Audio vs Text szétválasztás a téves besorolás elkerülésére), magyar és angol nyelvű MediaInfo kulcsszavak, szabadszavas leírás-elemző. |
| `server/app/modules/media_attributes/seeds.py` | Módosult | `MediaAttributeKey.JAP` regex mintájának kibővítése: `jpn`, `ja[-_]jp`, `dual[-_. ]audio`, `multi[-_. ]audio` támogatása. |
| `server/app/modules/indexer_definitions/base_indexer_definition.py` | Módosult | `fetch_tech_info(torrent_id)` metódus bevezetése a bázisosztályban. |
| `server/app/modules/indexer_definitions/integrations/ncore.py` | Módosult | `fetch_tech_info(torrent_id)` implementációja: párhuzamos lekérés az nCore `ajax.php?action=nfo` és `action=torrent_drop` végpontjairól. |
| `server/app/modules/torrent_streams/schemas.py` | Módosult | `TorrentStream.has_hungarian_sub: bool` mező bevezetése; `has_hungarian_sub=bool(subtitles)` automatikus beállítása a `from_imdb_id` és `from_torrent_id` metódusokban. |
| `server/app/modules/torrent_streams/service.py` | Módosult | `_enrich_torrent_streams_with_tech_info` és `_enrich_single_stream` metódusok, `_TECH_INFO_CACHE` LRU gyorsítótár, téves `ENG` fallback intelligens eltávolítása, és stream-dúsítás a szűrés/rendezés előtt. |
| `server/app/modules/torrent_streams/dependencies.py` | Módosult | `get_indexer_definitions_service()` injektálása a `create_torrent_streams_service` gyárfüggvénybe. |
| `server/app/modules/stremio/schemas.py` | Módosult | `readable_subtitle = "💬 🇭🇺 Felirat"` megjelenítése a stream kártya `description_second_line` mezőjében. |
| `server/tests/test_audio_subtitle_detection.py` | **ÚJ** | 8 db átfogó teszt: angol és magyar MediaInfo feldolgozás, szabad leírások elemzése, JPN és Dual-Audio regex tesztek, Stremio kártya felirat badge ellenőrzése, és Tech Info dúsítás mock indexerrel. |

---

## 3. Architektúra és Folyamatábrák

### 3.1. Tech Info Dúsítás és Keresési Szekvencia

```mermaid
sequenceDiagram
    autonumber
    actor Player as Lejátszó (Stremio / Nuvio)
    participant StremioService as Stremio API (/stream)
    participant StreamsService as TorrentStreamsService
    participant Cache as _TECH_INFO_CACHE (RAM)
    participant nCore as NcoreIndexerDefinition (ajax.php)
    participant Parser as tech_info_parser

    Player->>StremioService: GET /api/{key}/stremio/stream/series/tt2560140:1:1.json
    StremioService->>StreamsService: find_by_imdb(user, "tt2560140", series)
    StreamsService->>StreamsService: Torrentek feloldása (from_imdb_id)
    Note over StreamsService: torrent_streams: [TorrentStream, ...]

    StreamsService->>StreamsService: _enrich_torrent_streams_with_tech_info(candidates[:15])

    par Párhuzamos Tech Info Lekérés (asyncio.gather, timeout=2.0s)
        StreamsService->>Cache: Ellenőrzés: (indexer_id, torrent_id)
        alt Cache Hit
            Cache-->>StreamsService: TechInfoMetadata (0 ms)
        else Cache Miss
            StreamsService->>nCore: fetch_tech_info(torrent_id)
            nCore->>nCore: GET /ajax.php?action=nfo & action=torrent_drop
            nCore-->>StreamsService: (nfo_text, desc_text)
            StreamsService->>Parser: parse_tech_info_metadata(nfo_text, desc_text)
            Parser-->>StreamsService: TechInfoMetadata(audio={'jap'}, has_hun_sub=True)
            StreamsService->>Cache: Eltárolás (_TECH_INFO_CACHE)
        end
    end

    Note over StreamsService: JAP hozzáadása, téves ENG törlése,<br/>has_hungarian_sub = True beállítása
    StreamsService->>StreamsService: _filter_torrent_streams() & _sort_torrent_streams()
    StreamsService-->>StremioService: sorted_torrent_streams
    StremioService->>StremioService: StremioStream kártyák formázása
    Note over StremioService: "🇯🇵 Jap | 💬 🇭🇺 Felirat | 🔊 AAC"
    StremioService-->>Player: streams JSON válasz
```

### 3.2. MediaInfo és Leírás Elemzési Döntési Folyamat

```mermaid
flowchart TD
    Start[NFO / Tech Info és Leírás Szöveg] --> Clean[HTML tagek és entitások tisztítása, br cseréje újsorra]
    Clean --> Lines[Soronkénti feldolgozás]
    
    Lines --> SectionCheck{Szekció fejléc?}
    SectionCheck -->|Audio / Hang #...| SetAudio[current_section = audio]
    SectionCheck -->|Text / Szöveg / Felirat #...| SetText[current_section = text]
    SectionCheck -->|General / Video / Menu| SetOther[current_section = other]
    SectionCheck -->|Normál sor| PropertyCheck{current_section?}
    
    PropertyCheck -->|audio| CheckAudioProp[Language / Nyelv / Title vizsgálata:<br/>japán -> 'jap'<br/>magyar -> 'hun'<br/>angol -> 'eng']
    PropertyCheck -->|text| CheckTextProp[Language / Nyelv / Title vizsgálata:<br/>magyar / hun -> has_hungarian_sub = True]
    PropertyCheck -->|other| SkipLine[Kihagyás]
    
    Lines --> DescAudioRegex["Regex: Hang(?:sáv)? / Audio : ..."]
    DescAudioRegex --> AddDescAudio[Hangsávok felvétele]
    
    Lines --> DescSubRegex["Regex: Felirat(?:ok)? / Subtitles : ..."]
    DescSubRegex --> CheckDescSub{Tartalmaz 'magyar' / 'hun' szót?}
    CheckDescSub -->|Igen| SetDescSub[has_hungarian_sub = True]
    CheckDescSub -->|Nem| DoneSub[Nincs magyar felirat a leírásban]
    
    CheckAudioProp --> Merge[TechInfoMetadata összeállítása]
    CheckTextProp --> Merge
    AddDescAudio --> Merge
    SetDescSub --> Merge
    DoneSub --> Merge
```

---

## 4. Modulonkénti Részletes Működési Leírás és Kritikus Invariánsok

### 4.1. `tech_info_parser.py` (Kritikus Szekció-Elkülönítés)
- **A MediaInfo szekciók szigorú elválasztása:**
  - A MediaInfo kimenetekben a `Language : Hungarian` szöveg megjelenhet `Audio #1` alatt (magyar szinkron), vagy `Text #1` alatt (magyar felirat).
  - A parser szigorúan nyilvántartja a `current_section` állapotot (`audio`, `text`, `other`).
  - **Invariáns:** A `Text` szekcióban található nyelvi bejegyzés **soha nem kerülhet** az `audio_languages` halmazba, és az `Audio` szekcióban található nyelv **soha nem aktiválhatja** a `has_hungarian_sub` jelölőt.
- **Többnyelvű MediaInfo támogatás:**
  - Felismeri az angol (`Audio #1`, `Language : Japanese`, `Text #1`, `Language : Hungarian`) és a magyar nyelvű MediaInfo fejléceket (`Hang #1`, `Nyelv : japán`, `Szöveg #1`, `Nyelv : magyar`).
  - Felismeri a táblázatos pontozott MediaInfo formátumot (`Language...........: Japanese`).
- **Szabadszavas leírások kezelése:**
  - Az nCore `ajax.php?action=torrent_drop` leírásában lévő `<span ...>Hangsáv</span>:` és `<span ...>Feliratok</span>:` sorokat HTML-tisztítás után regex-szel elemzi.
  - Ha a leírás tartalmazza pl. a `Feliratok: Angol, Magyar` sort, a `has_hungarian_sub` értéke `True` lesz még akkor is, ha az NFO fájlban az MKV MediaInfoja nem tartalmazott beágyazott feliratot.

### 4.2. `torrent_streams/service.py` (Intelligens Attribútum Dúsítás)
- **Téves tracker fallback intelligens törlése:**
  - Ha a Tech Info szerint a tartalom rendelkezik japán hangsávval (`"jap" in meta.audio_languages`), beillesztjük a `MediaAttributeKey.JAP` modellt a stream attribútumai közé.
  - Amennyiben a Tech Info szerint a release **nem tartalmaz angol hangsávot**, de a tracker kategória (`hdser`) miatt korábban rákerült az `ENG` attribútum, az `ENG` attribútumot **töröljük**!
  - Ha a release valódi Dual-Audio (japán és angol szinkronnal), mindkét attribútum (`JAP` és `ENG`) megmarad, és a Stremio felületén `🇯🇵 Jap, 🇬🇧 Eng` formátumban jelenik meg.
- **Időzítés és szűrési sorrend:**
  - Az `_enrich_torrent_streams_with_tech_info` hívás közvetlenül a `TorrentStream.from_imdb_id` után, de **a `_filter_torrent_streams` és `_sort_torrent_streams` előtt** fut le!
  - Ennek köszönhetően:
    1. A felhasználó nyelvi preferenciái (pl. Japán nyelv előre sorolása) pontosan az új, helyes nyelvi címkék alapján érvényesülnek.
    2. A kizárási szabályok (pl. angol nyelv kizárása) nem dobják ki tévesen a csak japán szinkronnal rendelkező animéket.
- **Hálózati védelem és memóriás gyorsítótár:**
  - `_TECH_INFO_CACHE: dict[tuple[str, str], TechInfoMetadata]` legfeljebb 5000 elemet tárol memóriában.
  - A párhuzamos hívásokra 2.0 másodperces szigorú timeout vonatkozik (`asyncio.wait_for`). Időtúllépés vagy tracker-hiba esetén a kérés nem áll le, hanem biztonságosan a név-alapú adatokkal halad tovább.
  - Keresésenként legfeljebb az első 15 leginkább releváns találatra fut le a dúsítás, megóvva az nCore szerverét a túlterheléstől.

### 4.3. `stremio/schemas.py` (Kártya Megjelenítés)
- A Stremio stream kártya leírásának 2. sorában (`description_second_line`):
  - Ha `torrent_stream.has_hungarian_sub` igaz: megjelenik a **`💬 🇭🇺 Felirat`** jelölő.
  - Példák a felületen:
    - Japán anime magyar felirattal:
      `🇯🇵 Jap | 💬 🇭🇺 Felirat | 🔊 AAC | 🎞️ x264`
    - Dual-Audio anime magyar felirattal és veszteségmentes hanggal:
      `🇬🇧 Eng, 🇯🇵 Jap | 💬 🇭🇺 Felirat | 🎵 FLAC | 💿 Blu-ray`
    - Magyar szinkronos anime felirat nélkül:
      `🇭🇺 Hun | 🔊 5.1 | 💿 BDRip | 🎞️ x265`
    - Nyers japán release magyar felirat nélkül (pl. HorribleSubs):
      `🇯🇵 Jap | 🔊 AAC | 🎞️ x264`

---

## 5. Teljesítménymérési (Benchmark) és Tesztelési Referencia

### 5.1. Unit és Integrációs Tesztek
A `server/tests/test_audio_subtitle_detection.py` tesztcsomag lefedte az összes funkciót:
1. `test_tech_info_parser_english_mediainfo`: Angol nyelvű MediaInfo és leírás kombinációjának tesztelése (Japán + Angol audio, Magyar felirat a leírásból).
2. `test_tech_info_parser_hungarian_mediainfo`: Magyar nyelvű MediaInfo tesztelése (`Hang #1 Nyelv: japán`, `Szöveg #1 Nyelv: magyar`).
3. `test_tech_info_parser_hungarian_audio_only`: Csak magyar szinkronos tartalom felirat nélkül (`has_hungarian_sub == False`).
4. `test_tech_info_parser_english_only_no_hungarian`: Csak angol nyelvű tartalom (idegen felirat nem aktiválja a magyar jelölőt).
5. `test_tech_info_parser_empty_and_garbage`: Üres és hibás HTML bemenetek robusztussága.
6. `test_japanese_regex_matching_in_seeds`: `JPN`, `Dual-Audio`, `Multi-Audio` és `Japanese` cím-alapú illeszkedése.
7. `test_stremio_stream_subtitle_badge`: Stremio kártya formázásának ellenőrzése (`💬 🇭🇺 Felirat` és `🇯🇵 Jap` megléte).
8. `test_enrich_torrent_streams_with_tech_info`: Stream attribútumok dinamikus cseréje (téves ENG fallback eltávolítása, JAP és magyar felirat beállítása).

**Futtatási eredmény:**
```powershell
$env:HOST_IP="192.168.1.23"; $env:PYTHONIOENCODING="utf-8"; cd d:\Git\stremhu-source-anime\server; .venv\Scripts\python -m ruff check .; .venv\Scripts\basedpyright.exe; .venv\Scripts\python -m pytest tests
```
- `ruff check`: **0 hiba (All checks passed)**
- `basedpyright`: **0 errors, 0 warnings, 0 notes**
- `pytest`: **231 passed, 0 failed in 2.50s**

### 5.2. Élő Docker Desktop Teszt Éles Tracker Adatokkal
A helyi Docker tesztkonténeren (`https://192-168-1-23.local-ip.medicmobile.org:7070`) végrehajtott valós lekérdezések eredménye:

1. **Attack on Titan S01E01 (`tt2560140:1:1`):**
   - *Korábban:* Mind a 6 nCore release tévesen `🇬🇧 Eng` címkét kapott, felirat-információ nélkül.
   - *Jelenlegi ágon:*
     - `Shingeki no Kyojin - 01 (BD 1280x720 x264 AAC).mkv`: **`🇯🇵 Jap | 💬 🇭🇺 Felirat`**
     - `[ReinForce] Shingeki no Kyojin - 01 (BDRip 1920x1080 x264 FLAC).mkv`: **`🇯🇵 Jap | 💬 🇭🇺 Felirat`**
     - `Attack on Titan S01E01.mkv`: **`🇯🇵 Jap`**
     - `[HorribleSubs] Shingeki no Kyojin - 01 [480p].mkv`: **`🇯🇵 Jap`** (angol felirat, ezért nincs `Felirat` badge)
     - `Attack.on.Titan.S01E01.BDRIP.x264-Krissz.mkv`: **`🇬🇧 Eng | 💬 🇭🇺 Felirat`**

2. **Fullmetal Alchemist: Brotherhood S01E01 (`tt1355642:1:1`):**
   - `[Coalgirls] Fullmetal Alchemist Brotherhood 01 (Blu-ray FLAC)` (nCore ID 812207):
     - **`🇬🇧 Eng, 🇯🇵 Jap | 💬 🇭🇺 Felirat | 🎵 FLAC | 💿 Blu-ray`** (pontosan megegyezik az nCore NFO-val és a CoolTeam Anime leírásával).
   - `01.mkv` (magyar szinkronos): **`🇭🇺 Hun | 🔊 5.1 | 💿 BDRip`**.
