#!/usr/bin/env python3
"""Phase 0 PoC P0-1 + Rule 10:
1. B-MountFree CLONE_NEWNET + pre-opened netns_fd + 6-step Mandatory Worker Privilege Drop.
2. Fail-closed exit(79) before `import libtorrent` on privilege-drop failure.
3. Post-setresuid PR_SET_PDEATHSIG kernel orphan-prevention verification.
4. Rule 10: Privileged Helper (`stremhu-netd`) & post-bootstrap CAP_SYS_ADMIN elimination test
   (proving FastAPI can run as UID=10000 with CapEff=0, and testing whether runtime WG/nft
    lifecycle works even after CAP_SYS_ADMIN is dropped from CapBnd everywhere!).

PoC ONLY — zero production files touched. Automatic cleanup included.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Linux prctl / capset constants (x86_64 & aarch64 ABI identical)
PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4

CAP_NET_ADMIN = 12
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21

_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapUserHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapUserData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _get_libc() -> ctypes.CDLL:
    libc_name = ctypes.util.find_library("c") or "libc.so.6"
    return ctypes.CDLL(libc_name, use_errno=True)


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
    libc = _get_libc()
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


def drop_all_privileges_and_verify(
    target_uid: int,
    target_gid: int,
    expected_ppid: int,
    simulate_failure: bool = False,
) -> dict[str, object]:
    """Exact 6-step mandatory privilege drop from Section 12.2.1 (before import libtorrent)."""
    assert "libtorrent" not in sys.modules, "libtorrent MUST NOT be imported before privdrop!"
    if target_uid < 1 or target_gid < 1:
        raise ValueError("target_uid/gid must be >= 1")

    libc = _get_libc()

    # Step 1: Clear ambient capabilities
    if libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        if err != errno.EINVAL:
            raise OSError(err, "PR_CAP_AMBIENT_CLEAR_ALL failed")

    # Step 2: Drop entire bounding set (CapBnd -> 0) while CAP_SETPCAP is still active
    cap_last = get_cap_last_cap()
    for cap in range(cap_last + 1):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"PR_CAPBSET_DROP({cap}) failed: {os.strerror(err)}")

    if simulate_failure:
        raise RuntimeError("Simulated privilege drop failure before libtorrent import")

    # Step 3: Supplementary groups, GID, UID irreversible switch
    os.setgroups([])
    os.setresgid(target_gid, target_gid, target_gid)
    os.setresuid(target_uid, target_uid, target_uid)

    # Step 4: Explicit capset zeroing CapEff/CapPrm/CapInh
    set_caps_exact([])

    # Step 5: PR_SET_NO_NEW_PRIVS=1 and re-arming PR_SET_PDEATHSIG after setresuid()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"PR_SET_NO_NEW_PRIVS failed: {os.strerror(err)}")
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"PR_SET_PDEATHSIG failed: {os.strerror(err)}")
    if os.getppid() != expected_ppid:
        raise RuntimeError("Parent exited during privilege drop race window")

    # Step 6: Self-audit of /proc/self/status + negative probes
    st = read_proc_status("self")
    expected_uid_line = f"{target_uid}\t{target_uid}\t{target_uid}\t{target_uid}"
    expected_gid_line = f"{target_gid}\t{target_gid}\t{target_gid}\t{target_gid}"
    zero_hex = "0000000000000000"
    assert st["Uid"] == expected_uid_line, f"Bad Uid: {st['Uid']}"
    assert st["Gid"] == expected_gid_line, f"Bad Gid: {st['Gid']}"
    assert st.get("Groups", "") == "", f"Non-empty Groups: {st.get('Groups')}"
    for cap_key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert st[cap_key] == zero_hex, f"{cap_key} != 0: {st[cap_key]}"
    assert st["NoNewPrivs"] == "1", f"NoNewPrivs != 1: {st['NoNewPrivs']}"

    # Negative probes: unshare(CLONE_NEWNET), setuid(0), and nft flush MUST fail with EPERM!
    unshare_blocked = False
    try:
        os.unshare(os.CLONE_NEWNET)
    except PermissionError as exc:
        unshare_blocked = exc.errno == errno.EPERM

    setuid0_blocked = False
    try:
        os.setuid(0)
    except PermissionError as exc:
        setuid0_blocked = exc.errno == errno.EPERM

    nft_proc = subprocess.run(
        ["nft", "list", "ruleset"],
        capture_output=True,
        text=True,
        check=False,
    )
    nft_blocked = nft_proc.returncode != 0

    if not (unshare_blocked and setuid0_blocked and nft_blocked):
        raise RuntimeError(
            f"Negative probes failed: unshare={unshare_blocked} setuid0={setuid0_blocked} nft={nft_blocked}"
        )

    return {
        "status": {
            k: st[k]
            for k in (
                "Uid",
                "Gid",
                "Groups",
                "CapInh",
                "CapPrm",
                "CapEff",
                "CapBnd",
                "CapAmb",
                "NoNewPrivs",
                "Seccomp",
            )
        },
        "unshare_eperm": unshare_blocked,
        "setuid0_eperm": setuid0_blocked,
        "nft_blocked": nft_blocked,
        "nft_stderr": nft_proc.stderr.strip(),
    }


def worker_entry(sock_fd: int, mode: str) -> None:
    sock = socket.socket(fileno=sock_fd)
    ppid = os.getppid()
    try:
        os.unshare(os.CLONE_NEWNET)
        netns_stat = os.stat("/proc/self/ns/net")
        sock.sendall(
            (json.dumps({"stage": "unshared", "netns_ino": netns_stat.st_ino}) + "\n").encode()
        )
        cmd = sock.makefile("r").readline().strip()
        if cmd != "drop_now":
            os._exit(90)

        audit = drop_all_privileges_and_verify(
            target_uid=10001,
            target_gid=10001,
            expected_ppid=ppid,
            simulate_failure=(mode == "fail_drop"),
        )
        # Only AFTER full privilege drop is libtorrent imported!
        import libtorrent as lt  # noqa: PLC0415

        audit["libtorrent_imported_after_drop"] = lt.__version__
        audit["sysfs_class_net"] = sorted(os.listdir("/sys/class/net"))
        audit["rtnetlink_if_nameindex"] = [name for _idx, name in socket.if_nameindex()]
        sock.sendall((json.dumps({"stage": "dropped_ok", "audit": audit}) + "\n").encode())

        if mode.startswith("pdeathsig_wait:"):
            marker_fd = int(mode.split(":", 1)[1])

            def _on_sigterm(signum: int, _frame: object) -> None:
                os.write(marker_fd, f"SIGTERM_RECEIVED:{signum}\n".encode())
                os.close(marker_fd)
                os._exit(143)

            signal.signal(signal.SIGTERM, _on_sigterm)
            sock.sendall(b"pdeathsig_armed\n")
            time.sleep(5.0)
            os.write(marker_fd, b"TIMEOUT_NO_SIGTERM\n")
            os.close(marker_fd)
            os._exit(99)

        os._exit(0)
    except BaseException as exc:
        print(f"WORKER_PRIVDROP_ABORT: {exc!r}", file=sys.stderr, flush=True)
        os._exit(79)


def run_rule10_helper_poc() -> dict[str, object]:
    """Rule 10 PoC: Can we eliminate root + CAP_SYS_ADMIN from FastAPI AND even drop
    CAP_SYS_ADMIN from the privileged netlink helper after bootstrap?"""
    libc = _get_libc()
    p_sock, c_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    pid = os.fork()
    if pid == 0:
        # Child acts as co-resident Netns Netlink Agent (`stremhu-netns-ctl`):
        # 1. Enters isolated netns during bootstrap (uses CAP_SYS_ADMIN once).
        # 2. Permanently drops CAP_SYS_ADMIN (and everything except CAP_NET_ADMIN) from CapBnd & CapEff!
        p_sock.close()
        try:
            os.unshare(os.CLONE_NEWNET)
            cap_last = get_cap_last_cap()
            for cap in range(cap_last + 1):
                if cap != CAP_NET_ADMIN:
                    libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
            set_caps_exact([CAP_NET_ADMIN])
            libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

            st = read_proc_status("self")
            c_sock.sendall(
                (
                    json.dumps(
                        {
                            "event": "netctl_ready",
                            "pid": os.getpid(),
                            "CapEff": st["CapEff"],
                            "CapBnd": st["CapBnd"],
                            "NoNewPrivs": st["NoNewPrivs"],
                        }
                    )
                    + "\n"
                ).encode()
            )
            line = c_sock.makefile("r").readline().strip()
            if line == "configure_wg_and_nft":
                subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
                subprocess.run(["ip", "addr", "add", "10.2.0.2/32", "dev", "stremhu-wg0"], check=True)
                subprocess.run(["ip", "link", "set", "dev", "stremhu-wg0", "mtu", "1420", "up"], check=True)
                subprocess.run(["ip", "route", "add", "default", "dev", "stremhu-wg0"], check=True)
                nft_rules = (
                    "table inet stremhu_killswitch\n"
                    "delete table inet stremhu_killswitch\n"
                    "table inet stremhu_killswitch {\n"
                    "  chain output { type filter hook output priority 0; policy drop; oifname \"stremhu-wg0\" accept; }\n"
                    "}\n"
                )
                subprocess.run(["nft", "-c", "-f", "-"], input=nft_rules, text=True, check=True)
                subprocess.run(["nft", "-f", "-"], input=nft_rules, text=True, check=True)
                sysfs_ifaces = sorted(os.listdir("/sys/class/net"))
                rtnl_ifaces = sorted(name for _idx, name in socket.if_nameindex())
                c_sock.sendall(
                    (
                        json.dumps(
                            {
                                "event": "configured_ok",
                                "sysfs_class_net_stale_init_net": sysfs_ifaces,
                                "rtnetlink_if_nameindex_actual": rtnl_ifaces,
                            }
                        )
                        + "\n"
                    ).encode()
                )
            os._exit(0)
        except BaseException as exc:
            c_sock.sendall((json.dumps({"event": "error", "err": repr(exc)}) + "\n").encode())
            os._exit(1)

    c_sock.close()
    f_in = p_sock.makefile("r")
    ready_msg = json.loads(f_in.readline())

    # Open netns_fd in init_net helper BEFORE dropping CAP_SYS_ADMIN in an isolated fork test
    netctl_ns_fd = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

    # Fork a process representing `stremhu-netd` (init_net helper) that ALSO drops CAP_SYS_ADMIN
    # from CapBnd/CapEff/CapPrm, keeping ONLY CAP_NET_ADMIN!
    r_pipe, w_pipe = os.pipe()
    netd_pid = os.fork()
    if netd_pid == 0:
        os.close(r_pipe)
        try:
            cap_last = get_cap_last_cap()
            for cap in range(cap_last + 1):
                if cap != CAP_NET_ADMIN:
                    libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
            set_caps_exact([CAP_NET_ADMIN])
            libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            st = read_proc_status("self")

            # Test creating stremhu-wg0 and moving it via /proc/self/fd/<netctl_ns_fd> with ZERO CAP_SYS_ADMIN!
            subprocess.run(["ip", "link", "add", "dev", "stremhu-wg0", "type", "wireguard"], check=True)
            # Pass netns_fd across exec so iproute2 uses IFLA_NET_NS_FD
            os.set_inheritable(netctl_ns_fd, True)
            move_proc = subprocess.run(
                ["ip", "link", "set", "dev", "stremhu-wg0", "netns", f"/proc/self/fd/{netctl_ns_fd}"],
                pass_fds=(netctl_ns_fd,),
                capture_output=True,
                text=True,
                check=False,
            )
            if move_proc.returncode != 0:
                subprocess.run(["ip", "link", "del", "dev", "stremhu-wg0"], check=False)
                res = {
                    "netd_CapEff": st["CapEff"],
                    "netd_CapBnd": st["CapBnd"],
                    "wg_move_without_cap_sys_admin_ok": False,
                    "move_stderr": move_proc.stderr.strip(),
                }
            else:
                res = {
                    "netd_CapEff": st["CapEff"],
                    "netd_CapBnd": st["CapBnd"],
                    "wg_move_without_cap_sys_admin_ok": True,
                }
            os.write(w_pipe, json.dumps(res).encode())
            os._exit(0)
        except BaseException as exc:
            os.write(w_pipe, json.dumps({"error": repr(exc)}).encode())
            os._exit(1)

    os.close(w_pipe)
    netd_result = json.loads(os.read(r_pipe, 8192).decode())
    os.close(r_pipe)
    os.waitpid(netd_pid, 0)
    os.close(netctl_ns_fd)

    netctl_cfg_result = None
    if netd_result.get("wg_move_without_cap_sys_admin_ok"):
        p_sock.sendall(b"configure_wg_and_nft\n")
        netctl_cfg_result = json.loads(f_in.readline())
    else:
        p_sock.sendall(b"abort\n")

    p_sock.close()
    os.waitpid(pid, 0)

    # Also verify unprivileged FastAPI candidate (`stremhu-app` UID=10000, CapEff=0) can bind :7070 and create /dev/shm!
    app_r, app_w = os.pipe()
    app_pid = os.fork()
    if app_pid == 0:
        os.close(app_r)
        try:
            drop_all_privileges_and_verify(10000, 10000, os.getppid())
            from multiprocessing import shared_memory  # noqa: PLC0415

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 7070))
            s.close()
            shm = shared_memory.SharedMemory(create=True, size=4096, name="stremhu_poc_app_test")
            shm.close()
            shm.unlink()
            st_app = read_proc_status("self")
            os.write(
                app_w,
                json.dumps(
                    {
                        "fastapi_unprivileged_ok": True,
                        "Uid": st_app["Uid"],
                        "CapEff": st_app["CapEff"],
                        "CapBnd": st_app["CapBnd"],
                        "NoNewPrivs": st_app["NoNewPrivs"],
                    }
                ).encode(),
            )
            os._exit(0)
        except BaseException as exc:
            os.write(app_w, json.dumps({"fastapi_unprivileged_ok": False, "err": repr(exc)}).encode())
            os._exit(1)

    os.close(app_w)
    fastapi_result = json.loads(os.read(app_r, 8192).decode())
    os.close(app_r)
    os.waitpid(app_pid, 0)

    return {
        "netctl_bootstrap": ready_msg,
        "netd_without_cap_sys_admin": netd_result,
        "netctl_wg_and_nft_without_cap_sys_admin": netctl_cfg_result,
        "fastapi_uid10000_zero_caps": fastapi_result,
    }


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker-child":
        worker_entry(int(sys.argv[2]), sys.argv[3])
        return 0

    report: dict[str, object] = {}
    parent_status = read_proc_status("self")
    init_netns_ino = os.stat("/proc/self/ns/net").st_ino
    apparmor_attr = (
        Path("/proc/self/attr/current").read_text(encoding="utf-8", errors="replace").strip()
        if Path("/proc/self/attr/current").exists()
        else "MISSING"
    )
    apparmor_param = (
        Path("/sys/module/apparmor/parameters/enabled").read_text(encoding="utf-8").strip()
        if Path("/sys/module/apparmor/parameters/enabled").exists()
        else "MISSING"
    )

    # Verify NOT running with --privileged (all 41+ caps = 000001ffffffffff)
    cap_bnd_int = int(parent_status["CapBnd"], 16)
    has_sys_ptrace = bool(cap_bnd_int & (1 << CAP_SYS_PTRACE))
    is_privileged_container = cap_bnd_int >= 0x000001FFFFFFFFFF

    report["environment"] = {
        "init_netns_ino": init_netns_ino,
        "parent_CapEff": parent_status["CapEff"],
        "parent_CapBnd": parent_status["CapBnd"],
        "parent_Seccomp": parent_status.get("Seccomp"),
        "has_CAP_SYS_PTRACE": has_sys_ptrace,
        "is_privileged_container": is_privileged_container,
        "proc_self_attr_current": apparmor_attr,
        "apparmor_module_enabled": apparmor_param,
    }
    assert not is_privileged_container, "Refusing to run in --privileged container!"

    # Test 1A: B-MountFree + pre-opened netns_fd + 6-step Worker Privilege Drop
    p_sock, c_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(c_sock.fileno(), True)
    proc = subprocess.Popen(
        [sys.executable, __file__, "--worker-child", str(c_sock.fileno()), "normal"],
        pass_fds=(c_sock.fileno(),),
        stderr=subprocess.PIPE,
        text=True,
    )
    c_sock.close()
    f_in = p_sock.makefile("r")
    unshared_msg = json.loads(f_in.readline())

    # Open netns_fd BEFORE worker drops UID!
    netns_fd = os.open(f"/proc/{proc.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

    # Tell worker to drop privileges now
    p_sock.sendall(b"drop_now\n")
    dropped_msg = json.loads(f_in.readline())

    # Test external open of /proc/<worker_pid>/ns/net AFTER setresuid (expected EACCES without CAP_SYS_PTRACE!)
    # Wait: test this in a child that still hasn't finished waiting on worker? Wait: worker already exited 0,
    # so let's test post-setresuid /proc/<pid>/ns/net EACCES AND setns(netns_fd) while worker is alive in pdeathsig test!
    proc.wait(timeout=5)

    # Test setns(netns_fd) even AFTER worker has changed UID / exited, proving pre-opened netns_fd works!
    r_ns, w_ns = os.pipe()
    ns_test_pid = os.fork()
    if ns_test_pid == 0:
        os.close(r_ns)
        os.setns(netns_fd, os.CLONE_NEWNET)
        ino = os.stat("/proc/self/ns/net").st_ino
        os.write(w_ns, json.dumps({"setns_ok": True, "entered_ino": ino}).encode())
        os._exit(0)
    os.close(w_ns)
    setns_res = json.loads(os.read(r_ns, 4096).decode())
    os.close(r_ns)
    os.waitpid(ns_test_pid, 0)
    os.close(netns_fd)
    p_sock.close()

    report["test_1a_mountfree_and_privdrop"] = {
        "worker_netns_ino": unshared_msg["netns_ino"],
        "distinct_from_init_net": unshared_msg["netns_ino"] != init_netns_ino,
        "worker_exit_code": proc.returncode,
        "setns_via_preopened_fd": setns_res,
        "worker_audit": dropped_msg["audit"],
    }
    if "--test-1a-only" in sys.argv:
        print(json.dumps(report, indent=2))
        return 0

    # Test 1B: Fail-closed exit(79) before libtorrent import
    p2, c2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(c2.fileno(), True)
    proc_fail = subprocess.Popen(
        [sys.executable, __file__, "--worker-child", str(c2.fileno()), "fail_drop"],
        pass_fds=(c2.fileno(),),
        stderr=subprocess.PIPE,
        text=True,
    )
    c2.close()
    _ = json.loads(p2.makefile("r").readline())
    p2.sendall(b"drop_now\n")
    proc_fail.wait(timeout=5)
    p2.close()
    report["test_1b_fail_closed_exit_79"] = {
        "exit_code": proc_fail.returncode,
        "stderr": (proc_fail.stderr.read() if proc_fail.stderr else "").strip(),
        "passed": proc_fail.returncode == 79,
    }

    # Test 1C: Post-setresuid /proc/<worker_pid>/ns/net EACCES proof + PR_SET_PDEATHSIG delivery on parent SIGKILL
    r_pd, w_pd = os.pipe()
    r_sig, w_sig = os.pipe()
    os.set_inheritable(w_sig, True)
    mid_pid = os.fork()
    if mid_pid == 0:
        os.close(r_pd)
        os.close(r_sig)
        p3, c3 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(c3.fileno(), True)
        w_proc = subprocess.Popen(
            [sys.executable, __file__, "--worker-child", str(c3.fileno()), f"pdeathsig_wait:{w_sig}"],
            pass_fds=(c3.fileno(), w_sig),
        )
        c3.close()
        os.close(w_sig)
        f3 = p3.makefile("r")
        _ = json.loads(f3.readline())
        p3.sendall(b"drop_now\n")
        _ = json.loads(f3.readline())
        _ = f3.readline()  # wait until SIGTERM handler is armed!

        # Now worker is ALIVE as UID=10001 (dumpable=0)! Try opening /proc/<w_pid>/ns/net externally:
        post_uid_open_errno = None
        try:
            fd_tmp = os.open(f"/proc/{w_proc.pid}/ns/net", os.O_RDONLY)
            os.close(fd_tmp)
        except OSError as exc:
            post_uid_open_errno = exc.errno

        os.write(
            w_pd,
            json.dumps({"worker_pid": w_proc.pid, "post_uid_open_errno": post_uid_open_errno}).encode(),
        )
        os.close(w_pd)
        time.sleep(10.0)
        os._exit(0)

    os.close(w_pd)
    os.close(w_sig)
    mid_info = json.loads(os.read(r_pd, 4096).decode())
    os.close(r_pd)
    grandchild_pid = mid_info["worker_pid"]
    # Kill intermediate parent with SIGKILL -> kernel must send SIGTERM to UID=10001 grandchild!
    os.kill(mid_pid, signal.SIGKILL)
    os.waitpid(mid_pid, 0)
    sig_marker = os.read(r_sig, 256).decode("utf-8", errors="replace").strip()
    os.close(r_sig)
    time.sleep(0.1)
    # If main() is PID 1 (--init omitted), reparented dead grandchild is a zombie until PID 1 reaps it
    proc_state_before_reap = (
        read_proc_status(grandchild_pid).get("State") if Path(f"/proc/{grandchild_pid}/status").exists() else "GONE"
    )
    reaped_pid = 0
    reaped_status = None
    try:
        reaped_pid, raw_st = os.waitpid(grandchild_pid, os.WNOHANG)
        if reaped_pid == grandchild_pid:
            reaped_status = os.waitstatus_to_exitcode(raw_st)
    except ChildProcessError:
        pass
    grandchild_alive = Path(f"/proc/{grandchild_pid}").exists()
    report["test_1c_dumpable_eacces_and_pdeathsig"] = {
        "main_pid": os.getpid(),
        "post_setresuid_proc_ns_open_errno": mid_info["post_uid_open_errno"],
        "post_setresuid_proc_ns_open_errno_name": (
            errno.errorcode.get(mid_info["post_uid_open_errno"], "NONE")
            if mid_info["post_uid_open_errno"] is not None
            else "OPEN_SUCCEEDED"
        ),
        "pdeathsig_pipe_marker": sig_marker,
        "proc_state_before_pid1_reap": proc_state_before_reap,
        "reaped_by_pid1_exit_code": reaped_status,
        "grandchild_terminated_on_parent_sigkill": sig_marker.startswith("SIGTERM_RECEIVED") and not grandchild_alive,
    }

    # Test 1D: Rule 10 Privileged Helper + Post-Bootstrap CAP_SYS_ADMIN Elimination PoC
    report["test_1d_rule10_privileged_helper_zero_cap_sys_admin"] = run_rule10_helper_poc()

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
