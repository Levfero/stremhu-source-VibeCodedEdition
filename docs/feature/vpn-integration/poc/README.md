# StremHU VPN Integration — Phase 0 Isolated Proof of Concept (`poc/`)

Ez a könyvtár a [docs/feature/vpn-integration/implementation-plan.md](../implementation-plan.md) (Rev. 3) terv **Phase 0 (`POC ONLY`)** technikai megvalósíthatósági vizsgálatához tartozó, a produkciós moduloktól (`server/`, `client/`, `Dockerfile`, `docker-compose.yml`) 100%-ban elkülönített tesztszkripteket tartalmazza.

## Biztonsági és Izolációs Garanciák

- **0 produkciós fájlmódosítás:** Semmilyen produkciós Python, React, Docker vagy adatbázisfájlt nem érint.
- **0 host / NAS hálózati módosítás:** Minden teszt eldobható (`--rm`) Docker konténerekben fut (`--network none` vagy teljesen elszigetelt `--internal` teszt-bridge hálózaton, `try / finally` automatikus takarítással).
- **`privileged: true` mentes:** Egyetlen teszt sem használ `--privileged` konténert.

## Fájlok

| Fájl | Szerep |
|---|---|
| `Dockerfile.poc` | Eldobható PoC image (`iproute2`, `wireguard-tools`, `nftables`, `natpmpc`, `tcpdump`, `strace`, `stremhu-app` `10000:10000`, `stremhu-worker` `10001:10001`). |
| `poc_p0_1_netns_privdrop_and_helper.py` | **`P0-1` + 10. szabály:** `B-MountFree` (`CLONE_NEWNET` + előre megnyitott `netns_fd` + `setns`), 6-lépéses Worker privilege drop (`UID=10001, Cap*=0, NoNewPrivs=1`), `exit(79)` fail-closed, `PR_SET_PDEATHSIG` és a `CAP_SYS_ADMIN`-mentes privilegizált helper (`stremhu-netd` / `stremhu-netns-ctl` + `UID=10000` FastAPI) PoC. |
| `poc_wg_peer_server.py` | Izolált lokális WireGuard teszt-peer (`10.2.0.1/32`), UDP/TCP `:53` autoritatív DNS szerver, `:6969` BitTorrent HTTP tracker és `:5351` RFC 6886 NAT-PMP responder. |
| `poc_p0_2_and_p0_3_netns_wg_nft_dns.py` | **`P0-2` + lokális `P0-3`:** A 14.1. fejezet `nftables` szabályzatának tesztje (`nft -c -f -` hiba reprodukálása és 3-részes kernel javítás igazolása), Docker `127.0.0.11` DNS DNAT, `libtorrent 2.1.1` tracker announce, `/dev/net/tun`-mentes cross-netns WireGuard kézfogás és negatív szivárgáspróbák (`tcpdump` + `nft` számlálókkal). |
| `run_p0_2_and_p0_3.ps1` | Eldobható `--internal` Docker teszthálózat (`stremhu-poc-isolated-net`) felépítése, futtatása és garantált `finally` takarítása. |
| `poc_p0_4_proton_natpmp.py` | **`P0-4`:** Élő Proton VPN előfeltétel-ellenőrzés (`PROTON_P2P_WG_CONF` hiányában szigorú `NOT RUN`) + offline RFC 6886 / `/usr/bin/natpmpc` bájtszintű csomag-összehasonlítás. |
| `poc_p0_5_libtorrent_port_rebind.py` | **`P0-5`:** `libtorrent 2.1.1.0` élő `listen_interfaces` portváltás (`10.2.0.2:6881` -> `10.2.0.2:54321`) aktív `64 MiB`-os letöltés közben (kimenő és bejövő peer kapcsolattal, SHA-256 ellenőrzéssel). |
| `poc_p0_6_shm_singleflight_inline_and_http.py` | **`P0-6` + 11. szabály:** `64 MiB` `/dev/shm` korlát, `32 MiB` pool, `65s` lassú HTTP olvasó, `1000×` post-arrival `try_retain`, `CancelledError` race, bounded UDS inline fallback (`max_slots=2`), `20 MiB` frame elutasítás, Worker `SIGKILL` + újraindítás, továbbá FastAPI `StreamingResponse` `HTTP 206` vs `HTTP 503` pre-prime teszt és `16 MiB` 4K Remux memória-deadlock teszt. |
| `run_all_poc.ps1` | Összes PoC reprodukálható végrehajtása és automatikus erőforrás-takarítása. |

## Futtatás

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\docs\feature\vpn-integration\poc\run_all_poc.ps1
```
