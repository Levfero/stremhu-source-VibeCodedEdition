$ErrorActionPreference = "Stop"
$PocDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$NetName = "stremhu-poc-isolated-net"
$PeerName = "stremhu-poc-wg-peer"
$PeerJsonHost = Join-Path $PocDir ".tmp_peer_ready.json"

function Invoke-QuietCleanup {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    docker rm -f $PeerName *> $null
    docker network rm $NetName *> $null
    $ErrorActionPreference = $prev
}

try {
    Invoke-QuietCleanup

    docker network create --internal --subnet 172.31.250.0/24 $NetName | Out-Null

    docker run -d --name $PeerName --network $NetName --ip 172.31.250.10 `
        --cap-add NET_ADMIN `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 stremhu-vpn-phase0-poc:local /poc/poc_wg_peer_server.py | Out-Null

    $peerReady = $null
    for ($i = 0; $i -lt 30; $i++) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $logs = (docker logs $PeerName 2>&1) -join "`n"
        $ErrorActionPreference = $prev
        if ($logs -and $logs.Trim().StartsWith("{")) {
            $peerReady = $logs.Trim()
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $peerReady) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $errLogs = (docker logs $PeerName 2>&1) -join "`n"
        $ErrorActionPreference = $prev
        throw "Peer container failed to initialize: $errLogs"
    }
    [System.IO.File]::WriteAllText($PeerJsonHost, $peerReady, (New-Object System.Text.UTF8Encoding($false)))

    docker run --rm --network $NetName --ip 172.31.250.20 `
        --cap-add NET_ADMIN --cap-add SYS_ADMIN `
        -v "${PocDir}:/poc:ro" `
        --entrypoint python3 stremhu-vpn-phase0-poc:local /poc/poc_p0_2_and_p0_3_netns_wg_nft_dns.py /poc/.tmp_peer_ready.json
    $rc = $LASTEXITCODE
    if ($rc -ne 0) {
        exit $rc
    }
} finally {
    if (Test-Path $PeerJsonHost) { Remove-Item -Force $PeerJsonHost }
    Invoke-QuietCleanup
}
