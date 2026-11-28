---
doc_id: "vpn-beallitas"
title: "Beépített VPN a torrentforgalomhoz"
kind: "operator-guide"
status: "instructions-locally-verified"
audience: ["operator"]
tags: ["vpn", "proton", "wireguard", "wireproxy", "privacy", "relay", "kill-switch", "troubleshooting"]
source_commit: "4cecffb8"
baseline: null
verification: "UI labels, states, error codes and environment variables read from the source at 4cecffb8 (feature/vpn-integration); F5 multiple configs and failover checked on 2026-11-28 with two local PoC WireGuard test exits (migration from the single-config build, failover, stickiness, restart, manual switch, downgrade) through the API and in Chrome; VPN_CHECK_ALIVE parsing tried with the backend config class; the guide's flows run through the API on a throwaway container of the branch image with the PoC kernel-WireGuard test server (2026-11-27: invalid uploads, enable, Test, relay lock, outage to no-handshake and recovery, disable with confirmation, delete; private key absent from the logs) and the same flows clicked through in Chrome on that container (2026-11-27: navigation, file picker, invalid upload toast, save, enable, Test, relay lock texts, outage states, disable and delete dialogs); behaviour measured in the plan's gates: G1 live Proton P2P server (import, enable, handshake, about 66 Mbit/s), G2-G4 and G6 in local air-gapped Docker; the GUI steps were not clicked through in a browser; resource use measured read-only on the user's Ubuntu server (i7-4720HQ, host network mode, real Proton) during a 4K playback (G5, 2026-11-27); Proton steps from protonvpn.com support pages (2026-11-27) and a screenshot of the Proton WireGuard configuration page (2026-11-28: field names, expiry, delete button)"
structure_reviewed: "2026-11-27"
related_docs: ["README.md", "docs/README.md", "docs/atallas-es-visszaallas.md", "docs/feature/vpn-integration/implementation-plan.md"]
source_paths: ["server/app/modules/vpn/", "server/app/modules/relay/network_policy.py", "server/app/modules/stream/service.py", "server/app/config.py", "client/src/routes/_protected/dashboard/system/vpn/"]
---

# Beépített VPN a torrentforgalomhoz

<!-- doc-nav:start -->
**Navigáció:** [Dokumentáció](README.md) · [AI-index](AI-INDEX.md)

