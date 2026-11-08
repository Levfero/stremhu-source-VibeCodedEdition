# Fejlesztői Dokumentáció: `feature/tracker-parity`

| Metaadat | Érték |
|---|---|
| **Típus / státusz** | Implementációs terv (vázlat). Implementáció még nem kezdődött. |
| **Dátum** | 2026-11-08 |
| **Alap** | `main` `2615596`. Az ellenőrzés kódolvasás volt a `main`-en és a `feature/stability-and-optimization` (PR #9) ágon; élő trackerkérés nem történt. |
| **Hatókör** | A csak nCore-on (vagy csak egyes trackereken) működő funkciók kiterjesztése a többi támogatott trackerre: bitHUmen, HunTorrent, iNSANE, Majomparádé, FileList, IPTorrents. |
| **Viszonyítási alap (upstream)** | [`s4pp1/stremhu-source`](https://github.com/s4pp1/stremhu-source) `8c99c7d` |

---

## 1. Cél

A funkciók mind a hét trackeren egyformán működjenek, ahol ez technikailag lehetséges. Ahol nem, ott a korlát legyen dokumentálva és a felületen is érthető.

## 2. Jelenlegi képességmátrix (kódolvasás, `main` `2615596`)

Adapterek: `server/app/modules/indexer_definitions/integrations/*.py`, alaposztály: `base_indexer_definition.py`.

| Képesség | nCore | bitHUmen | HunTorrent | iNSANE | Majomparádé | FileList | IPTorrents |
|---|---|---|---|---|---|---|---|
| IMDb-keresés (`_fetch_torrents`) | ✅ (API, `miben=imdb`) | ✅ IMDb-link a sorban | ✅ IMDb-link a sorban | ✅ IMDb-link a sorban | ✅ IMDb-link a sorban | ✅ (`searchin=3`) | ⚠️ szöveges keresés, soronkénti IMDb-ellenőrzés nélkül, max. 20, lapozás nélkül |
| Szabadszavas keresés (`_fetch_torrents_by_query`) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Film/sorozat szűrés a trackeren | ✅ (`_NCORE_MOVIE/SERIES_CATEGORIES`) | ❌ (kategóriatérkép van) | ❌ (kategória-útvonal van) | ❌ (kategóriatérkép van) | ❌ (kategóriatérkép van) | ❌ (kategóriatérkép van) | ❌ (kategóriatérkép van) |
| Tech Info / NFO (`fetch_tech_info`) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Kétlépcsős bejelentkezés (`supports_totp`) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Hit'n'Run lista | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Teljes letöltés kötelező (`requires_full_download`) | — | — | — | — | ✅ (H&R szabály) | — | — |
| Egyidejű kérés (`max_concurrent`) | 5 | 5 | 3 | 5 | 5 | 5 | 1 |
| Tesztfájlok, amelyek említik (`main`; a PR #9 ágon nCore: 16) | 11 | 1 | 1 | 0 | 1 | 0 | 0 |

**Trackerfüggetlen és már ma közös:** az IMDb-alapú streamkeresés és az azonosító-feloldás (TMDB, TVDB, Kitsu, MAL, anime), a fájlszintű feloldás (`StreamFileResolver`, `SeriesParser`), a deduplikáció, a tracker-sorrend, a torrentben lévő feliratok, az ASS→VTT konverzió, a relay-streamelés és a gyorsítótár.

## 3. Munkacsomagok

### TP1 — Film/sorozat szétválasztás mind a 7 trackeren (minta nélkül megvalósítható)

- **Most:** a katalóguskeresés (`StremioCatalogsService.get_catalog`) a `media_type`-ot továbbadja a `find_by_query`-nek, de ezt csak az nCore használja. A többi trackernél a `SeriesParser` utólag, a névből szűr; ez pontatlanabb.
- **Terv:**
  - Új mező az `IndexerDefinitionTorrent`-ben (`schemas/internal.py`): `content_kind: Literal["movie", "series"] | None = None`.
  - Minden adapter a már meglévő kategóriatérképből tölti ki: bitHUmen, iNSANE, Majomparádé, FileList, IPTorrents a `_CATEGORY_MAP`-ből; HunTorrent a `sorozat/…` kategória-útvonalból; nCore a saját kategóriáiból.
  - A `_find_all_by_query` (alaposztály) a `media_type`-nak nem megfelelő, ismert `content_kind`-ú találatokat kiszűri. Ismeretlen kategóriánál a mai `SeriesParser`-tartalék marad.
- **Elfogadás:** trackerenként szintetikus sorokkal tesztelve, hogy a sorozatkategória a film-katalógusból kiesik és fordítva; az nCore viselkedése változatlan; teljes pytest zöld.

### TP2 — Szabadszavas keresés FileListen és IPTorrentsen

- **Terv:**
  - `_fetch_torrents_by_query` a két adapterben, a meglévő sorfeldolgozóra építve (FileList: a `_fetch_torrents` sorfeldolgozása; IPTorrents: `_parse_torrent_rows`).
  - **FileList:** névkeresés (`searchin` névre) — a paraméterértéket **mintán vagy élőben ellenőrizni kell**.
  - **IPTorrents:** címkeresés (`qf`) — szintén ellenőrizendő. A rate-limit miatt marad a `max_concurrent=1`, lapozás nélkül, legfeljebb 20 találat.
  - IMDb-azonosító a találati listában nincs, ezért a katalógusban nem lesz borítókép. Tartalék a mai „nincs poszter” megjelenítés.
- **Elfogadás:** HTML-mintás (fixture) teszt mindkét trackerre; a TP1 `content_kind`-ja itt is ki van töltve.

### TP3 — Tech Info (valódi hangsáv- és feliratfelismerés) a többi trackeren

- **Most:** csak az `NcoreIndexerDefinition.fetch_tech_info` ad szöveget. Az elemző (`media_attributes/tech_info_parser.py`, `parse_tech_info_metadata(nfo_text, desc_text)`) trackerfüggetlen, a hívó `torrent_streams/service.py` `_enrich_torrent_group`.
- **Terv:** trackerenként `fetch_tech_info`, amely a részletek oldalról kinyeri az NFO-t, MediaInfo-t vagy leírást, és visszaadja a meglévő elemzőnek. Új elemzőlogika nem kell. A meglévő korlátok maradnak: legfeljebb 5 torrent, 1,5 s időkeret, 3600 s-os cache.
- **Feltétel:** a részletek oldalak HTML-szerkezetét nem ismerjük, ehhez **minták kellenek** (6. fejezet).
- **Elfogadás:** trackerenként mintás teszt: a nyelvek (hun/eng/jap) és a feliratjelölés egyezik a minta MediaInfo-jával; hiányzó blokk esetén `(None, None)`, hiba nélkül.

### TP4 — IPTorrents IMDb-pontosság

- **Most:** a sorokhoz vakon a keresett `imdb_id` rendelődik; az üres találatnál a numerikus azonosítóval is keres, ami téves egyezést adhat.
- **Választható megoldások (döntés kell):**
  - **a) Címegyezés-szűrés:** a feloldott cím (már ismert a keresési láncban) normalizálva szerepeljen a torrentnévben. Olcsó, kérés nélkül.
  - **b) Részletek oldal torrentenként:** IMDb-link ellenőrzése. Pontos, de az IPT rate-limitje miatt lassú.

  **Javaslat:** a), és a numerikus azonosítós második keresés csak címegyezéssel együtt fogadjon el találatot.

