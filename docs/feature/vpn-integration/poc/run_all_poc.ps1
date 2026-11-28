<#
.SYNOPSIS
Reproducible Phase 0 PoC Runner for StremHU VPN Integration (POC ONLY — Zero Production Code Changes).

.DESCRIPTION
Builds the disposable `stremhu-vpn-phase0-poc:local` image (derived from `python:3.12-slim-bookworm`
or local `stremhu-source:repo-master` without touching `Dockerfile` or `server/`), executes all
isolated Phase 0 PoC harnesses (`P0-1` + Rule 10, `P0-2` + Local `P0-3`, `P0-4` preflight/codec,
`P0-5`, `P0-6` + Rule 11, and `P0-7` read-only kernel capability audit), and guarantees automatic
cleanup of temporary containers, networks, and optional image teardown in `finally`.

NEVER uses `--privileged` (`privileged: true`) and NEVER touches host/NAS networking.
#>
param(
    [switch]$RemovePocImageOnExit
)

$ErrorActionPreference = "Stop"
$PocDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $PocDir "..\..\..\..")
$ImageTag = "stremhu-vpn-phase0-poc:local"

try {
    Write-Host "=== [0/6] Building disposable PoC image ($ImageTag) ==="
    docker build -f (Join-Path $PocDir "Dockerfile.poc") -t $ImageTag $PocDir

    Write-Host "`n=== [1/6] Running P0-1 + Rule 10 (B-MountFree Netns, 6-Step PrivDrop, Zero-CAP_SYS_ADMIN Helper) ==="
    docker run --rm --network none --cap-add NET_ADMIN --cap-add SYS_ADMIN `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 $ImageTag /poc/poc_p0_1_netns_privdrop_and_helper.py

    Write-Host "`n=== [2/6] Running P0-2 + Local P0-3 (Section 14.1 nftables, 127.0.0.11 DNS DNAT, Cross-Netns WG) ==="
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PocDir "run_p0_2_and_p0_3.ps1")

    Write-Host "`n=== [3/6] Running P0-4 (Proton NAT-PMP Live Preflight & RFC 6886 / natpmpc Wire Equivalence) ==="
    docker run --rm --network none `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 $ImageTag /poc/poc_p0_4_proton_natpmp.py

    Write-Host "`n=== [4/6] Running P0-5 (Libtorrent 2.1.1 Live listen_interfaces Port-Rebind PoC) ==="
    docker run --rm --network none --cap-add NET_ADMIN --cap-add SYS_ADMIN `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 $ImageTag /poc/poc_p0_5_libtorrent_port_rebind.py

    Write-Host "`n=== [5/6] Running P0-6 + Rule 11 (64 MiB /dev/shm, 65s Slow Reader, SIGKILL, HTTP 503 & Remux Limits) ==="
    docker run --rm --network none --shm-size=64m `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 $ImageTag /poc/poc_p0_6_shm_singleflight_inline_and_http.py

    Write-Host "`n=== [6/6] Running P0-7 Read-Only Kernel Config Audit ==="
    docker run --rm --network none `
        --entrypoint python3 $ImageTag -c "import gzip, os; cfg = gzip.open('/proc/config.gz','rt').read() if os.path.exists('/proc/config.gz') else ''; keys = ['CONFIG_NET_NS','CONFIG_USER_NS','CONFIG_WIREGUARD','CONFIG_NF_TABLES','CONFIG_NF_TABLES_INET','CONFIG_NFT_NAT','CONFIG_NFT_MASQ','CONFIG_NFT_CT','CONFIG_SECCOMP','CONFIG_SECURITY_APPARMOR']; print('\n'.join(l for l in cfg.splitlines() if any(l.startswith(k+'=') for k in keys)))"
} finally {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    docker rm -f stremhu-poc-wg-peer *> $null
    docker network rm stremhu-poc-isolated-net *> $null
    if ($RemovePocImageOnExit) {
        docker rmi $ImageTag *> $null
    }
    $ErrorActionPreference = $prev
}
