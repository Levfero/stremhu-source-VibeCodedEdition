# Fejlesztői és AI Agent Dokumentáció: `feature/stability-and-optimization`

> [!IMPORTANT]
> **Viszonyítási alap (Baseline):**
> Ez a dokumentáció az eredeti **[s4pp1/stremhu-source](https://github.com/s4pp1/stremhu-source)** (`8c99c7d` commit) és a korábbi M01–M18 stabilitási mérföldkő utáni állapotot rögzíti, és a **`feature/stability-and-optimization`** ág átfogó megerősítési (Hardening), megbízhatósági, párhuzamossági és teljesítmény-optimalizációs architektúráját tartalmazza.

---

## 1. A Branch Célja és Áttekintése

A korábbi mérföldkövek (anime TVDB/Kitsu/MAL támogatás, külső torrent feliratok, multi-manifest végpontok, és a teljes M01–M18 aszinkron stabilitási csomag) után ez a branch a rendszer további mélyreható **megbízhatósági (Reliability), biztonsági (Hardening) és szűk keresztmetszeti (Optimization)** finomhangolását valósítja meg.

A feladatok alapját egy független, szigorú, statikus forráskód-audit képezi (19 konkrét tétellel: `CR01` – `CR19`), amely a következő két részletes dokumentumban található:

- 📊 **[Részletes Audit Jelentés](./stability-and-optimization/audit.md):** A 19 azonosított tétel részletes leírása, bizonyítottsági szintje és kockázati besorolása.
- 🛠️ **[Kódszintű Implementációs Terv](./stability-and-optimization/implementation-plan.md):** Fázisokra bontott, kapukkal (G0–G3) védett konkrét megvalósítási lépések, invariánsok és tesztmátrix.

---

## 2. A 19 Vizsgált Tétel (CR01 – CR19) Áttekintése

| Tétel | Terület | Besorolás | Cél és Megoldás |
| :--- | :--- | :---: | :--- |
| **CR01** | Relay / Libtorrent | P2 | Kapcsolati keret következetessége: az aktív stream alatti 50-es minimum boost és a konfigurált limit összhangba hozása. |
| **CR02** | SQLite / WAL | P2 | WAL állapotmegfigyelés és tranzakcióhatárok ellenőrzése; passzív checkpointing szükségességének vizsgálata. |
| **CR03** | Stremio API | P3 | Stremio JSON válaszok tömörítése (`GZipMiddleware`) méretküszöbbel nagy katalógusok esetén. |
| **CR04** | Kliens / React | P3 | Kliensoldali torrentlista feliratkozások szűkítése, felesleges komponens-újrarajzolások minimalizálása. |
| **CR05** | Biztonság / Auth | P1 | Ismert alapértelmezett session aláírókulcs (`session_secret = "stremhu-source"`) megszüntetése, perzisztált egyedi kulcs generálása. |
| **CR06** | Hálózat / TLS | P1 | Helyi HTTPS egyedi privát kulcs használatának biztosítása, bizalmi modell tisztázása. |
| **CR07** | Naplózás | P2 | Hitelesítő adatok és passkey-ek maszkolása a konzol- és hiba-naplókban (pl. letöltési URL-ek redakciója). |
| **CR08** | Hitelesítés / Pairing | P2 | Login rate limiting és egyszeri eszközpárosítási állapotgép atomi `PENDING -> LINKED` átmenettel. |
| **CR09** | Stream Router | P2 | Lejátszásregiszter (`record_active_playback`) hívásának áthelyezése a sikeres API-kulcs hitelesítés **utánra**. |
| **CR10** | Range & Resolver | P2 | Range fejléc unicode szám-validálás (`isdigit` helyett ASCII) és külső ID formátum-szigorítás. |
| **CR11** | Indexer Relogin | P2 | Újrabejelentkezési Future megszakítás-biztossá tétele: csatlakozó kliens törlése ne okozzon `InvalidStateError`-t a főfolyamatban. |
| **CR12** | Streamcache | P2 | Az `invalidate_stream_cache(user_id)` hívások bekötése a tényleges felhasználói és preferencia módosítási útvonalakba. |
| **CR13** | Event Loop | P2 | Szinkron adatbázis- és DNS műveletek leválasztása az aszinkron event loopról worker szálba. |
| **CR14** | Adatbázis / ORM | P2 | Torrentlista lekérdezés optimalizálása: szükségtelen `resume_bytes` és BLOB oszlopok kizárása a listázásból. |
| **CR15** | Bemenet-védelem | P2 | Külső indexer letöltések és válaszok méretkorlátja és timeout kerete. |
| **CR16** | Munkamenet | P2 | Jelszóváltoztatás utáni session-érvénytelenítés és explicit cookie attribútumok (`Secure`, `SameSite`). |
| **CR17** | Aszinkron Taskok | P2 | Megosztott háttértaskok saját tranzakció-határai és tiszta, rendezett leállás (`shutdown`). |
| **CR18** | Rendszerindítás | P2 | Az első adminisztrátor létrehozásának atomi biztosítása (race condition védelem). |
| **CR19** | HTTP Cache | P2 | Védett végpontok cache-fejléceinek rendezése (`no-store` érzékeny adatokra, privát felirat-fejlécek). |

---

## 3. Implementációs Fázisok és Sorrend

A feladatok a kockázat és a hatás szerint 4 jól elkülönülő fázisban valósulnak meg:

1. **Fázis 1 – Gyors, azonnali javítások (Quick Wins):**
   - **CR09:** `record_active_playback` mozgatása az auth mögé.
   - **CR10:** Range parser integer/unicode hiba kezelése (416-os státusz 500 helyett).
   - **CR12:** Streamcache invalidálás bekötése a user/preferencia módosító routerekbe.
   - **CR11:** Indexer relogin megszakításvédelme.
   - **CR07:** Letöltési URL-ek naplózási redakciója.

2. **Fázis 2 – Biztonsági és hitelesítési megerősítés (Hardening):**
   - **CR05:** Dinamikus, perzisztens session titkos kulcs.
   - **CR08:** Pairing állapotgép és login rate limiting.
   - **CR18:** Első admin atomi létrehozása.
   - **CR16:** Session visszavonási mechanizmus.

3. **Fázis 3 – Adatbázis, szálkezelés és I/O optimalizáció (Backend Performance):**
   - **CR14:** Torrentlista BLOB-mentesítése és eager loading rendezése.
   - **CR13:** Alert/resume szinkron mentések elmozgatása az event loopról.
   - **CR01:** Libtorrent kapcsolati korlátok tisztázása.
   - **CR17:** Háttértaskok DB session izolációja és rendezett leállás.

4. **Fázis 4 – Hálózati és kliensoldali finomhangolás (Networking & Frontend):**
   - **CR03:** Stremio GZip tömörítés (méretküszöbbel).
   - **CR19:** Cache-Control fejlécek optimalizálása.
   - **CR04:** Kliensoldali TanStack Query observer szűkítés.
   - **CR02:** SQLite WAL terheléses validáció.

---

## 4. Tesztelési és Ellenőrzési Referencia

Minden módosítás után kötelezően futtatandó a teljes ellenőrző lánc:

```powershell
$env:HOST_IP="<HOST_IP>"; $env:PYTHONIOENCODING="utf-8"; cd server; .venv\Scripts\python -m ruff check .; .venv\Scripts\basedpyright.exe; .venv\Scripts\python -m pytest tests
```

- **Ruff linter:** 0 hiba
- **Basedpyright:** 0 hiba, 0 figyelmeztetés
- **Pytest:** 259+ teszt hiba nélkül