### TP5 — Kétlépcsős bejelentkezés

Csak ott értelmes, ahol a tracker kínál 2FA-t. **Nyitott kérdés a felhasználónak:** melyik trackeren van 2FA? Ott az nCore-minta (`supports_totp`, a bejelentkezési payload TOTP-mezője) szerint implementálható.

### TP6 — Tesztlefedettség minden adapterre

Fixture-alapú parser-tesztek mind a 7 adapterre, legalább ezekre:
- találati lista (IMDb és szöveg);
- részletek oldal (letöltési link, TP3 esetén Tech Info);
- bejelentkezési hiba felismerése (`_detect_authentication_error`);
- Hit'n'Run oldal.

A fixture-ök anonimizált valódi oldalakból készülnek (6. fejezet). Ahol ilyen nincs, ott szintetikus, és ezt jelölni kell.

### TP7 — Trackerfüggetlen médiafejléc-elemzés (hosszabb távú, opcionális)

- **Ötlet:** a lejátszás indításakor a fájl eleje (és MKV-nál a vége) amúgy is elsőként töltődik. Ebből az MKV EBML Tracks, illetve az MP4 `moov` blokk kiolvasásával pontosan megállapítható a hangsávok és feliratok nyelve.
- **Tárolás:** `info_hash` + fájlindex szerint, a következő listázásoknál felhasználva.
- **Hatókör:** minden trackeren működik, de csak a már megnyitott torrenteknél; az első találati listán nem segít.
- **Döntés:** külön terv és mérés után, a TP3 kiegészítéseként.

