# Userspace (plug & play) VPN PoC

A [`../../userspace-poc-report.md`](../../userspace-poc-report.md) mérési szkriptjei, valamint a terv Rev. 5 G2, G3 és G4 kapujának mérései ([`../../implementation-plan.md`](../../implementation-plan.md), 9. fejezet). Csak helyi Dockerben futnak, belső (`--internal`) hálózatokon, internet és élő Proton nélkül.

| Fájl | Szerep |
|---|---|
| `run_userspace_poc.py` | Vezérlő a fejlesztői gépen: hálózatok, kötet, tesztkulcsok, teszt-CA, konténerek, forgatókönyvek, eredmény-JSON, takarítás. |
| `us_wg_server.py` | Szimulált Proton-kilépő (tesztinfrastruktúra, egyedül ez kap `NET_ADMIN`-t): kernel WireGuard, NAT, DNS, HTTP- és HTTPS-tracker (SNI-naplóval), UDP-trackernapló, porttovábbítás. |
| `us_seeder.py` | 256 MiB-os teszttorrent létrehozása és seedelése, ugyanez HTTP web seedként (Range); a látott peerek és HTTP-kérések naplózása. |
| `us_client.py` | Az alkalmazás oldala **alapértelmezett Docker-jogosultsággal**: wireproxy + libtorrent proxyval, csomagrögzítés, szivárgás-, socket- és erőforrás-elemzés. |
| `results/userspace-poc-results.json` | Az eredeti 8 forgatókönyv eredménye (2026-11-08, lezárt). |
| `results/g2-product-pack-results.json` | G2: a termék `vpn_settings()` csomagja (2026-11-27). |
| `results/g3-runtime-switch-results.json` | G3: futás közbeni váltás aktív letöltés közben (2026-11-27). |
| `results/g4-magnet-webseed-https-results.json` | G4: magnet-metaadat, web seed, HTTPS-tracker és negatív tanúsítvány-kontroll (2026-11-27). |
| `results/lt213-leak-suite-run1-results.json` | Mind a 16 forgatókönyv libtorrent 2.1.3-mal, a 2.1.3-as termékimage-re épített PoC-image-en, az addigi, azonnali hash-sel (2026-11-28, 1. futás). |
| `results/lt213-leak-suite-results.json` | Ugyanez a fájl-hash-t `flush_cache` után számolva (2026-11-28, 2. futás). |
| `results/lt213-g2-g3-isolated-results.json` | A G2 és a G3 négy forgatókönyve külön futtatva, mint eredetileg (2026-11-28). |

**Előfeltételek:**
- a `stremhu-vpn-phase0-poc:rev4` image (lásd: [`../Dockerfile.poc`](../Dockerfile.poc)), vagy a `POC_IMAGE` változóban megadott másik PoC-image;
- egy ellenőrzött wireproxy bináris (v1.1.3 `linux_amd64`, SHA-256 a jelentésben).

**Futtatás:**

```powershell
$env:POC_WIREPROXY_DIR = "<a wireproxy binárist tartalmazó könyvtár>"
$env:POC_RESULTS_NAME = "sajat-meres-results.json"  # nélküle a lezárt userspace-poc-results.json íródik felül
$env:POC_IMAGE = "stremhu-vpn-poc:lt213"            # opcionális; alapból stremhu-vpn-phase0-poc:rev4
python run_userspace_poc.py                 # mind a 16 forgatókönyv
python run_userspace_poc.py vpn_tcp inbound # csak a megadottak (a meglévő eredmény-JSON-ba összefésülve)
```

A tesztkulcsok, a teszt-CA és a tracker-tanúsítvány csak az eldobható kötetben léteznek, és sehol nem íródnak ki. A futás végén minden `vpnpoc-*` objektum törlődik.

**Libtorrent-frissítés utáni újrafuttatás:** a `Dockerfile.poc` alapimage-e BuildKit névvel ellátott kontextussal cserélhető, a fájl módosítása nélkül:

```powershell
docker build --pull -t stremhu-source:lt213-local .   # a repó gyökeréből, az új libtorrenttel
docker build -f docs/feature/vpn-integration/poc/Dockerfile.poc `
  --build-context stremhu-source:repo-master=docker-image://stremhu-source:lt213-local `
  -t stremhu-vpn-poc:lt213 docs/feature/vpn-integration/poc
```

A wireproxy bináris ugyanebből az image-ből másolható ki (`/usr/local/bin/wireproxy`); a SHA-256-ja a `Dockerfile` `WIREPROXY_BINARY_SHA256_AMD64` értéke.

**Fájl-hash (2026-11-28 óta):** a `us_client.py` a letöltött fájlt az `is_seeding` után kétszer hash-eli: azonnal (`sha256_ok_before_flush`, csak tájékoztató), majd `flush_cache` és a `cache_flushed_alert` után (`sha256_ok`, mérvadó). A libtorrent 2.1.3 pread lemez-I/O-ja a seedelés kezdetén még tarthat ki nem írt blokkot; a termék ezt nem érinti, mert a streamelt adatot a `read_piece`-szel, a libtorrent lemezrétegén át olvassa.
