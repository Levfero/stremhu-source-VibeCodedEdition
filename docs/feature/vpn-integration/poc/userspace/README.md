# Userspace (plug & play) VPN PoC

A [`../../userspace-poc-report.md`](../../userspace-poc-report.md) mérési szkriptjei. Csak helyi Dockerben futnak, belső (`--internal`) hálózatokon, internet és élő Proton nélkül.

| Fájl | Szerep |
|---|---|
| `run_userspace_poc.py` | Vezérlő a fejlesztői gépen: hálózatok, kötet, tesztkulcsok, konténerek, forgatókönyvek, eredmény-JSON, takarítás. |
| `us_wg_server.py` | Szimulált Proton-kilépő (tesztinfrastruktúra, egyedül ez kap `NET_ADMIN`-t): kernel WireGuard, NAT, DNS, HTTP tracker, UDP-trackernapló, porttovábbítás. |
| `us_seeder.py` | 256 MiB-os teszttorrent létrehozása és seedelése; a látott peerek naplózása. |
| `us_client.py` | Az alkalmazás oldala **alapértelmezett Docker-jogosultsággal**: wireproxy + libtorrent proxyval, csomagrögzítés, szivárgás-, socket- és erőforrás-elemzés. |
| `results/userspace-poc-results.json` | A 8 forgatókönyv eredménye (2026-11-08). |

**Előfeltételek:**
- a `stremhu-vpn-phase0-poc:rev4` image (lásd: [`../Dockerfile.poc`](../Dockerfile.poc));
- egy ellenőrzött wireproxy bináris (v1.1.3 `linux_amd64`, SHA-256 a jelentésben).

**Futtatás:**

```powershell
$env:POC_WIREPROXY_DIR = "<a wireproxy binárist tartalmazó könyvtár>"
python run_userspace_poc.py                 # mind a 8 forgatókönyv
python run_userspace_poc.py vpn_tcp inbound # csak a megadottak (az eredmény-JSON-ba összefésülve)
```

A tesztkulcsok csak az eldobható kötetben léteznek, és sehol nem íródnak ki. A futás végén minden `vpnpoc-*` objektum törlődik.