## 4. Függőségek és kockázatok

| Téma | Hatás | Kezelés |
|---|---|---|
| **PR #9** (`feature/stability-and-optimization`) módosítja az adapter-alaposztályt: korlátos letöltés (`get_bounded`), cookie-hatókör, letöltési hostszabály | Ütközés a `base_indexer_definition.py`-ban | A megvalósítás a PR #9 beolvasztása után induljon, vagy rebase-eljen rá. |
| **PR #9 26b hostszabály:** az iNSANE és az IPTorrents a letöltési linket az oldal HTML-jéből veszi | Ha élesben más hostról adnak `.torrent`-et, a letöltés hibát ad | Élő ellenőrzés a felhasználó kézi tesztjében; szükség esetén a hostszabály trackerenkénti kivétellistája (külön döntés). |
| **Oldalszerkezet-változás** (HTML scraperek) | Csendes hiba | TP6 fixture-tesztek; a hibák indexerhibaként jelennek meg (meglévő viselkedés). |
| **IPTorrents rate-limit** | Lassú, 429-es hibák | `max_concurrent=1`, nincs lapozás, nincs torrentenkénti részletek-lekérés (TP4 a). |

## 5. Ütemezés és elfogadás

| Fázis | Tartalom | Minta kell? | Elfogadás |
|---|---|---|---|
| F1 | TP1 + TP6 szintetikus alap | Nem | Célzott tesztek → Ruff → basedpyright → teljes pytest zöld; az nCore változatlan. |
| F2 | TP2 + TP4 a) | Paraméter-ellenőrzés (minta vagy élő) | Fixture-tesztek; a katalóguskeresés a FileListen és az IPT-n is ad találatot (kézi teszt). |
| F3 | TP3 + TP6 valódi fixture-ök | **Igen** | Trackerenként helyes nyelvjelölés a mintákon; kézi teszt élőben. |
| F4 | TP5 | 2FA-s trackerlista a felhasználótól | Bejelentkezés TOTP-vel (kézi teszt). |
| (később) | TP7 | Nem | Külön terv. |

## 6. Minták és ellenőrzés

- **Élő tracker** a projekt szabályai szerint csak kifejezett engedéllyel kérdezhető le, a felhasználó fiókjával. Alapértelmezés az offline, mintás munka.
- **Kért minták trackerenként**, a böngészőből „Mentés másként, csak HTML”:
  1. egy találati lista (keresés egy ismert filmre vagy sorozatra);
  2. egy torrent részletek oldala, NFO-val vagy MediaInfo-val;
  3. ha van, a Hit'n'Run / seedkötelezettség oldal.
- **Anonimizálás beküldés előtt:** felhasználónév, felhasználói azonosító, passkey vagy RSS-kulcs (a letöltési linkekben is), e-mail, IP-cím, arány- és statisztikai adatok. Cookie- vagy munkamenetadat ne kerüljön a mintába. A repóba csak anonimizált fixture kerülhet.
- **Végső ellenőrzés:** a felhasználó kézi tesztje élőben, trackerenként (keresés, katalógus, lejátszás, nyelvjelölés).

## 7. Nyitott döntések

1. Mintákat küld-e a felhasználó (TP2 ellenőrzés, TP3, TP6), vagy engedélyez élő, csak olvasó lekérdezést?
2. Melyik trackeren van 2FA (TP5)?
3. IPTorrents: címegyezés-szűrés (javasolt) vagy torrentenkénti részletek-lekérés (TP4)?
4. TP7 (médiafejléc-elemzés) bekerüljön-e a tervbe?
