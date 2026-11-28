# VPN-integráció — A Rev. 3 terv felülvizsgálata (megállapítások)

| Metaadat | Érték |
|---|---|
| **Típus / státusz** | Felülvizsgálati jelentés (audit), lezárva. Statikus forrás- és dokumentumellenőrzés; PoC nem futott hozzá. |
| **Dátum** | 2026-11-08 |
| **Vizsgált anyag** | [`archive/implementation-plan-rev3.md`](archive/implementation-plan-rev3.md) (Rev. 3), [`phase0-evidence-report.md`](phase0-evidence-report.md), a tervezési prompt (a repón kívül) és a `poc/` szkriptjei |
| **Ellenőrzött kód** | `main` `2615596` (azonos a `feature/vpn-integration` alapjával) |
| **Döntések a felülvizsgálat után** | Plug & play követelmény; a userspace irány választása. Lásd a [6. fejezetet](#6-felhasználói-döntések-és-következményük). |

---

## 1. Összegzés

A Rev. 3 terv **alapiránya szakmailag megalapozott, de a dokumentum nem implementálható**:

1. elavult a saját Phase 0 bizonyítékjelentéséhez képest;
2. az internet felé nyitott folyamatnak veszélyes jogosultsági modellt ad;
3. legfontosabb döntését bizonyítatlan, `VERIFIED`-ként jelölt állításra építi;
4. a célplatformon való megvalósíthatósága (P0-7) nem ismert.

Az utólag rögzített **plug & play követelményt** (6. fejezet) a választott „B” architektúra eleve nem teljesítheti, mert indításkor `NET_ADMIN` és `SYS_ADMIN` jogosultságot igényel.

## 2. Ami helyes és megtartandó

| Megállapítás | Ellenőrzés |
|---|---|
| A libtorrent session már az indulási szinkronnál létrejön (`server/app/sync.py` → `create_relay_settings_service` → `get_relay_service()`), még bármilyen VPN-ellenőrzés előtt. | Igazolt a forrásban (`sync.py` 34–35., `relay/dependencies.py`). |
| A `RelayService.__init__` argumentum nélkül hívja a `libtorrent.session()`-t, és csak utána állítja be a `listen_interfaces = 0.0.0.0/[::]:6881`-et. Közben a libtorrent alapértékei (UPnP/NAT-PMP be) érvényesek. | Igazolt (`relay/service.py` 25. és 34–56. sor). |
| A GUI relay-beállításai futás közben bekapcsolhatják az UPnP/NAT-PMP-t és átírhatják a listen portot (`RelayService.update_settings`). | Igazolt (`relay/service.py` 101–131). |
| A main `session_secret` alapértéke nyilvános (`"stremhu-source"`), ezért abból VPN-kulcsot származtatni tilos. | Igazolt (`config.py` 31). |
| A `TorrentStreamsService._sort_torrent_streams` közvetlenül eléri a relay belső `_torrents` szótárát és a libtorrent-handle-t. | Igazolt. |
| A VPN-beállítások a meglévő `settings` kulcs-érték táblában migráció nélkül tárolhatók, ami visszaállításkor is biztonságos (egy új alembic-revízió miatt a régi image nem indulna). | Igazolt (`boot/setup.py` `run_migrations` → `sys.exit(1)`). |
| A SOCKS5 a libtorrent-proxy módban nem ad bejövő TCP-t (porttovábbítás nélkül). | A userspace PoC megerősítette. |
| A Phase 0 kapu, a PoC-k, a negatív szivárgástesztek és a kettős egress-ellenőrzés jó módszertan. | — |

## 3. Problémák

| # | Súly | Megállapítás |
|---|---|---|
| F1 | Magas | **A terv elavult a saját bizonyítékaihoz képest.** A Phase 0 jelentés eredménye 1 PASS, 2 FAIL és 4 NOT RUN. A jelentés 8 kötelező javítást ír elő Rev. 4-hez, ezek egyike sem került be a tervbe (`stremhu-netd`, nftables, NAT-PMP bájtsorrend, uTP-rebind, 503-as pre-priming, `ContiguousPageShmPool`). A `poc_rev4_*.py` szkriptek eredménye nincs dokumentálva. |
| F2 | Magas | **Veszélyes jogosultsági modell.** A Rev. 3-ban az internet felé nyitott FastAPI rootként, tartósan `CAP_SYS_ADMIN` és `CAP_NET_ADMIN` jogosultsággal fut; a jogosultság-eldobás csak a libtorrent-workert védi. Ezt a bizonyítékjelentés 10. szabálya is igazolta. |
| F3 | Magas | **A fő döntés bizonyítatlan állításra épül.** A terv `VERIFIED`-ként állítja, hogy egy többszálú folyamatban nem lehet csak a libtorrent szálait másik network namespace-be tenni, ezért kell külön worker-folyamat. Linuxon azonban a `setns(CLONE_NEWNET)` szálszintű, és a később indított szálak öröklik. Az in-process változatot nem vizsgálták, holott az kiváltaná a worker + SHM + UDS IPC rendszert; épp ez okozta a bizonyítékjelentés streamelési hibáit (4K Remux holtpont, HTTP 206 0 bájttal). |
| F4 | Magas | **A célplatform nem ismert.** A P0-7 (NAS kernelmodulok és `SYS_ADMIN` engedélyezhetősége) nem futott, és az élő Proton-kapuk (P0-3, P0-4) sem. |
| F5 | Közepes | **Elavul a PR #9 után.** A terv a main `2615596`-ot auditálta. A `feature/stability-and-optimization` (PR #9) átírja az életciklust, a relay- és stream-kódot, a `SESSION_SECRET`-et kötelezővé teszi, és új alembic-revíziókat hoz, míg a terv fix `e72a1c3d4f5b` revízióval számol. |
| F6 | Közepes | **Nehezen használható dokumentum.** 2300+ sor, belső ellentmondások (memóriakeretek: 13.2. vs. 24.1/28. fejezet). A `VERIFIED` címke tervezői állításokat is jelöl (pl. „0 use-after-grant race”, „100% adatintegritás”). |

## 4. Plug & play ütközés

A tervezési prompt 12.1. pontja maga rögzíti: a GUI nem adhat a konténernek olyan jogosultságot, amelyet az indításkor nem kapott meg. A Rev. 3 „B” architektúrájához indításkor kell:
- `cap_add: [NET_ADMIN, SYS_ADMIN]`;
- kernel-WireGuard modul (Synologyn gyárilag nincs);
- esetenként `apparmor:unconfined`.

Egy átlagos felhasználó, aki csak elindítja a konténert, ezeket nem állítja be, tehát a „B” nem plug & play.

**Mellékmegállapítás (PR #9):** a `feature/stability-and-optimization` ágon a CR05 kötelezővé teszi a `SESSION_SECRET`-et (legalább 32 karakter, a `"stremhu-source"` tiltott), nélküle az alkalmazás nem indul. Ez a plug & play elvvel szintén ütközik. Javasolt megoldás: ha nincs megadva, az első induláskor generáljon és tároljon egy erős titkot az adatkötetben (`0600`), és csak a környezeti változó írja felül. Ugyanezt a mintát követi az új terv VPN-kulcsa is.

## 5. Mit ellenőriztem és mit nem

- **Ellenőriztem:** a terv kódra vonatkozó állításait a forrásban (`config.py`, `sync.py`, `relay/service.py`, `relay/dependencies.py`, `torrent_streams/service.py`), valamint a bizonyítékjelentés kapu-összesítőjét és javítási listáját.
- **Nem ellenőriztem:** a Rev. 3 hálózati részleteit (nftables-szabályzat, NAT-PMP-csomagformátum) futtatással. Ezek az új irányban nem kellenek.

## 6. Felhasználói döntések és következményük

1. **2026-11-08 — Plug & play:** „mezei user csak felteszi konténerbe a StremHU-t, és ha van VPN-je, akkor a GUI-ba bekonfigurálja.” Következmény: a „B” architektúra elsődleges megoldásként kiesik.
2. **2026-11-08 — Az 1. változat választása:** teljes userspace megoldás PoC-val kezdve. Eredmény: [`userspace-poc-report.md`](userspace-poc-report.md), 8 forgatókönyv. A plug & play működik; DHT és uTP nélkül, porttovábbítás nélkül.
3. **Eredmény:** az új terv a [`implementation-plan.md`](implementation-plan.md) Rev. 4 (userspace, plug & play). A Rev. 3 archiválva, és a kernelszintű „emelt védelem” későbbi opció lehet.
