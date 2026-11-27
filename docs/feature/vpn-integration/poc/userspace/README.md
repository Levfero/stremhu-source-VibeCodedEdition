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

**Előfeltételek:**
- a `stremhu-vpn-phase0-poc:rev4` image (lásd: [`../Dockerfile.poc`](../Dockerfile.poc));
- egy ellenőrzött wireproxy bináris (v1.1.3 `linux_amd64`, SHA-256 a jelentésben).

**Futtatás:**

```powershell
$env:POC_WIREPROXY_DIR = "<a wireproxy binárist tartalmazó könyvtár>"
$env:POC_RESULTS_NAME = "sajat-meres-results.json"  # nélküle a lezárt userspace-poc-results.json íródik felül
python run_userspace_poc.py                 # mind a 16 forgatókönyv
python run_userspace_poc.py vpn_tcp inbound # csak a megadottak (a meglévő eredmény-JSON-ba összefésülve)
```

A tesztkulcsok, a teszt-CA és a tracker-tanúsítvány csak az eldobható kötetben léteznek, és sehol nem íródnak ki. A futás végén minden `vpnpoc-*` objektum törlődik.
