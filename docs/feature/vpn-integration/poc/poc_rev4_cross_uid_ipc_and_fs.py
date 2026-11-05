#!/usr/bin/env python3
"""Phase 0 Rev. 4 PoC (Tasks 2 & 3):
Non-root Multi-UID Architecture Compatibility & Security Boundary Verification:
  - PID 1 bootstrap (t=0) -> drops ALL capabilities after spawning helpers & FastAPI
  - stremhu-netd (init_net, UID=0, CapEff=CapBnd=0x1000 ONLY CAP_NET_ADMIN, NoNewPrivs=1)
  - stremhu-netns-ctl (worker_netns, UID=0, CapEff=CapBnd=0x11c0 CAP_NET_ADMIN|CAP_SETPCAP|CAP_SETUID|CAP_SETGID,
    CAP_SYS_ADMIN DROPPED, NoNewPrivs=1)
  - FastAPI mock (init_net, UID=10000, GID=10000, groups=[10002, 10003], Cap*=0, NoNewPrivs=1)
  - TorrentWorker #1 & #2 (worker_netns, UID=10001, GID=10001, groups=[10002, 10003], Cap*=0, NoNewPrivs=1)

Verifies:
  1. Zero processes in the entire container retain CAP_SYS_ADMIN after t=0 bootstrap.
  2. Cross-UID POSIX Shared Memory (/dev/shm, 64 MiB limit): created by UID=10000, fchown(-1, 10002) + 0660
     with ZERO capabilities, written by UID=10001 (create=False + resource_tracker.unregister), read zero-copy
     by UID=10000 via memoryview, surviving Worker #1 SIGKILL + Worker #2 restart without /dev/shm duplication.
  3. Cross-netns + cross-UID Unix Domain Socket (/run/stremhu/worker/ipc.sock, 0660, 10000:10002) with
     SO_PEERCRED kernel credential verification + 256-bit per-spawn token.
  4. Mandatory negative security boundaries from Worker (UID=10001):
     - connect('/run/stremhu/netd/netd.sock') -> EACCES
     - open('/tmp/poc_app_data/system/database/app.db') -> EACCES
     - open('/tmp/poc_app_data/system/certificates/privkey.pem') -> EACCES
     - unlink('/dev/shm/stremhu_rev4_pool') -> EPERM (sticky /dev/shm protection)
  5. Downloads directory (/tmp/poc_app_data/downloads, 2770, 10001:10003) seamless R/W/delete across
     UID=10001 (VPN Protected Mode) and UID=10000 (VPN Disabled Mode), including legacy root-file migration.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from multiprocessing import resource_tracker, shared_memory
from pathlib import Path

POC_VERSION = "rev4-1.0.0"

PR_SET_PDEATHSIG = 1
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4

CAP_KILL = 5
CAP_SETGID = 6
CAP_SETUID = 7
CAP_SETPCAP = 8
CAP_NET_ADMIN = 12
CAP_SYS_ADMIN = 21

UID_APP = 10000
GID_APP = 10000
UID_WORKER = 10001
GID_WORKER = 10001
GID_IPC = 10002
GID_DOWNLOADS = 10003

SHM_POOL_NAME = "stremhu_rev4_pool"
SHM_POOL_BYTES = 32 * 1024 * 1024  # 32 MiB (8 x 4 MiB pages)
PAGE_BYTES = 4 * 1024 * 1024

RUN_ROOT = Path("/run/stremhu_rev4_poc")
NETD_DIR = RUN_ROOT / "netd"
NETD_SOCK = NETD_DIR / "netd.sock"
WORKER_IPC_DIR = RUN_ROOT / "worker"
WORKER_IPC_SOCK = WORKER_IPC_DIR / "ipc.sock"
DATA_ROOT = Path("/tmp/poc_rev4_app_data")
SYSTEM_DIR = DATA_ROOT / "system"
DB_DIR = SYSTEM_DIR / "database"
CERT_DIR = SYSTEM_DIR / "certificates"
DOWNLOADS_DIR = DATA_ROOT / "downloads"

_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapUserHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapUserData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def read_proc_status(pid: int | str = "self") -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def get_cap_last_cap() -> int:
    return int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii").strip())


def set_caps_exact(cap_bits: list[int]) -> None:
    libc = _libc()
    hdr = _CapUserHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapUserData * 2)()
    low = 0
    high = 0
    for b in cap_bits:
        if b < 32:
            low |= 1 << b
        else:
            high |= 1 << (b - 32)
    data[0].effective = low
    data[0].permitted = low
    data[0].inheritable = 0
    data[1].effective = high
    data[1].permitted = high
    data[1].inheritable = 0
    if libc.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"capset failed: {os.strerror(err)}")


def restrict_root_helper_caps(keep_caps: set[int], pdeathsig_ppid: int) -> dict[str, str]:
    """Drops CAP_SYS_ADMIN and all non-whitelisted caps from CapBnd/CapPrm/CapEff, sets NoNewPrivs=1."""
    libc = _libc()
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    cap_last = get_cap_last_cap()
    for cap in range(cap_last + 1):
        if cap not in keep_caps:
            if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
                err = ctypes.get_errno()
                raise OSError(err, f"PR_CAPBSET_DROP({cap}) failed: {os.strerror(err)}")
    set_caps_exact(sorted(keep_caps))
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    if os.getppid() != pdeathsig_ppid:
        os._exit(91)
    st = read_proc_status("self")
    # Fail-closed invariant: CAP_SYS_ADMIN bit (21 -> 0x200000) MUST be 0 in CapEff and CapBnd!
    if (int(st["CapEff"], 16) & (1 << CAP_SYS_ADMIN)) != 0 or (int(st["CapBnd"], 16) & (1 << CAP_SYS_ADMIN)) != 0:
        os._exit(79)
    return {
        "pid": str(os.getpid()),
        "Uid": st["Uid"],
        "Gid": st["Gid"],
        "CapEff": st["CapEff"],
        "CapPrm": st["CapPrm"],
        "CapBnd": st["CapBnd"],
        "NoNewPrivs": st["NoNewPrivs"],
    }


def drop_to_unprivileged_user(
    uid: int,
    gid: int,
    supp_groups: list[int],
    pdeathsig_ppid: int | None,
    umask_val: int = 0o007,
) -> dict[str, str]:
    """Irreversible 6-step privilege drop to unprivileged UID/GID + supplementary groups, Cap*=0, NoNewPrivs=1."""
    libc = _libc()
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    cap_last = get_cap_last_cap()
    for cap in range(cap_last + 1):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"PR_CAPBSET_DROP({cap}) failed: {os.strerror(err)}")
    os.setgroups(supp_groups)
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    set_caps_exact([])
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if pdeathsig_ppid is not None:
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
        if os.getppid() != pdeathsig_ppid:
            os._exit(92)
    os.umask(umask_val)
    st = read_proc_status("self")
    zero = "0000000000000000"
    if (
        st["Uid"] != f"{uid}\t{uid}\t{uid}\t{uid}"
        or st["Gid"] != f"{gid}\t{gid}\t{gid}\t{gid}"
        or st["CapInh"] != zero
        or st["CapPrm"] != zero
        or st["CapEff"] != zero
        or st["CapBnd"] != zero
        or st.get("CapAmb", zero) != zero
        or st["NoNewPrivs"] != "1"
    ):
        os._exit(79)
    return {
        "pid": str(os.getpid()),
        "Uid": st["Uid"],
        "Gid": st["Gid"],
        "Groups": st["Groups"],
        "CapEff": st["CapEff"],
        "CapBnd": st["CapBnd"],
        "NoNewPrivs": st["NoNewPrivs"],
    }


def get_peercred(conn: socket.socket) -> tuple[int, int, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


def deterministic_bytes(tag: str, length: int) -> bytes:
    seed = hashlib.blake2b(tag.encode("utf-8"), digest_size=64).digest()
    tile = b"".join(hashlib.blake2b(seed + i.to_bytes(2, "big"), digest_size=64).digest() for i in range(1024))
    full, rem = divmod(length, len(tile))
    return (tile * full) + tile[:rem]


def shm_snapshot() -> dict[str, object]:
    st = os.statvfs("/dev/shm")
    entries = []
    for p in sorted(Path("/dev/shm").iterdir()):
        ps = p.stat()
        entries.append(
            {
                "name": p.name,
                "size": ps.st_size,
                "uid": ps.st_uid,
                "gid": ps.st_gid,
                "mode": oct(stat.S_IMODE(ps.st_mode)),
            }
        )
    return {
        "total_bytes": st.f_blocks * st.f_frsize,
        "used_bytes": (st.f_blocks - st.f_bfree) * st.f_frsize,
        "free_bytes": st.f_bavail * st.f_frsize,
        "entries": entries,
    }


# ============================================================================
# Child Role 1: TorrentWorker (spawned inside worker_netns by stremhu-netns-ctl)
# ============================================================================
def role_torrent_worker(worker_gen: int, token: str) -> int:
    try:
        priv = drop_to_unprivileged_user(
            UID_WORKER,
            GID_WORKER,
            [GID_IPC, GID_DOWNLOADS],
            pdeathsig_ppid=os.getppid(),
            umask_val=0o007,
        )
    except Exception:
        os._exit(79)

    import libtorrent as lt  # noqa: PLC0415

    netns_ino = os.stat("/proc/self/ns/net").st_ino
    ifaces = sorted(n for _i, n in socket.if_nameindex())

    # Mandatory negative boundary tests from UID=10001
    neg_tests: dict[str, object] = {}

    # 1. Worker MUST NOT be able to connect to /run/stremhu/netd/netd.sock
    try:
        s_bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s_bad.connect(str(NETD_SOCK))
        s_bad.close()
        neg_tests["netd_sock_blocked"] = False
    except PermissionError as exc:
        neg_tests["netd_sock_blocked"] = exc.errno == errno.EACCES
        neg_tests["netd_sock_errno"] = errno.errorcode.get(exc.errno, str(exc.errno))

    # 2. Worker MUST NOT be able to read app.db or TLS privkey.pem in /data/system
    for label, target in [
        ("system_db_blocked", DB_DIR / "app.db"),
        ("system_tls_key_blocked", CERT_DIR / "privkey.pem"),
    ]:
        try:
            _ = target.read_bytes()
            neg_tests[label] = False
        except PermissionError as exc:
            neg_tests[label] = exc.errno == errno.EACCES

    # 3. Attach to FastAPI's POSIX Shared Memory (/dev/shm/stremhu_rev4_pool) as UID=10001
    shm = shared_memory.SharedMemory(name=SHM_POOL_NAME, create=False)
    resource_tracker.unregister(shm._name, "shared_memory")

    # 4. Worker MUST NOT be able to unlink FastAPI's SHM segment in sticky /dev/shm (1777)
    try:
        os.unlink(f"/dev/shm/{SHM_POOL_NAME}")
        neg_tests["shm_unlink_blocked_by_sticky_bit"] = False
    except PermissionError as exc:
        neg_tests["shm_unlink_blocked_by_sticky_bit"] = exc.errno in (errno.EPERM, errno.EACCES)
        neg_tests["shm_unlink_errno"] = errno.errorcode.get(exc.errno, str(exc.errno))

    # 5. Downloads directory compatibility: read legacy-migrated file & write new torrent folder
    legacy_read_ok = (DOWNLOADS_DIR / "legacy_existing.mkv").read_bytes() == b"legacy-payload-v3"
    movie_dir = DOWNLOADS_DIR / f"Movie.Gen{worker_gen}"
    movie_dir.mkdir(exist_ok=True)
    part_file = movie_dir / "video.mkv"
    part_file.write_bytes(f"worker-gen-{worker_gen}-payload".encode("utf-8"))
    part_stat = part_file.stat()
    dir_stat = movie_dir.stat()

    # Connect across network namespaces to FastAPI's filesystem UDS (/run/stremhu/worker/ipc.sock)
    ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ipc.connect(str(WORKER_IPC_SOCK))
    f_in = ipc.makefile("r", encoding="utf-8")

    hello = {
        "op": "worker_hello",
        "worker_gen": worker_gen,
        "token": token,
        "pid": os.getpid(),
        "priv": priv,
        "netns_ino": netns_ino,
        "ifaces": ifaces,
        "libtorrent_version": lt.__version__,
        "neg_tests": neg_tests,
        "downloads_check": {
            "legacy_read_ok": legacy_read_ok,
            "created_dir_gid": dir_stat.st_gid,
            "created_dir_setgid": bool(dir_stat.st_mode & stat.S_ISGID),
            "created_file_uid": part_stat.st_uid,
            "created_file_gid": part_stat.st_gid,
            "created_file_mode": oct(stat.S_IMODE(part_stat.st_mode)),
        },
    }
    ipc.sendall((json.dumps(hello) + "\n").encode("utf-8"))

    while True:
        line = f_in.readline()
        if not line:
            break
        req = json.loads(line)
        if req["op"] == "write_shm":
            offset = int(req["offset"])
            length = int(req["length"])
            tag = str(req["tag"])
            payload = deterministic_bytes(tag, length)
            shm.buf[offset : offset + length] = payload
            resp = {
                "op": "shm_ready",
                "lease_id": req["lease_id"],
                "length": length,
                "worker_sha256": hashlib.sha256(payload).hexdigest(),
            }
            ipc.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        elif req["op"] == "shutdown":
            break

    f_in.close()
    ipc.close()
    shm.close()
    return 0


# ============================================================================
# Child Role 2: stremhu-netns-ctl (inside worker_netns, CAP_SYS_ADMIN DROPPED!)
# ============================================================================
def role_netns_ctl(ctrl_fd: int, bootstrap_ppid: int) -> int:
    ctrl = socket.socket(fileno=ctrl_fd)
    f_in = ctrl.makefile("r", encoding="utf-8")

    # Unshare once at t=0 bootstrap BEFORE dropping CAP_SYS_ADMIN!
    os.unshare(os.CLONE_NEWNET)
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    netns_ino = os.stat("/proc/self/ns/net").st_ino

    # Permanently drop CAP_SYS_ADMIN! Keep ONLY CAP_KILL + CAP_SETGID + CAP_SETUID + CAP_SETPCAP + CAP_NET_ADMIN (0x11e0)
    # Note: CAP_KILL (bit 5) is required by kernel/signal.c check_kill_permission() for UID=0 supervisor to SIGTERM/SIGKILL UID=10001 worker!
    priv = restrict_root_helper_caps(
        {CAP_KILL, CAP_SETGID, CAP_SETUID, CAP_SETPCAP, CAP_NET_ADMIN},
        pdeathsig_ppid=bootstrap_ppid,
    )
    ctrl.sendall((json.dumps({"event": "netctl_ready", "netns_ino": netns_ino, "priv": priv}) + "\n").encode("utf-8"))

    workers: dict[int, subprocess.Popen[bytes]] = {}
    script_path = str(Path(__file__).resolve())

    while True:
        # Reap exited workers
        for gen, proc in list(workers.items()):
            if proc.poll() is not None:
                workers.pop(gen, None)

        line = f_in.readline()
        if not line:
            break
        cmd = json.loads(line)
        op = cmd["op"]

        if op == "configure_wg_and_killswitch":
            subprocess.run(["ip", "addr", "replace", "10.2.0.2/32", "dev", "stremhu-wg0"], check=True)
            subprocess.run(["ip", "link", "set", "mtu", "1420", "up", "dev", "stremhu-wg0"], check=True)
            subprocess.run(["ip", "route", "replace", "default", "dev", "stremhu-wg0"], check=True)
            nft_rules = """
