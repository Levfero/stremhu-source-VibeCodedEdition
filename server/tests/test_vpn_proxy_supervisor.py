import logging
import sys
import textwrap
from pathlib import Path

import pytest

from app.modules.vpn.proxy_supervisor import (
    ProxyStartError,
    ProxySupervisor,
    redact_output,
    reserve_loopback_port,
)

pytestmark = pytest.mark.anyio

FAKE_KEY = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU="

FAKE_WIREPROXY = textwrap.dedent(
    """
    import re, socket, sys
    config_path = sys.argv[sys.argv.index("-c") + 1]
    text = open(config_path, encoding="utf-8").read()
    port = int(re.search(r"BindAddress = 127\\.0\\.0\\.1:(\\d+)", text).group(1))
    key = re.search(r"PrivateKey = (\\S+)", text).group(1)
    print("peer handshake PrivateKey = " + key, flush=True)
    print("raw " + key, flush=True)
    for second in (56, 58, 59):
        print(f"ERROR: 2026/11/27 18:26:{second} Failed to read ping response from 10.2.0.1: i/o timeout", flush=True)
    if sys.argv[-1] == "exit":
        sys.exit(3)
    import http.server, os, threading
    flag = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "ready.flag")
    class Ready(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if os.path.exists(flag) else 503)
            self.end_headers()
        def log_message(self, *args):
            pass
    health = int(sys.argv[sys.argv.index("-i") + 1].rsplit(":", 1)[1])
    threading.Thread(target=http.server.HTTPServer(("127.0.0.1", health), Ready).serve_forever, daemon=True).start()
    server = socket.socket()
    server.bind(("127.0.0.1", port))
    server.listen()
    while True:
        connection, _ = server.accept()
        connection.close()
    """
)


def _supervisor(tmp_path: Path, mode: str) -> ProxySupervisor:
    script = tmp_path / "fake_wireproxy.py"
    script.write_text(FAKE_WIREPROXY, encoding="utf-8")
    return ProxySupervisor(
        tmp_path / "missing-binary",
        tmp_path / "shm" / "stremhu-vpn",
        ready_timeout=15.0,
        argv_factory=lambda _binary, config, health: [
            sys.executable, str(script), "-c", str(config), "-i", f"127.0.0.1:{health}", mode
        ],
    )


def _config(port: int) -> str:
    return f"[Interface]\nPrivateKey = {FAKE_KEY}\n\n[Socks5]\nBindAddress = 127.0.0.1:{port}\n"


async def test_proxy_becomes_ready_runtime_config_is_deleted_and_output_redacted(tmp_path, caplog):
    (tmp_path / "shm").mkdir()
    supervisor = _supervisor(tmp_path, "serve")
    caplog.set_level(logging.INFO)
    try:
        port = reserve_loopback_port()
        await supervisor.start(_config(port), port)
        assert supervisor.running
        assert list((tmp_path / "shm" / "stremhu-vpn").iterdir()) == []
    finally:
        await supervisor.stop()
    assert not supervisor.running
    assert FAKE_KEY not in caplog.text
    assert "wireproxy:" in caplog.text
    # Az ismétlődő CheckAlive-hiba (eltérő időbélyeggel) csak egyszer kerül a naplóba.
    assert caplog.text.count("Failed to read ping response") == 1


async def test_exiting_proxy_fails_start_and_still_deletes_the_config(tmp_path):
    (tmp_path / "shm").mkdir()
    supervisor = _supervisor(tmp_path, "exit")
    port = reserve_loopback_port()
    with pytest.raises(ProxyStartError) as raised:
        await supervisor.start(_config(port), port)
    assert raised.value.code in {"exited", "not-ready"}
    assert list((tmp_path / "shm" / "stremhu-vpn").iterdir()) == []
    assert not supervisor.running


async def test_missing_binary_and_missing_tmpfs_fail_closed(tmp_path):
    (tmp_path / "shm").mkdir()
    port = reserve_loopback_port()
    no_binary = ProxySupervisor(tmp_path / "missing-binary", tmp_path / "shm" / "stremhu-vpn")
    with pytest.raises(ProxyStartError) as raised:
        await no_binary.start(_config(port), port)
    assert raised.value.code == "binary-missing"
    assert list((tmp_path / "shm" / "stremhu-vpn").iterdir()) == []

    no_tmpfs = ProxySupervisor(tmp_path / "missing-binary", tmp_path / "no-shm" / "stremhu-vpn")
    with pytest.raises(ProxyStartError) as raised:
        await no_tmpfs.start(_config(port), port)
    assert raised.value.code == "runtime-dir-unavailable"


def test_redaction_removes_keys_and_credentials():
    assert FAKE_KEY not in redact_output(f"PrivateKey = {FAKE_KEY}")
    assert FAKE_KEY not in redact_output(f"handshake with {FAKE_KEY} done")
    assert "hunter2" not in redact_output("Password: hunter2")


async def test_readiness_follows_the_health_endpoint(tmp_path):
    (tmp_path / "shm").mkdir()
    supervisor = _supervisor(tmp_path, "serve")
    port = reserve_loopback_port()
    try:
        await supervisor.start(_config(port), port)
        assert await supervisor.readiness() is False
        (tmp_path / "ready.flag").write_text("ok", encoding="utf-8")
        assert await supervisor.readiness() is True
    finally:
        await supervisor.stop()
    assert await supervisor.readiness() is None
