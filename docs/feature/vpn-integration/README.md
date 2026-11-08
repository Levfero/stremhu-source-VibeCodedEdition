# VPN-integráció — dokumentumtérkép

Beépített Proton VPN a StremHU torrentforgalmához. **Csak a torrent le- és feltöltése megy VPN-en; a streamelés, a webes felület és az indexerek nem.**

| Dokumentum | Típus | Státusz |
|---|---|---|
| [`implementation-plan.md`](implementation-plan.md) | Implementációs terv, Rev. 4 (userspace, plug & play) | **Aktuális.** Az F1 (backend mag) indítható; nyitott kapuk: G1–G6. Megvalósítás a PR #9 beolvasztása után ajánlott. |
| [`userspace-poc-report.md`](userspace-poc-report.md) | PoC-mérési jelentés | Lezárva (2026-11-08): 8 forgatókönyv, helyi Docker, élő Proton nélkül. |
| [`review-rev3-findings.md`](review-rev3-findings.md) | A Rev. 3 felülvizsgálata | Lezárva (2026-11-08). |
| [`phase0-evidence-report.md`](phase0-evidence-report.md) | Phase 0 mérési jelentés a Rev. 3-hoz | Történeti (2026-11-04). |
| [`archive/implementation-plan-rev3.md`](archive/implementation-plan-rev3.md) | Előző terv (netns + kernel WireGuard + worker) | **Archivált**, a Rev. 4 váltotta fel. |
| [`poc/userspace/`](poc/userspace/) | A userspace PoC szkriptjei és eredmény-JSON-ja | A Rev. 4 bizonyítéka. |
| [`poc/`](poc/) (többi fájl) | Rev. 3 Phase 0 PoC-k | Történeti. |

**Döntések (felhasználó, 2026-11-08):**
1. A VPN-integráció legyen plug & play: a felhasználó csak elindítja a konténert, és a GUI-ban konfigurálja a VPN-t.
2. Az irány a teljes userspace megoldás (a PoC alapján).