> **Státusz:** Felhasználói útmutató. A funkció a `feature/vpn-integration` ágon van, a fork `main` ágába még nem került; amíg ott nincs, a fork kiadott image-ében sem érhető el. A leírt működést a [VPN-terv](feature/vpn-integration/implementation-plan.md#9-fázisok-kapuk-és-elfogadási-feltételek) mérései igazolják: élő Proton P2P szerver (G1), valamint helyi, internet nélküli Docker-tesztek (G2–G4, G6). A felület lépéseit egy eldobható helyi konténeren, teszt-VPN-szerverrel böngészőben (Chrome) végigkattintottuk, és API-n át is kipróbáltuk (2026-11-27): feltöltés, hibás fájl, bekapcsolás, Teszt, relay-zárolás, kapcsolatvesztés és visszaállás, kikapcsolás, törlés. Egy éles Ubuntu-szerveren valódi Proton-kapcsolattal, 4K lejátszás közben is fut, ott az erőforrás-használatot is mértük ([Korlátok](#doc-korlatok)); ARM-gépen nem mértük. A több konfiguráció és az automatikus átállás helyben, két teszt-VPN-szerverrel, API-n át és böngészőben kipróbálva (2026-11-28).

**Kapcsolódó:** [Fő README](../README.md) · [Átállás és visszaállás](atallas-es-visszaallas.md) · [VPN-terv](feature/vpn-integration/implementation-plan.md)

<details>
<summary>Fejezetek / közvetlen ugrás</summary>

- [Röviden](#doc-roviden)
- [Mi kell hozzá?](#doc-mi-kell-hozza)
- [WireGuard-konfiguráció a Protontól](#doc-wireguard-konfiguracio-a-protontol)
- [Beállítás a StremHU-ban](#doc-beallitas-a-stremhu-ban)
- [Mi változik bekapcsolt VPN mellett?](#doc-mi-valtozik-bekapcsolt-vpn-mellett)
- [Állapotok](#doc-allapotok)
- [Több konfiguráció és automatikus átállás](#doc-tobb-konfiguracio-es-automatikus-atallas)
- [Kikapcsolás, csere, törlés](#doc-kikapcsolas-csere-torles)
- [Biztonság és mentés](#doc-biztonsag-es-mentes)
- [Haladó beállítások](#doc-halado-beallitasok)
- [Hibaelhárítás](#doc-hibaelharitas)
- [Visszaállás VPN nélküli változatra](#doc-visszaallas-vpn-nelkuli-valtozatra)
- [Korlátok](#doc-korlatok)

</details>

<!-- doc-nav:end -->

<!-- doc-anchor:doc-roviden -->
<a id="doc-roviden"></a>
## Röviden

- **Csak a torrentforgalom megy a VPN-en:** a letöltés, a feltöltés, a trackerek és a trackernevek feloldása. A lejátszó felé menő stream, a webes felület, az indexerkeresés és a metaadatok a szokásos hálózaton maradnak.
- **Nem kell hozzá Docker-módosítás:** se `cap_add`, se `/dev/net/tun`, se külön VPN-konténer. A Proton WireGuard-konfigurációját a webes felületen töltöd fel, és egy kapcsolóval bekapcsolod.
- **Kill switch:** ha a VPN-kapcsolat megszakad, a torrentforgalom **leáll**, és nem megy tovább a saját IP-címedről. Amikor a kapcsolat visszajön, magától folytatódik.
- **Tartalék szerverek:** több Proton-konfiguráció menthető. Ha az aktív szerver kb. egy percig nem válaszol, a StremHU magától a következőre vált.
- **Ára:** VPN módban nincs bejövő kapcsolat (porttovábbítás), a DHT és az uTP ki van kapcsolva, és a lejátszás indulása a VPN sebességétől is függ.

<!-- doc-anchor:doc-mi-kell-hozza -->
<a id="doc-mi-kell-hozza"></a>
## Mi kell hozzá?

- **Proton VPN-előfizetés, amellyel P2P szerver választható.** A teszt Proton VPN Plus-szal készült. Más szolgáltató WireGuard-konfigurációja is működhet, ha csak a [lent felsorolt mezőket](#doc-hibaelharitas) tartalmazza; ezt nem teszteltük.
- **A StremHU fork VPN-t tartalmazó változata** (lásd a státuszt fent).
- **Admin felhasználó** a StremHU webes felületén.

<!-- doc-anchor:doc-wireguard-konfiguracio-a-protontol -->
<a id="doc-wireguard-konfiguracio-a-protontol"></a>
## WireGuard-konfiguráció a Protontól

1. Jelentkezz be az `account.protonvpn.com` oldalon, és nyisd meg: **Downloads → WireGuard configuration**. Az oldal tetején a korábban generált konfigurációid látszanak, mindegyik a lejárati dátumával (*expires …*) és egy × törlőgombbal.
2. **Device/certificate name:** adj nevet a konfigurációnak (pl. `stremhu`). **Select platform:** a StremHU konténere Linuxon fut, ezért ha a listában van GNU/Linux, azt válaszd.
3. A VPN-beállítások:
   - **NAT-PMP (port forwarding):** nem kell, mert VPN módban a StremHU nem fogad bejövő kapcsolatot.
   - **Moderate NAT:** nem kell.
   - **NetShield:** biztonság kedvéért kapcsold ki, hogy a DNS-szűrés ne akadályozhassa egy tracker nevének feloldását.
   - **VPN Accelerator:** maradhat az alapértéken.
4. Szervernek **P2P szervert** válassz: ezeket a listában kétirányú nyíl jelöli. Egy közeli ország, pl. Magyarország, általában gyorsabb.
5. **Create**, majd **Download**: egy `.conf` fájlt kapsz.

> [!WARNING]
> **Vigyázz a fájlra.** A `.conf` egy privát kulcsot tartalmaz, amellyel bárki a te VPN-fiókodon csatlakozhat. Ne küldd el senkinek, és ne tedd fel nyilvános helyre. Ha kiszivárgott, készíts újat, és a régit töröld a Proton-fiókodban.

> [!IMPORTANT]
> **A Proton-konfiguráció lejár.** A WireGuard-konfigurációk kb. egy évig érvényesek; a dátumot a Proton-oldalon a konfiguráció mellett látod (*expires …*). Lejárat után a VPN nem kapcsolódik (🔴 `no-handshake`), és a torrentforgalom áll. Lejárat előtt generálj újat, töltsd fel, majd a régit töröld a StremHU listájából és a Proton-oldalon (×). Az egyszerre generált tartalék konfigurációk egyszerre járnak le, ezért az automatikus átállás a lejárat ellen nem véd.

> [!TIP]
> **Külön konfigurációt használj a StremHU-hoz.** Ugyanazt a `.conf`-ot ne használd egyszerre a telefonon vagy a gépen is: két eszköz egy kulccsal zavarja egymás kapcsolatát. Tartalék szerverhez (automatikus átálláshoz) mindegyik szerverhez külön konfigurációt generálj.

<!-- doc-anchor:doc-beallitas-a-stremhu-ban -->
<a id="doc-beallitas-a-stremhu-ban"></a>
## Beállítás a StremHU-ban

1. Admin felhasználóval nyisd meg: a fejlécben a felhasználóneved menüje → **Irányítópult** → **Rendszer** fül → **VPN & Adatvédelem** kártya → fogaskerék ikon. Közvetlenül: `<a StremHU címe>/dashboard/system/vpn`.
2. Illeszd be a `.conf` tartalmát az **Új WireGuard-konfiguráció (.conf)** mezőbe, vagy töltsd be a **Fájl kiválasztása** gombbal. Nevet is adhatsz neki (pl. *Budapest #35*); fájlból a fájlnév lesz az alapértelmezés. Ezután: **Konfiguráció mentése**.
   - A StremHU mentés előtt ellenőrzi a fájlt; hibás fájlnál a hibakódot is kiírja ([Hibaelhárítás](#doc-hibaelharitas)).
   - Mentés után a konfiguráció megjelenik a **Mentett konfigurációk** listában. A privát kulcs nem jelenik meg újra; a felület csak a nevet, a végpontot és a címeket mutatja.
   - Több szerverhez ismételd meg a lépést ([Több konfiguráció és automatikus átállás](#doc-tobb-konfiguracio-es-automatikus-atallas)).
3. Kapcsold be: **VPN a torrentforgalomhoz**. Az állapot előbb 🟡 *Kapcsolódás...*, néhány másodperc múlva 🟢 *Kapcsolódva*, a kapcsolódás idejével. Az oldal magától frissül, nem kell újratölteni.
4. Nyomd meg a **Teszt** gombot. Ha minden rendben van, minden sor 🟢:

   | Sor | Mit ellenőriz |
   |---|---|
   | VPN bekapcsolva | A kapcsoló be van kapcsolva. |
   | A torrentforgalom proxyn megy | A torrentmotor a VPN-t használja. |
   | DHT, uTP, UPnP és bejövő port tiltva | Érvényesek a VPN mód torrentbeállításai. Hibánál zárójelben a nem megfelelő beállítás neve áll. |
   | A helyi VPN-proxy fut | A konténeren belüli VPN-folyamat fogad kapcsolatot. |
   | WireGuard-kapcsolat ellenőrzése | A VPN-szerver a tunnelen át válaszol. `no-reply`: nem válaszol, lásd a `no-handshake` hibát. |

5. A **Relay → Beállítások** oldalon a port, az UPnP / NAT-PMP és az uTP mező ezután zárolt, *„VPN által kezelt”* jelzéssel.

<!-- doc-anchor:doc-mi-valtozik-bekapcsolt-vpn-mellett -->
<a id="doc-mi-valtozik-bekapcsolt-vpn-mellett"></a>
## Mi változik bekapcsolt VPN mellett?

- **A futó torrentek is átállnak.** Bekapcsoláskor egy pillanatra megállnak, és a VPN-en folytatódnak. A korábbi közvetlen kapcsolatok a mérésben 2 s-on belül megszűntek. Akik a bekapcsolás előtt kapcsolódtak hozzád, azok a saját IP-címedet már látták.
- **Nincs bejövő kapcsolat.** Csak a StremHU kapcsolódik a többi peerhez, hozzá senki. A letöltés így is megy, de a feltöltés (ratio) lassabban nőhet.
- **A DHT és az uTP ki van kapcsolva**, a relay-beállítások port-, UPnP / NAT-PMP- és uTP-mezője zárolt. A titkosítás és a sebességkorlátok továbbra is állíthatók.
- **A streamelés sebessége a VPN-től is függ.** A lejátszó a szokásos hálózaton kapja az adatot a StremHU-tól, de a StremHU azt a torrentből, a VPN-en át tölti le. Egy magyar P2P szerveren kb. 66 Mbit/s-ot mértünk (egyetlen kapcsolattal, nem torrenttel).
- **Kapcsolatvesztéskor a torrentforgalom áll.** A már letöltött rész lejátszható. Ha a lejátszó még le nem töltött részt kér, a StremHU *„A VPN-kapcsolat nem áll készen”* választ ad (HTTP 503), és a lejátszó hibát jelezhet. Egy már letöltött részről indított lejátszás is megakadhat, amikor a letöltetlen részhez ér. A kapcsolat visszatérése után minden magától folytatódik.
- **Új torrent is csak a VPN-en át tölt le.** Hozzáadni kapcsolatvesztés közben is lehet, de a letöltés csak a VPN visszatérésekor indul.

<!-- doc-anchor:doc-allapotok -->
<a id="doc-allapotok"></a>
## Állapotok

| Állapot a felületen | Jelentése | Torrentforgalom |
|---|---|---|
| ⚪ Kikapcsolva | A VPN ki van kapcsolva. | Közvetlenül, a saját IP-címedről |
| 🟡 Kapcsolódás... | A VPN indul. | Áll |
| 🟢 Kapcsolódva | A VPN-szerver a tunnelen át válaszol. | A VPN-en |
| 🟡 Újrakapcsolódás... | A kapcsolat megszakadt, a StremHU újrapróbálja. | Áll |
| 🔴 Hiba: a torrentforgalom áll | A hibakód alatta látszik ([Hibaelhárítás](#doc-hibaelharitas)). | Áll |

Az **Irányítópult → Rendszer** fül kártyáján röviden: 🟢 *A torrentforgalom VPN-en megy*, 🟡 *VPN bekapcsolva, a kapcsolat nem él: a torrentforgalom áll*, vagy ⚪ *Kikapcsolva*.

Kapcsolati hibánál a StremHU a háttérben tovább próbálkozik, és a kapcsolat visszatérésekor magától 🟢 lesz. Konfigurációs és kulcshibánál nem próbálkozik: ott új `.conf` vagy javított beállítás kell.

<!-- doc-anchor:doc-tobb-konfiguracio-es-automatikus-atallas -->
<a id="doc-tobb-konfiguracio-es-automatikus-atallas"></a>
## Több konfiguráció és automatikus átállás

Ha az aktív Proton-szerver leáll, a StremHU magától átállhat egy másik mentett szerverre.

- **Több konfiguráció:** legfeljebb 10 menthető. Mindegyikhez a Protonnál külön konfigurációt kell generálni, pl. két budapesti és egy bécsi P2P szerverhez.
- **A lista (*Mentett konfigurációk*):**
  - a sorrend az átállás sorrendje, a nyilakkal módosítható;
  - az *Aktív* jelölés mutatja, melyik fut;
  - a **Használat** gombbal kézzel is válthatsz; bekapcsolt VPN mellett ez azonnal átáll, közben néhány másodpercig áll a torrentforgalom;
  - a kuka ikonnal törölhetsz.
- **Automatikus átállás a következő konfigurációra:** alapból bekapcsolt kapcsoló, legalább két konfiguráció kell hozzá.
  - Ha az aktív szerver kb. 60 s-ig nem válaszol, a StremHU a lista következő elemére vált. A lista végéről az elejére ugrik, és ha egyik szerver sem válaszol, sorban tovább próbálja őket.
  - Közben a torrentforgalom áll, közvetlen kapcsolatra soha nem vált.
- **Nincs visszaugrálás:** a StremHU a működő konfiguráción marad akkor is, ha az eredeti szerver közben visszajön, és újraindítás után is. Visszaváltani a **Használat** gombbal lehet.
- **Mit mutat a felület:** az *Aktív konfiguráció: …* és az *Utolsó automatikus átállás: …* sort. A naplóban egy `operation=vpn-failover from=… to=… reason=…` sor jelenik meg (kulcs nélkül).
- **Trackerek:** átálláskor új a VPN kilépő IP-címe, így a trackerek egy másik címet látnak. Ha ez egy trackernél gondot okoz, kapcsold ki az automatikus átállást, és válts kézzel.
- **Ugyanaz a konfiguráció újra feltöltve** nem lesz duplikátum: a listában a helyén frissül.
- **Hibaállapotban** (🔴) egy újonnan hozzáadott konfiguráció azonnal aktív lesz. Működő kapcsolat mellett a hozzáadás nem szakítja meg a kapcsolatot.
- **Helyi próba két teszt-VPN-szerverrel (2026-11-28):**
  - az aktív szerver leállítása után kb. 10 s-mal jött a 🟡, és 34–36 s-mal 🟢 lett a másik szerveren (30 s-os `VPN_FAILOVER_AFTER` beállítással; az alapértelmezett 60 s-mal ez kb. 65 s);
  - az eredeti szerver visszatérése és a konténer újraindítása után is a másikon maradt.

<!-- doc-anchor:doc-kikapcsolas-csere-torles -->
<a id="doc-kikapcsolas-csere-torles"></a>
## Kikapcsolás, csere, törlés

- **Kikapcsolás:** a kapcsolóval. A felület megerősítést kér, mert utána a torrentek a saját IP-címedről mennek.
- **Másik szerver:** add hozzá az új `.conf`-ot, majd a listában nyomd meg mellette a **Használat** gombot. Bekapcsolt VPN mellett a StremHU azonnal átáll rá; közben néhány másodpercig áll a torrentforgalom.
- **Törlés:** a listában a kuka ikonnal, megerősítéssel.
  - Ha az aktív konfigurációt törlöd bekapcsolt VPN mellett, a StremHU a következőre vált.
  - Bekapcsolt VPN mellett az utolsó konfiguráció nem törölhető; ehhez előbb kapcsold ki a VPN-t.

<!-- doc-anchor:doc-biztonsag-es-mentes -->
<a id="doc-biztonsag-es-mentes"></a>
## Biztonság és mentés

- **Titkosított tárolás:** a konfiguráció AES-256-GCM-mel titkosítva kerül az adatbázisba. A kulcs az adatkötetben van (`/app/data/system/vpn.key`), és az első használatkor jön létre. Független a `SESSION_SECRET`-től: annak cseréje nem érinti a VPN-t.
- **Mentés:** az adatbázist és a `vpn.key`-t együtt mentsd. Az [átállási útmutató](atallas-es-visszaallas.md) teljes `/app/data`-mentése mindkettőt tartalmazza.
- **Ha a kulcs elveszik** (vagy a `VPN_SECRET_KEY` megváltozik), a mentett konfiguráció nem fejthető vissza: az állapot 🔴 *Hiba* `decrypt-failed` kóddal, és a torrentforgalom áll. Megoldás: töltsd fel újra a `.conf`-ot. Ugyanazt a fájlt a StremHU a listában a helyén frissíti, és azonnal használja; több konfigurációnál mindegyiket újra kell tölteni.
- **Mi ellen véd:** egy kiszivárgott adatbázisfájl vagy adatbázismentés nem adja ki a privát kulcsot. **Mi ellen nem:** aki a teljes adatkötethez hozzáfér (adatbázis és `vpn.key`), az vissza tudja fejteni.
- **A privát kulcs** nem jelenik meg az API-válaszokban, a naplóban és a felületen. Futás közben a nyílt konfiguráció csak a memóriában (`/dev/shm`) van, és a VPN indulása után törlődik.
- **Tracker-passkey:** HTTPS-trackernél a VPN-szolgáltató csak a tracker nevét látja, az announce-ban lévő passkey-t nem; a tanúsítvány-ellenőrzés a VPN-en át is működik. Sima HTTP-trackernél a VPN-kilépő és a tracker közötti forgalom titkosítatlan, így a VPN-szolgáltató a passkey-t is láthatja. VPN nélkül ugyanez az internetszolgáltatódra igaz.

<!-- doc-anchor:doc-halado-beallitasok -->
<a id="doc-halado-beallitasok"></a>
## Haladó beállítások

Normál használathoz egyiket sem kell megadni.

| Környezeti változó | Mire jó | Formátum |
|---|---|---|
| `VPN_SECRET_KEY` | A titkosítókulcs környezeti változóból, a `vpn.key` helyett. Ha már van `vpn.key`, a mentett konfigurációt a StremHU a következő induláskor az új kulccsal titkosítja újra. | 32 bájt base64-ben, pl. az `openssl rand -base64 32` kimenete |
| `VPN_CHECK_ALIVE` | A kapcsolatellenőrzés ezeket a címeket pingeli a tunnelen át. Alapból a konfiguráció IPv4-es DNS-címei (Protonnál `10.2.0.1`). | JSON-lista, pl. `["10.2.0.1"]` |
| `VPN_CHECK_ALIVE_INTERVAL` | Az ellenőrzés gyakorisága másodpercben. | 1–300, alapból 5 |
| `VPN_FAILOVER_AFTER` | Ennyi másodperc válasz nélkül vált a StremHU a következő mentett konfigurációra. | 20–3600, alapból 60 |

```yaml
services:
  stremhu-source:
    environment:
      - 'VPN_CHECK_ALIVE=["10.2.0.1"]'
```

- A `VPN_CHECK_ALIVE` formátuma szigorú: szögletes zárójel és idézőjel nélkül (pl. `VPN_CHECK_ALIVE=10.2.0.1`) a StremHU el sem indul. Nem IP-cím értéknél az állapot 🔴 `invalid-check-alive`.
- A `VPN_WIREPROXY_PATH` és a `VPN_RUNTIME_DIR` belső beállítás; ne módosítsd.

<!-- doc-anchor:doc-hibaelharitas -->
<a id="doc-hibaelharitas"></a>
## Hibaelhárítás

**Hibakódok az állapot alatt:**

| Hibakód | Mit jelent | Mit tegyél |
|---|---|---|
| `no-handshake` | 30 s óta nem jön válasz a VPN-szerverről a tunnelen át. A helyi próbában a szerver leállítása után a felületen kb. 10 s-mal jelent meg a 🟡 *Újrakapcsolódás...*, kb. 35–40 s-mal ez a hiba, és a szerver visszakapcsolása után kb. 10–15 s-mal újra 🟢. | Ellenőrizd, hogy a konfiguráció nem járt-e le, és nem törölted-e a Proton-fiókban (a Proton-oldalon a konfiguráció mellett: *expires …*). Próbálj másik szervert (új `.conf`). Nézd meg, hogy a hálózatod engedi-e a kimenő UDP-forgalmat a konfiguráció `Endpoint` portjára (Protonnál jellemzően 51820). Ha a VPN valójában működik, csak a DNS-cím nem válaszol pingre, állítsd a `VPN_CHECK_ALIVE`-ot egy válaszoló címre. Több mentett konfigurációval és bekapcsolt automatikus átállással a StremHU kb. 60 s után magától a következőre vált. |
| `decrypt-failed` | A mentett konfiguráció a jelenlegi kulccsal nem fejthető vissza (elveszett `vpn.key` vagy megváltozott `VPN_SECRET_KEY`). | Töltsd fel újra a `.conf`-ot. |
| `missing-config` | A VPN be van kapcsolva, de nincs mentett konfiguráció. | Töltsd fel a `.conf`-ot. |
| `invalid-key` | A `VPN_SECRET_KEY` nem 32 bájtos base64-érték, vagy a `vpn.key` tartalma hibás. | Javítsd vagy töröld a változót, illetve állítsd vissza a `vpn.key`-t mentésből; ha nem megy, töltsd fel újra a `.conf`-ot. |
| `key-file-unreadable`, `key-file-unwritable`, `key-file-corrupt` | A `/app/data/system/vpn.key` nem olvasható, nem írható, vagy üres. | Ellenőrizd az adatkötet jogosultságát. Sérült kulcsfájlt a StremHU nem ír felül: állítsd vissza mentésből, vagy töröld, és töltsd fel újra a `.conf`-ot. |
| `unknown-envelope`, `corrupt-envelope` | A mentett konfiguráció sérült az adatbázisban. | Töltsd fel újra a `.conf`-ot. |
| `binary-missing` | Az image-ben nincs VPN-komponens (wireproxy). | VPN-t tartalmazó image-et használj. |
| `runtime-dir-unavailable`, `runtime-dir-unwritable` | A `/dev/shm` nem érhető el vagy nem írható a konténerben. | Dockerben alapból elérhető; ne kapcsold ki (pl. `ipc: none`). |
| `exited`, `not-ready`, `spawn-failed` | A VPN-folyamat nem indult el, vagy indulás után kilépett. 5 sikertelen próba után jelenik meg; a StremHU ezután is újrapróbálja, legfeljebb percenként. | Nézd meg a konténer naplóját: az `operation=vpn-proxy-start` és a `wireproxy:` sorokat (kulcsot nem tartalmaznak). Gyakori ok a hibás vagy szokatlan konfiguráció: tölts le újat. |
| `invalid-check-alive` | A `VPN_CHECK_ALIVE` nem IP-címet tartalmaz. | Javítsd a változót. |
| `settings-unavailable` | Induláskor a beállítások nem voltak olvashatók az adatbázisból. Ilyenkor egy torrent sem indul. | Nézd meg a naplót, és indítsd újra a konténert. |
| `already-running` | Belső állapothiba. | Indítsd újra a konténert. |

**Hibák a konfiguráció mentésekor** (*„Érvénytelen WireGuard-konfiguráció (…)”*). A StremHU ezeket a mezőket fogadja el: az `[Interface]` szakaszban `PrivateKey`, `Address`, `DNS`, `MTU`; a `[Peer]` szakaszban `PublicKey`, `PresharedKey`, `Endpoint`, `AllowedIPs`, `PersistentKeepalive`. A Proton letöltött fájlja ennek megfelel.

| Hibakód | Mit jelent | Mit tegyél |
|---|---|---|
| `unsupported-key` | A fájlban `PostUp`, `PostDown`, `PreUp`, `PreDown`, `Table`, `SaveConfig`, `FwMark` vagy `ListenPort` van. Ezek parancsot futtatnának vagy útvonalat állítanának, ezért a StremHU nem fogadja el őket. | Töröld ezeket a sorokat, vagy használd a Proton eredeti fájlját. |
| `missing-section`, `missing-key` | Hiányzik az `[Interface]` vagy a `[Peer]` szakasz, illetve a `PrivateKey`, `Address`, `PublicKey`, `Endpoint` vagy `AllowedIPs` mező. | Töltsd le újra a fájlt. |
| `unknown-key`, `unknown-section`, `duplicate-key`, `duplicate-section`, `multiple-peers` | Nem szabványos mező, ismétlődő rész, vagy több szerver egy fájlban. | Egy `[Interface]` és egy `[Peer]` szakasz kell. |
| `invalid-key`, `invalid-endpoint`, `invalid-address`, `invalid-number` | Hibás érték valamelyik mezőben. | Töltsd le újra a fájlt. |
| `too-large`, `invalid-characters`, `malformed-line` | Nem WireGuard-konfiguráció, vagy sérült (16 KiB a felső korlát). | A Proton letöltött `.conf`-ját használd. |

**Egyéb:**

- **A kapcsoló nem kapcsolható be:** még nincs mentett konfiguráció.
- **„Legfeljebb 10 VPN-konfiguráció menthető.”:** törölj egy régit.
- **„Bekapcsolt VPN mellett az utolsó konfiguráció nem törölhető.”:** előbb kapcsold ki a VPN-t.
- **Mikor váltott a StremHU másik szerverre?** A felületen az *Utolsó automatikus átállás* sor mutatja, a naplóban az `operation=vpn-failover` sor.
- **A relay-beállítás mentése hibát ad:** VPN módban a port, az UPnP / NAT-PMP és az uTP nem állítható.
- **Lassú letöltés vagy lejátszás:** próbálj közelebbi vagy kevésbé terhelt P2P szervert. VPN módban kevesebb peer érhető el (nincs bejövő kapcsolat és DHT).

<!-- doc-anchor:doc-visszaallas-vpn-nelkuli-valtozatra -->
<a id="doc-visszaallas-vpn-nelkuli-valtozatra"></a>
## Visszaállás VPN nélküli változatra

- Az eredeti StremHU-ban és a fork VPN nélküli változataiban nincs VPN. Visszaváltás után a torrentforgalom **a saját IP-címedről** megy; ezt vedd figyelembe, mielőtt váltasz.
- A VPN-hez nem tartozik adatbázis-migráció: a beállítás egyetlen sor a beállítások között. A VPN-nel együtt végzett visszaállást nem teszteltük; az általános lépések az [átállási útmutatóban](atallas-es-visszaallas.md#doc-visszaallas-az-eredetire) vannak.
- **Visszalépés egy korábbi, egykonfigos VPN-buildre:** az csak az aktív konfigurációt látja, és induláskor a saját formájában menti el a beállítást. A többi mentett konfiguráció ezért elvész, azokat később újra fel kell tölteni. Ezt helyben kipróbáltuk: a korábbi build az aktív konfigurációval kapcsolódott.

<!-- doc-anchor:doc-korlatok -->
<a id="doc-korlatok"></a>
## Korlátok

- **Mért:** élő Proton P2P szerver (handshake, DNS a tunnelen, kb. 66 Mbit/s), szivárgásmentesség a torrentforgalomban (letöltés, HTTP-, HTTPS- és UDP-tracker, magnet, web seed), a kill switch és a futás közbeni be- és kikapcsolás. A részletek a [VPN-terv 9. fejezetében](feature/vpn-integration/implementation-plan.md#9-fázisok-kapuk-és-elfogadási-feltételek) vannak.
- **Erőforrásigény (egy éles szerveren mérve):** Intel i7-4720HQ (4 mag / 8 szál) Ubuntu-szerveren, 4K lejátszás közben kb. 75–80 Mbit/s ment a VPN-en. A VPN-folyamat kb. egy CPU-magot és kb. 115 MiB memóriát használt, a gép a 8 szálból kb. egyet. Gyengébb, 2–4 magos ARM NAS-on ez szűk lehet, ott a VPN-sebesség alacsonyabb lehet.
- **Hálózati mód:** bridge és host (`network_mode: host`) módban is kipróbálva.
- **Nem mért:** normál (nem P2P) Proton-szerver, ARM-gép, hosszú távú stabilitás.
- **IPv6:** a Proton-konfiguráció IPv6-címet is tartalmaz. A kapcsolatellenőrzés mégis csak IPv4-címet pingel, mert a Proton IPv6-os DNS-címe a mérésben nem válaszolt pingre.
- **A védelem alkalmazásszintű:** a StremHU a torrentmotort kényszeríti a VPN-re, nem a konténer teljes hálózatát. A konténer többi forgalma (stream, webes felület, indexerek) szándékosan nem megy a VPN-en.
