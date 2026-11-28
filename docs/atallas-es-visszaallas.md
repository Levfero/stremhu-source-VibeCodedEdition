---
doc_id: "atallas-es-visszaallas"
title: "Átállás az eredeti StremHU-ról és vissza"
kind: "operator-guide"
status: "instructions-locally-verified"
audience: ["operator"]
tags: ["installation", "docker", "migration", "backup", "restore", "alembic", "session-secret"]
source_commit: "afca1e6d"
baseline: "s4pp1/stremhu-source@f3ffff3"
verification: "local Docker, throwaway volume, --network none, 2026-11-24: s4pp1/stremhu-source:latest (db head c30e6d9dd16d) and a fork image with the migrations of afca1e6d (db head a7c41e9b2d10); upstream boot, backup with busybox tar, fork migration, upstream exit 1 on the fork database, restore, alembic downgrade; no real tracker data"
structure_reviewed: "2026-11-24"
related_docs: ["README.md", "docs/README.md"]
source_paths: ["server/alembic/versions/", "server/app/config.py", "server/app/boot/setup.py"]
---

# Átállás az eredeti StremHU-ról és vissza

<!-- doc-nav:start -->
**Navigáció:** [Dokumentáció](README.md) · [AI-index](AI-INDEX.md)

> **Státusz:** Felhasználói útmutató. A mentés, a visszatöltés és az adatbázis-visszaállítás parancsait helyi Dockerben, eldobható nevesített kötettel kipróbáltuk (2026-11-24). A mappás (`./data`) változat ugyanez a parancs más `-v` forrással; azt külön nem futtattuk. Valódi trackerrel és valódi torrentlistával nem futott.

**Kapcsolódó:** [Fő README](../README.md) · [Dokumentáció](README.md)

<details>
<summary>Fejezetek / közvetlen ugrás</summary>