table inet stremhu_killswitch
delete table inet stremhu_killswitch
table inet stremhu_killswitch {
    chain output {
        type filter hook output priority filter; policy drop;
        oifname "lo" ip daddr 127.0.0.1 ip saddr 127.0.0.1 accept
        oifname "stremhu-wg0" meta nfproto ipv4 accept
    }
}
"""
            subprocess.run(["nft", "-c", "-f", "-"], input=nft_rules, text=True, check=True)
            subprocess.run(["nft", "-f", "-"], input=nft_rules, text=True, check=True)
            ctrl.sendall(
                (
                    json.dumps(
                        {
                            "ok": True,
                            "ifaces": sorted(n for _i, n in socket.if_nameindex()),
                            "netctl_CapEff": read_proc_status("self")["CapEff"],
                            "netctl_CapBnd": read_proc_status("self")["CapBnd"],
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        elif op == "spawn_worker":
            gen = int(cmd["worker_gen"])
            token = str(cmd["token"])
            proc = subprocess.Popen(
                [sys.executable, script_path, "--role", "worker", "--gen", str(gen), "--token", token],
                close_fds=True,
            )
            workers[gen] = proc
            ctrl.sendall((json.dumps({"ok": True, "worker_pid": proc.pid}) + "\n").encode("utf-8"))
        elif op == "kill_worker":
            gen = int(cmd["worker_gen"])
            proc = workers.pop(gen, None)
            if proc is not None and proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            ctrl.sendall((json.dumps({"ok": True}) + "\n").encode("utf-8"))
        elif op == "shutdown":
            for proc in workers.values():
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            ctrl.sendall((json.dumps({"ok": True}) + "\n").encode("utf-8"))
            break

    return 0


# ============================================================================
# Child Role 3: stremhu-netd (in init_net, ONLY CAP_NET_ADMIN 0x1000!)
# ============================================================================
def role_netd(netctl_fd: int, worker_netns_fd: int, ready_fd: int, bootstrap_ppid: int) -> int:
    netctl_sock = socket.socket(fileno=netctl_fd)
    netctl_in = netctl_sock.makefile("r", encoding="utf-8")
    ready_sock = socket.socket(fileno=ready_fd)

    if NETD_SOCK.exists():
        NETD_SOCK.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(NETD_SOCK))
    os.chown(NETD_SOCK, 0, GID_APP)
    os.chmod(NETD_SOCK, 0o660)
    srv.listen(4)

    # Permanently drop CAP_SYS_ADMIN and CAP_CHOWN/SETUID/SETGID -> keep ONLY CAP_NET_ADMIN (0x1000)!
    priv = restrict_root_helper_caps({CAP_NET_ADMIN}, pdeathsig_ppid=bootstrap_ppid)
    ready_sock.sendall((json.dumps({"event": "netd_ready", "priv": priv}) + "\n").encode("utf-8"))
    ready_sock.close()

    conn, _ = srv.accept()
    peer_pid, peer_uid, peer_gid = get_peercred(conn)
    if peer_uid != UID_APP or peer_gid != GID_APP:
        conn.close()
        srv.close()
        return 81

    f_in = conn.makefile("r", encoding="utf-8")
    while True:
        line = f_in.readline()
        if not line:
            break
        req = json.loads(line)
        op = req["op"]
        if op == "setup_tunnel":
            # Birthplace WireGuard creation in init_net + move to worker_netns WITHOUT CAP_SYS_ADMIN!
            # CRITICAL Python 3.12 detail: subprocess.run defaults to close_fds=True, so worker_netns_fd
            # MUST be marked inheritable and passed in pass_fds=(worker_netns_fd,) for `ip` to open /proc/self/fd/<fd>!
            os.set_inheritable(worker_netns_fd, True)
            subprocess.run(["ip", "link", "add", "dev", "stremhu-wg0", "type", "wireguard"], check=True)
            subprocess.run(
                ["ip", "link", "set", "dev", "stremhu-wg0", "netns", f"/proc/self/fd/{worker_netns_fd}"],
                pass_fds=(worker_netns_fd,),
                check=True,
            )
            netctl_sock.sendall((json.dumps({"op": "configure_wg_and_killswitch"}) + "\n").encode("utf-8"))
            res = json.loads(netctl_in.readline())
            res["netd_peercred_verified"] = {"pid": peer_pid, "uid": peer_uid, "gid": peer_gid}
            res["netd_CapEff"] = read_proc_status("self")["CapEff"]
            res["netd_CapBnd"] = read_proc_status("self")["CapBnd"]
            conn.sendall((json.dumps(res) + "\n").encode("utf-8"))
        elif op in ("spawn_worker", "kill_worker", "shutdown"):
            netctl_sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
            res = json.loads(netctl_in.readline())
            conn.sendall((json.dumps(res) + "\n").encode("utf-8"))
            if op == "shutdown":
                break

    f_in.close()
    conn.close()
    srv.close()
    os.close(worker_netns_fd)
    return 0


# ============================================================================
# Child Role 4: FastAPI Mock (init_net, UID=10000, Cap*=0, NoNewPrivs=1)
# ============================================================================
def role_fastapi_app(result_fd: int, bootstrap_ppid: int) -> int:
    res_sock = socket.socket(fileno=result_fd)
    priv = drop_to_unprivileged_user(
        UID_APP,
        GID_APP,
        [GID_IPC, GID_DOWNLOADS],
        pdeathsig_ppid=bootstrap_ppid,
        umask_val=0o007,
    )
    res_sock.sendall((json.dumps({"event": "fastapi_priv_dropped", "priv": priv}) + "\n").encode("utf-8"))

    # 1. Verify HTTP :7070 bind as UID=10000, Cap*=0
    http_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    http_sock.bind(("0.0.0.0", 7070))
    http_sock.listen(4)

    # 2. Verify /data/system R/W access as UID=10000
    db_ok = (DB_DIR / "app.db").read_bytes() == b"sqlite-encrypted-wg-config"
    (DB_DIR / "app.db-wal").write_bytes(b"wal-test-ok")

    # 3. Create 32 MiB POSIX Shared Memory as UID=10000 and grant GID_IPC (10002) 0660 WITHOUT CAP_CHOWN!
    shm_t0 = shm_snapshot()
    shm = shared_memory.SharedMemory(name=SHM_POOL_NAME, create=True, size=SHM_POOL_BYTES)
    os.fchown(shm._fd, -1, GID_IPC)
    os.fchmod(shm._fd, 0o660)
    shm_t1 = shm_snapshot()

    # 4. Bind Worker IPC Unix Domain Socket in /run/stremhu/worker/ipc.sock (2770 dir -> group 10002)
    if WORKER_IPC_SOCK.exists():
        WORKER_IPC_SOCK.unlink()
    worker_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    worker_srv.bind(str(WORKER_IPC_SOCK))
    os.chmod(WORKER_IPC_SOCK, 0o660)
    worker_srv.listen(4)
    worker_sock_stat = WORKER_IPC_SOCK.stat()

    # 5. Connect to stremhu-netd over /run/stremhu/netd/netd.sock and bring up tunnel + Kill Switch
    netd = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    netd.connect(str(NETD_SOCK))
    netd_in = netd.makefile("r", encoding="utf-8")

    netd.sendall((json.dumps({"op": "setup_tunnel"}) + "\n").encode("utf-8"))
    tunnel_res = json.loads(netd_in.readline())

    # 6. Spawn TorrentWorker #1 (gen=1) inside worker_netns
    tok1 = secrets.token_hex(32)
    netd.sendall((json.dumps({"op": "spawn_worker", "worker_gen": 1, "token": tok1}) + "\n").encode("utf-8"))
    spawn1_res = json.loads(netd_in.readline())

    w1_conn, _ = worker_srv.accept()
    w1_pid, w1_uid, w1_gid = get_peercred(w1_conn)
    assert w1_uid == UID_WORKER and w1_gid == GID_WORKER, f"Unexpected Worker SO_PEERCRED: {w1_uid}:{w1_gid}"
    w1_in = w1_conn.makefile("r", encoding="utf-8")
    w1_hello = json.loads(w1_in.readline())
    assert secrets.compare_digest(w1_hello["token"], tok1), "Worker token mismatch!"

    # 7. Pin Page 0 (4 MiB) via Worker #1 and also transfer a 16 MiB 4-page span (Pages 4..7)!
    w1_conn.sendall(
        (json.dumps({"op": "write_shm", "lease_id": 1, "offset": 0, "length": PAGE_BYTES, "tag": "pinned-slot0"}) + "\n").encode("utf-8")
    )
    r_pin = json.loads(w1_in.readline())
    pinned_view = memoryview(shm.buf)[0:PAGE_BYTES]
    expected_pin_sha = hashlib.sha256(deterministic_bytes("pinned-slot0", PAGE_BYTES)).hexdigest()
    sha_before_kill = hashlib.sha256(pinned_view).hexdigest()

    len_16m = 16 * 1024 * 1024
    off_16m = 4 * PAGE_BYTES
    w1_conn.sendall(
        (json.dumps({"op": "write_shm", "lease_id": 2, "offset": off_16m, "length": len_16m, "tag": "remux-16m-w1"}) + "\n").encode("utf-8")
    )
    r_16m_w1 = json.loads(w1_in.readline())
    mv_16m_w1 = memoryview(shm.buf)[off_16m : off_16m + len_16m]
    sha_16m_w1 = hashlib.sha256(mv_16m_w1).hexdigest()
    mv_16m_w1.release()

    # 8. SIGKILL Worker #1 while FastAPI (UID=10000) still holds pinned_view on Page 0!
    netd.sendall((json.dumps({"op": "kill_worker", "worker_gen": 1}) + "\n").encode("utf-8"))
    _ = json.loads(netd_in.readline())
    w1_in.close()
    w1_conn.close()

    # Spawn Worker #2 (gen=2) and verify re-attach to SAME /dev/shm segment without ENOSPC or duplication!
    tok2 = secrets.token_hex(32)
    netd.sendall((json.dumps({"op": "spawn_worker", "worker_gen": 2, "token": tok2}) + "\n").encode("utf-8"))
    spawn2_res = json.loads(netd_in.readline())

    w2_conn, _ = worker_srv.accept()
    w2_pid, w2_uid, w2_gid = get_peercred(w2_conn)
    assert w2_uid == UID_WORKER and w2_gid == GID_WORKER
    w2_in = w2_conn.makefile("r", encoding="utf-8")
    w2_hello = json.loads(w2_in.readline())
    assert secrets.compare_digest(w2_hello["token"], tok2)

    shm_t2_after_sigkill = shm_snapshot()

    # Worker #2 writes a new 16 MiB piece into Pages 4..7 while Page 0 stays pinned!
    w2_conn.sendall(
        (json.dumps({"op": "write_shm", "lease_id": 3, "offset": off_16m, "length": len_16m, "tag": "remux-16m-w2"}) + "\n").encode("utf-8")
    )
    r_16m_w2 = json.loads(w2_in.readline())
    mv_16m_w2 = memoryview(shm.buf)[off_16m : off_16m + len_16m]
    sha_16m_w2 = hashlib.sha256(mv_16m_w2).hexdigest()
    mv_16m_w2.release()

    # Verify Page 0 pinned_view was NEVER corrupted across Worker #1 SIGKILL + Worker #2 16 MiB write!
    sha_after_kill = hashlib.sha256(pinned_view).hexdigest()
    pinned_view.release()

    # 9. Verify FastAPI (UID=10000) can read & clean up Worker #1's files in /data/downloads (VPN Disabled Mode switch compatibility!)
    w1_file = DOWNLOADS_DIR / "Movie.Gen1" / "video.mkv"
    fastapi_read_worker_download_ok = w1_file.read_bytes() == b"worker-gen-1-payload"
    w1_file.unlink()
    (DOWNLOADS_DIR / "Movie.Gen1").rmdir()
    fastapi_deleted_worker_download_ok = not (DOWNLOADS_DIR / "Movie.Gen1").exists()

    # Clean shutdown
    w2_conn.sendall((json.dumps({"op": "shutdown"}) + "\n").encode("utf-8"))
    w2_in.close()
    w2_conn.close()

    netd.sendall((json.dumps({"op": "shutdown"}) + "\n").encode("utf-8"))
    _ = json.loads(netd_in.readline())
    netd_in.close()
    netd.close()

    worker_srv.close()
    if WORKER_IPC_SOCK.exists():
        WORKER_IPC_SOCK.unlink()
    http_sock.close()

    shm.close()
    shm.unlink()
    shm_t3_clean = shm_snapshot()

    report = {
        "fastapi_priv": priv,
        "fastapi_system_db_rw_ok": db_ok,
        "worker_ipc_sock_stat": {
            "uid": worker_sock_stat.st_uid,
            "gid": worker_sock_stat.st_gid,
            "mode": oct(stat.S_IMODE(worker_sock_stat.st_mode)),
        },
        "tunnel_setup_via_netd": tunnel_res,
        "worker_1": {
            "spawn": spawn1_res,
            "peercred": {"pid": w1_pid, "uid": w1_uid, "gid": w1_gid},
            "hello": w1_hello,
            "16mib_sha256_match": sha_16m_w1 == r_16m_w1["worker_sha256"],
        },
        "worker_2_after_sigkill": {
            "spawn": spawn2_res,
            "peercred": {"pid": w2_pid, "uid": w2_uid, "gid": w2_gid},
            "16mib_sha256_match": sha_16m_w2 == r_16m_w2["worker_sha256"],
        },
        "pinned_page0_integrity_across_sigkill": {
            "expected_sha256": expected_pin_sha,
            "before_sigkill_match": sha_before_kill == expected_pin_sha,
            "after_sigkill_and_w2_write_match": sha_after_kill == expected_pin_sha,
        },
        "downloads_cross_uid_compatibility": {
            "fastapi_read_worker_download_ok": fastapi_read_worker_download_ok,
            "fastapi_deleted_worker_download_ok": fastapi_deleted_worker_download_ok,
        },
        "shm_snapshots": {
            "t0_initial": shm_t0,
            "t1_created_by_uid10000": shm_t1,
            "t2_after_worker1_sigkill_and_worker2_attach": shm_t2_after_sigkill,
            "t3_after_clean_unlink": shm_t3_clean,
        },
    }
    res_sock.sendall((json.dumps(report) + "\n").encode("utf-8"))
    res_sock.close()
    return 0


# ============================================================================
# PID 1 Bootstrap Orchestrator (t=0 setup -> drops ALL capabilities!)
# ============================================================================
def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--role":
        role = sys.argv[2]
        if role == "worker":
            gen = int(sys.argv[sys.argv.index("--gen") + 1])
            tok = sys.argv[sys.argv.index("--token") + 1]
            return role_torrent_worker(gen, tok)
        if role == "netctl":
            fd = int(sys.argv[sys.argv.index("--fd") + 1])
            ppid = int(sys.argv[sys.argv.index("--ppid") + 1])
            return role_netns_ctl(fd, ppid)
        if role == "netd":
            netctl_fd = int(sys.argv[sys.argv.index("--netctl-fd") + 1])
            ns_fd = int(sys.argv[sys.argv.index("--ns-fd") + 1])
            r_fd = int(sys.argv[sys.argv.index("--ready-fd") + 1])
            ppid = int(sys.argv[sys.argv.index("--ppid") + 1])
            return role_netd(netctl_fd, ns_fd, r_fd, ppid)
        if role == "fastapi":
            r_fd = int(sys.argv[sys.argv.index("--res-fd") + 1])
            ppid = int(sys.argv[sys.argv.index("--ppid") + 1])
            return role_fastapi_app(r_fd, ppid)
        return 2

    script_path = Path(__file__).resolve()
    script_sha256 = hashlib.sha256(script_path.read_bytes()).hexdigest()
    bootstrap_pid = os.getpid()

    try:
        # Clean & prepare runtime socket and data directories (t=0 bootstrap)
        for p in (RUN_ROOT, DATA_ROOT):
            if p.exists():
                shutil.rmtree(p)

        RUN_ROOT.mkdir(parents=True, mode=0o711)
        os.chown(RUN_ROOT, 0, 0)
        os.chmod(RUN_ROOT, 0o711)

        NETD_DIR.mkdir(mode=0o750)
        os.chown(NETD_DIR, 0, GID_APP)
        os.chmod(NETD_DIR, 0o750)

        WORKER_IPC_DIR.mkdir(mode=0o2770)
        os.chown(WORKER_IPC_DIR, UID_APP, GID_IPC)
        os.chmod(WORKER_IPC_DIR, 0o2770)

        DB_DIR.mkdir(parents=True, mode=0o700)
        CERT_DIR.mkdir(parents=True, mode=0o700)
        (DB_DIR / "app.db").write_bytes(b"sqlite-encrypted-wg-config")
        (CERT_DIR / "privkey.pem").write_bytes(b"tls-private-key-placeholder")
        for root, dirs, files in os.walk(SYSTEM_DIR):
            os.chown(root, UID_APP, GID_APP)
            os.chmod(root, 0o700)
            for d in dirs:
                dp = Path(root) / d
                os.chown(dp, UID_APP, GID_APP)
                os.chmod(dp, 0o700)
            for fn in files:
                fp = Path(root) / fn
                os.chown(fp, UID_APP, GID_APP)
                os.chmod(fp, 0o600)
        os.chown(DATA_ROOT, 0, 0)
        os.chmod(DATA_ROOT, 0o755)

        # Simulate legacy root-owned download file migrated at t=0 startup
        DOWNLOADS_DIR.mkdir(parents=True, mode=0o755)
        legacy_file = DOWNLOADS_DIR / "legacy_existing.mkv"
        legacy_file.write_bytes(b"legacy-payload-v3")
        os.chown(DOWNLOADS_DIR, UID_WORKER, GID_DOWNLOADS)
        os.chmod(DOWNLOADS_DIR, 0o2770)
        os.chown(legacy_file, UID_WORKER, GID_DOWNLOADS)
        os.chmod(legacy_file, 0o660)

        # 1. Spawn stremhu-netns-ctl (unshares worker_netns once, then drops CAP_SYS_ADMIN)
        nc_parent, nc_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(nc_child.fileno(), True)
        netctl_proc = subprocess.Popen(
            [sys.executable, str(script_path), "--role", "netctl", "--fd", str(nc_child.fileno()), "--ppid", str(bootstrap_pid)],
            pass_fds=(nc_child.fileno(),),
        )
        nc_child.close()
        nc_in = nc_parent.makefile("r", encoding="utf-8")
        netctl_ready = json.loads(nc_in.readline())

        # Open worker_netns_fd while PID 1 is still UID=0 (before dropping PID 1 capabilities!)
        worker_netns_fd = os.open(f"/proc/{netctl_proc.pid}/ns/net", os.O_RDONLY)
        os.set_inheritable(worker_netns_fd, True)
        os.set_inheritable(nc_parent.fileno(), True)

        # 2. Spawn stremhu-netd (binds /run/stremhu/netd/netd.sock, then drops CAP_SYS_ADMIN -> ONLY 0x1000)
        nd_r_parent, nd_r_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(nd_r_child.fileno(), True)
        netd_proc = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--role",
                "netd",
                "--netctl-fd",
                str(nc_parent.fileno()),
                "--ns-fd",
                str(worker_netns_fd),
                "--ready-fd",
                str(nd_r_child.fileno()),
                "--ppid",
                str(bootstrap_pid),
            ],
            pass_fds=(nc_parent.fileno(), worker_netns_fd, nd_r_child.fileno()),
        )
        nd_r_child.close()
        os.close(worker_netns_fd)
        nc_in.close()
        nc_parent.close()
        netd_ready = json.loads(nd_r_parent.makefile("r", encoding="utf-8").readline())
        nd_r_parent.close()

        # 3. Spawn FastAPI process (drops to UID=10000, Cap*=0, NoNewPrivs=1)
        fa_parent, fa_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(fa_child.fileno(), True)
        fastapi_proc = subprocess.Popen(
            [sys.executable, str(script_path), "--role", "fastapi", "--res-fd", str(fa_child.fileno()), "--ppid", str(bootstrap_pid)],
            pass_fds=(fa_child.fileno(),),
        )
        fa_child.close()

        fa_in = fa_parent.makefile("r", encoding="utf-8")
        fastapi_ready = json.loads(fa_in.readline())

        # 4. PID 1 ITSELF drops ALL capabilities & switches to UID=10000 so ZERO processes hold CAP_SYS_ADMIN!
        pid1_post_bootstrap_priv = drop_to_unprivileged_user(
            UID_APP,
            GID_APP,
            [],
            pdeathsig_ppid=None,
            umask_val=0o077,
        )

        # Audit /proc/[0-9]*/status across ALL visible processes in steady state: confirm 0 hold CAP_SYS_ADMIN!
        cap_sys_admin_holders = []
        steady_state_processes = []
        for p in sorted(Path("/proc").iterdir(), key=lambda x: int(x.name) if x.name.isdigit() else -1):
            if p.name.isdigit():
                try:
                    st = read_proc_status(int(p.name))
                    eff = int(st["CapEff"], 16)
                    bnd = int(st["CapBnd"], 16)
                    steady_state_processes.append(
                        {
                            "pid": p.name,
                            "Name": st.get("Name", ""),
                            "Uid": st["Uid"],
                            "CapEff": st["CapEff"],
                            "CapBnd": st["CapBnd"],
                            "NoNewPrivs": st.get("NoNewPrivs", ""),
                        }
                    )
                    if (eff & (1 << CAP_SYS_ADMIN)) or (bnd & (1 << CAP_SYS_ADMIN)):
                        cap_sys_admin_holders.append({"pid": p.name, "CapEff": st["CapEff"], "CapBnd": st["CapBnd"]})
                except OSError:
                    pass

        fastapi_report = json.loads(fa_in.readline())
        fa_in.close()
        fa_parent.close()

        fastapi_rc = fastapi_proc.wait(timeout=10)
        netd_rc = netd_proc.wait(timeout=10)
        netctl_rc = netctl_proc.wait(timeout=10)

        w1_neg = fastapi_report["worker_1"]["hello"]["neg_tests"]
        w1_dl = fastapi_report["worker_1"]["hello"]["downloads_check"]
        t2_shm = fastapi_report["shm_snapshots"]["t2_after_worker1_sigkill_and_worker2_attach"]
        all_passed = all(
            [
                len(cap_sys_admin_holders) == 0,
                fastapi_ready["priv"]["CapEff"] == "0000000000000000",
                netctl_ready["priv"]["CapEff"] == "00000000000011e0",
                netctl_ready["priv"]["CapBnd"] == "00000000000011e0",
                netd_ready["priv"]["CapEff"] == "0000000000001000",
                netd_ready["priv"]["CapBnd"] == "0000000000001000",
                fastapi_report["fastapi_priv"]["CapEff"] == "0000000000000000",
                fastapi_report["worker_1"]["hello"]["priv"]["CapEff"] == "0000000000000000",
                bool(w1_neg.get("netd_sock_blocked")),
                bool(w1_neg.get("system_db_blocked")),
                bool(w1_neg.get("system_tls_key_blocked")),
                bool(w1_neg.get("shm_unlink_blocked_by_sticky_bit")),
                bool(w1_dl.get("legacy_read_ok")),
                bool(w1_dl.get("created_dir_setgid")),
                w1_dl.get("created_file_gid") == GID_DOWNLOADS,
                fastapi_report["worker_1"]["16mib_sha256_match"],
                fastapi_report["worker_2_after_sigkill"]["16mib_sha256_match"],
                fastapi_report["pinned_page0_integrity_across_sigkill"]["before_sigkill_match"],
                fastapi_report["pinned_page0_integrity_across_sigkill"]["after_sigkill_and_w2_write_match"],
                fastapi_report["downloads_cross_uid_compatibility"]["fastapi_read_worker_download_ok"],
                fastapi_report["downloads_cross_uid_compatibility"]["fastapi_deleted_worker_download_ok"],
                len(t2_shm["entries"]) == 1 and t2_shm["entries"][0]["size"] == SHM_POOL_BYTES,
                0 < t2_shm["used_bytes"] <= SHM_POOL_BYTES,
                fastapi_report["shm_snapshots"]["t3_after_clean_unlink"]["used_bytes"] == 0,
                fastapi_rc == 0 and netd_rc == 0 and netctl_rc == 0,
            ]
        )

        out = {
            "poc_id": "Rev4-Task3-CrossUID-IPC-and-FS",
            "poc_version": POC_VERSION,
            "script_sha256": script_sha256,
            "executed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "PASS" if all_passed else "FAIL",
            "zero_cap_sys_admin_holders_in_steady_state": len(cap_sys_admin_holders) == 0,
            "steady_state_processes": steady_state_processes,
            "cap_sys_admin_holders": cap_sys_admin_holders,
            "pid1_post_bootstrap_priv": pid1_post_bootstrap_priv,
            "netctl_ready": netctl_ready,
            "netd_ready": netd_ready,
            "fastapi_and_workers_report": fastapi_report,
            "exit_codes": {"fastapi": fastapi_rc, "netd": netd_rc, "netctl": netctl_rc},
        }
        print(json.dumps(out, indent=2))
        return 0 if all_passed else 1
    finally:
        try:
            s = shared_memory.SharedMemory(name=SHM_POOL_NAME, create=False)
            s.close()
            s.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