- [Röviden](#doc-roviden)
- [Mi marad meg, és mi változik?](#doc-mi-marad-meg-es-mi-valtozik)
- [Átállás az eredetiről a forkra](#doc-atallas-az-eredetirol-a-forkra)
- [Visszaállás az eredetire](#doc-visszaallas-az-eredetire)
- [Lejátszási linkek és SESSION_SECRET](#doc-lejatszasi-linkek-es-session-secret)
- [Hibaelhárítás](#doc-hibaelharitas)

</details>

<!-- doc-nav:end -->

<!-- doc-anchor:doc-roviden -->
<a id="doc-roviden"></a>
## Röviden

- **Átállás:** a `docker-compose.yml`-ben csak az image sort kell cserélni. Minden adat megmarad.
- **Visszaállás:** az image sor visszaírása **önmagában nem elég**. A fork pár új oszlopot ad az adatbázishoz, és ezt az eredeti nem ismeri: visszaváltás után el sem indul, és a naplóba sem ír hibát. Ezért visszaállás előtt az adatbázist is vissza kell állítani: mentésből vagy egy paranccsal.
- **Átállás előtt készíts mentést.** Ez a legegyszerűbb biztosíték.

<!-- doc-anchor:doc-mi-marad-meg-es-mi-valtozik -->
<a id="doc-mi-marad-meg-es-mi-valtozik"></a>
## Mi marad meg, és mi változik?

| | Átállás a forkra | Visszaállás mentésből | Visszaállás paranccsal (downgrade) |
|---|---|---|---|
| Felhasználók, indexerek, beállítások | Megmaradnak | A mentés állapota tér vissza | Megmaradnak |
| Torrentlista (seedelt torrentek) | Megmarad | A mentés állapota; a fork alatt hozzáadottak kiesnek | Megmarad |
| Letöltött fájlok (`downloads`) | Megmaradnak | Megmaradnak; a fork alatt letöltöttek gazdátlanok lesznek | Megmaradnak |
| A fork saját kapcsolói (pl. letöltve-jelölés, feliratkölcsönzés) | Megjelennek | Elvesznek | Elvesznek |
| Bejelentkezés a webes felületre | Egyszer újra kell | Egyszer újra kell | Egyszer újra kell |
| Mentett „Folytatás” linkek (Nuvio, Stremio) | Egyszer 400-as hibát adnak | Egyszer 400-as hibát adnak | Egyszer 400-as hibát adnak |

A bejelentkezés és a „Folytatás” linkek oka a [következő fejezetben](#doc-lejatszasi-linkek-es-session-secret) van. A javítás egyszerű: nyisd meg újra a filmet, és válaszd ki a forrást a listából.

<!-- doc-anchor:doc-atallas-az-eredetirol-a-forkra -->
<a id="doc-atallas-az-eredetirol-a-forkra"></a>
## Átállás az eredetiről a forkra

**1. Nézd meg, hol vannak az adataid.** A `docker-compose.yml` `volumes` részében a `/app/data` előtti rész a lényeg:

- **Nevesített kötet**, pl. `data:/app/data`: a kötet pontos nevét így látod:
  ```
  docker volume ls
  ```
  A compose néha a mappa nevét is elé teszi (pl. `stremhu_data`). Ha a compose-ban `name:` is szerepel (pl. `name: stremhu-source-data`), akkor az a név.
- **Mappa**, pl. `./data:/app/data`: az adatok a compose melletti `data` mappában vannak.

**2. Mentés** (a compose mappájában). A `downloads` mappa kimarad, mert az csak letöltött torrentadat.

Nevesített kötetnél (a `stremhu-source-data` helyére a saját köteted neve kell):
```
docker compose down
docker run --rm -v stremhu-source-data:/data -v "$(pwd)":/backup alpine tar czf /backup/stremhu-mentes.tgz --exclude=./downloads -C /data .
```

Mappánál (`./data:/app/data`):
```
docker compose down
docker run --rm -v "$(pwd)/data":/data -v "$(pwd)":/backup alpine tar czf /backup/stremhu-mentes.tgz --exclude=./downloads -C /data .
```

Windows PowerShellben `"$(pwd)"` helyett `"${PWD}"` kell. A mentés (`stremhu-mentes.tgz`) a compose mellé kerül; tartsd meg, amíg ki nem próbáltad a forkot.

**3. Cseréld az image sort** a `docker-compose.yml`-ben:
```yaml
image: levfero/stremhu-source:dev
```
Minden más maradhat. Egy megkötés van: ha a compose-ban `SESSION_SECRET` szerepel, legalább 32 karakter legyen, különben a fork nem indul el. Ha nincs megadva, a fork első induláskor magának generál egyet, és az adatkötetben tárolja.

**4. Indítás:**
```
docker compose pull
docker compose up -d
```
Az első induláskor a fork frissíti az adatbázist. Utána egyszer újra be kell lépni a webes felületre.

<!-- doc-anchor:doc-visszaallas-az-eredetire -->
<a id="doc-visszaallas-az-eredetire"></a>
## Visszaállás az eredetire

Két út van. Mindkettőnél előbb az adatbázist kell visszaállítani, és **csak utána** szabad az image sort visszaírni.

> [!NOTE]
> Ha a fork [beépített VPN-jét](vpn-beallitas.md) használtad: az eredetiben nincs VPN, ezért visszaváltás után a torrentforgalom a saját IP-címedről megy.

### A) Mentésből (a mentés pillanatára áll vissza)

1. Írd vissza az image sort:
   ```yaml
   image: s4pp1/stremhu-source:latest
   ```
2. Töltsd vissza a mentést, és indítsd el. Ez a `downloads` kivételével mindent töröl a kötetből, és a mentés tartalmát teszi a helyére.

   Nevesített kötetnél:
   ```
   docker compose down
   docker run --rm -v stremhu-source-data:/data -v "$(pwd)":/backup alpine sh -c "find /data -mindepth 1 -maxdepth 1 ! -name downloads -exec rm -rf {} + && tar xzf /backup/stremhu-mentes.tgz -C /data"
   docker compose up -d
   ```
   Mappánál ugyanez, csak a `-v stremhu-source-data:/data` helyett `-v "$(pwd)/data":/data` kell.

A fork alatt hozzáadott torrentek és beállítások ezzel elvesznek. Az akkor letöltött fájlok a `downloads` mappában maradnak, ezeket kézzel törölheted.

### B) Paranccsal, mentés nélkül (minden adat megmarad)

1. Az image sor **még a forké** legyen (`levfero/stremhu-source:dev`), mert csak a fork tudja visszaállítani a saját változtatásait.
2. Állítsd le, és futtasd a visszaállítást (a `stremhu-source` a compose-beli szolgáltatás neve):
   ```
   docker compose down
   docker compose run --rm stremhu-source alembic downgrade c30e6d9dd16d
   ```
3. Írd vissza az image sort `s4pp1/stremhu-source:latest`-re, és indítsd:
   ```
   docker compose up -d
   ```

A `c30e6d9dd16d` az eredeti StremHU jelenlegi (`f3ffff3`) adatbázis-verziója. Ha az eredeti később új adatbázis-változást kap, ez az azonosító is változhat; ezt itt frissítjük.

<!-- doc-anchor:doc-lejatszasi-linkek-es-session-secret -->
<a id="doc-lejatszasi-linkek-es-session-secret"></a>
## Lejátszási linkek és SESSION_SECRET

A lejátszási linkeket és a webes bejelentkezést a szerver egy kulccsal (`SESSION_SECRET`) írja alá:

- **Az eredeti**, ha nincs megadva, egy fix, nyilvános kulcsot használ (`stremhu-source`).
- **A fork** ezt biztonsági okból soha nem használja. Ha nincs megadva, első induláskor erős kulcsot generál, és a `/app/data/system/session_secret` fájlba menti. Minden további induláskor ezt olvassa vissza, amíg az adatkötet megvan.

Ezért váltáskor (mindkét irányban) a kulcs megváltozik, és a régi linkek érvénytelenek lesznek. Ilyenkor a Nuvio vagy a Stremio „Folytatás” gombja egyszer **HTTP 400** hibát ad. Ez nem hiba: nyisd meg újra a filmet, és válaszd ki a forrást a listából. Onnantól a folytatás minden eszközön működik.

**Ha gyakran váltogatnál**, adj meg mindkét compose-ban ugyanazt a legalább 32 karakteres `SESSION_SECRET`-et. Így a linkek és a bejelentkezés túlélik a váltást. Az adatbázist visszafelé ettől függetlenül vissza kell állítani (lásd fent).

Erős kulcs generálása (64 karakteres hexa kód, nincs benne olyan karakter, ami a compose-t megzavarná):

Linux, NAS, macOS:
```
openssl rand -hex 32
```

Windows PowerShell:
```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
```

Beírás a compose-ba (idézőjel nélkül; utána ne változtasd):
```yaml
    environment:
      - SESSION_SECRET=<ide a generált kulcs>
```

A kulcsot ne oszd meg, és a compose-t se, ha benne van.

<!-- doc-anchor:doc-hibaelharitas -->
<a id="doc-hibaelharitas"></a>
## Hibaelhárítás

| Tünet | Ok | Teendő |
|---|---|---|
| Visszaváltás után az eredeti nem indul el, a naplóban nincs hibaüzenet | Az adatbázis még a fork verzióján áll | [Visszaállás](#doc-visszaallas-az-eredetire) mentésből vagy paranccsal |
| A fork nem indul: „A SESSION_SECRET legalább 32 karakteres…” | A compose-ban túl rövid kulcs van | Hosszabb kulcs, vagy töröld a sort (akkor generál egyet) |
| A „Folytatás” HTTP 400-as hibát ad | Váltás vagy kulcscsere után a régi link érvénytelen | Nyisd meg újra a filmet, válaszd ki a forrást a listából |
| Minden indulás után újra be kell lépni | A `/app/data` nincs kötetként megtartva, így a kulcs minden alkalommal új | Tedd a `/app/data`-t kötetre (`data:/app/data` vagy `./data:/app/data`) |

**Soha ne futtasd** a `docker compose down -v` parancsot: a `-v` törli a kötetet, és vele az adatbázist, a beállításokat és a kulcsot. Leállításhoz elég a `docker compose down`.
