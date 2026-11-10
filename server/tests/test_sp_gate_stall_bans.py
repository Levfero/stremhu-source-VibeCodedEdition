"""GSP10 harness: the H1 stall-ban counter and the decider's count-based hard checks.

Both are opt-in (controller plan key ``count_stall_bans``, decider rule key
``stall_ban_check``); without them the H1 chain must stay byte-identical, which the
reproduction of the committed gsp02-v4 decision checks.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
EVIDENCE = REPO / "docs/feature/stream-performance/evidence"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bans = load("sp_gate_stall_bans")

MIXED = {"peers": 8, "profiles": [{"rate_bytes_per_s": 3000000}] * 6 + [{"stall": True}, {"stall": True}]}
SLOWLIVE = {"peers": 8, "profiles": [{"rate_bytes_per_s": 3000000}] * 6 + [{"rate_bytes_per_s": 250000}] * 2}
FAST = {"peers": 8, "profiles": [{"rate_bytes_per_s": 4000000}]}


@pytest.mark.parametrize(("log", "expected"), [
    ("", 0),  # switch off: the product never writes the line
    ("[19:02:01] INFO     Relay stall watchdog: banned=0 expired=1 active=0   service.py:282", 0),
    ("[19:02:01] WARNING  Relay stall watchdog: banned=1 expired=0 active=1   service.py:282", 1),
    ("Relay stall watchdog: banned=1 expired=0 active=1\nother line\n"
     "Relay stall watchdog: banned=2 expired=0 active=3\n", 3),
    ("some other component: banned=5", 0),
])
def test_counter_sums_the_ban_lines(log, expected):
    assert bans.count_stall_bans(log) == expected


def test_stalled_peer_count_follows_the_controller_assignment():
    assert bans.stalled_peers(MIXED) == 2
    assert bans.stalled_peers(SLOWLIVE) == 0
    assert bans.stalled_peers(FAST) == 0
    # peer i gets profiles[i % len(profiles)]: one stalled entry of two over 8 peers = 4
    assert bans.stalled_peers({"peers": 8, "profiles": [{}, {"stall": True}]}) == 4


def protocol(bounded=("mixed",)) -> dict:
    return {"plan": {"profiles": {"mixed": MIXED, "slowlive": SLOWLIVE, "fast": FAST}},
            "decision_rule": {"stall_ban_check": {"counter": "app_log.stall_bans", "bounded_profiles": list(bounded)}}}


def record(label: str, profile: str, value, rep: int = 0, failed: bool = False) -> dict:
    out: dict = {"label": label, "rep": rep, "cell": {"profile": profile, "piece_size": 16 << 20, "bitrate_mbit": 30},
                 "app_log": {} if value is None else {"stall_bans": value}}
    if failed:
        out["failed"] = "TimeoutError: synthetic"
    return out


def cell_of(r: dict) -> str:
    return f"{r['cell']['profile']}/{r['cell']['piece_size'] >> 20}MiB/{r['cell']['bitrate_mbit']}Mbit"


def check(records: list[dict]) -> dict:
    return bans.stall_ban_check(protocol(), records, "CW", cell_of)


def test_clean_runs_pass():
    report = check([record("C0", "mixed", 0), record("C0", "slowlive", 0), record("C0", "fast", 0),
                    record("CW", "mixed", 2), record("CW", "mixed", 0, rep=1), record("CW", "slowlive", 0),
                    record("CW", "fast", 0)])
    assert report["pass"] and report["violations"] == []
    assert report["bans_total"] == {"C0": 0, "CW": 2}
    assert report["bounded_cell_runs"] == 2 and report["bounded_cell_runs_with_a_ban"] == 1


@pytest.mark.parametrize(("records", "field"), [
    ([record("CW", "slowlive", 1)], "zero_cell_runs_with_bans"),  # (a) false ban, live peers only
    ([record("CW", "fast", 1)], "zero_cell_runs_with_bans"),
    ([record("CW", "mixed", 3)], "bounded_cell_runs_over_limit"),  # (b) more bans than stalled peers
    ([record("C0", "mixed", 1)], "c0_runs_with_bans"),  # (c) wiring: switch off, no ban
    ([record("CW", "mixed", None)], "missing"),  # counter not collected
])
def test_each_hard_check_fails_the_candidate(records, field):
    report = check(records)
    assert not report["pass"] and report[field] == 1 and len(report["violations"]) == 1


def test_failed_runs_and_other_labels_are_not_judged():
    report = check([record("CW", "slowlive", 5, failed=True), record("C4", "slowlive", 5), record("CW", "fast", 0)])
    assert report["pass"] and report["bans_total"] == {"C0": 0, "CW": 0}


def run_decider(protocol_path: Path, complete: Path, out: Path) -> dict:
    subprocess.run([sys.executable, str(SCRIPTS / "decide_sp_gate_bootstrap.py"), str(protocol_path), str(complete),
                    str(out)], check=True, capture_output=True, text=True, timeout=120)
    return json.loads(out.read_text(encoding="utf-8"))


def test_without_the_rule_key_the_committed_gsp02_v4_decision_is_reproduced_byte_for_byte(tmp_path):
    out = tmp_path / "decision.json"
    decision = run_decider(EVIDENCE / "gsp02-v4-protocol.json", EVIDENCE / "gsp02-v4-complete.json", out)
    assert decision["decision"] == "PASS:C2"
    assert out.read_bytes() == (EVIDENCE / "gsp02-v4-decision.json").read_bytes()
    assert all("stall_ban_check" not in result for result in decision["candidates"].values())


def test_with_the_rule_key_a_missing_counter_fails_every_candidate(tmp_path):
    # gsp02-v4 runs carry no ban counter: the opt-in check must not pass them silently.
    proto = json.loads((EVIDENCE / "gsp02-v4-protocol.json").read_text(encoding="utf-8"))
    proto["decision_rule"]["resamples"] = 200
    proto["decision_rule"]["stall_ban_check"] = {"counter": "app_log.stall_bans", "bounded_profiles": ["mixed"]}
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(proto), encoding="utf-8")
    decision = run_decider(path, EVIDENCE / "gsp02-v4-complete.json", tmp_path / "decision.json")
    assert decision["decision"] == "FAIL"
    for result in decision["candidates"].values():
        assert result["stall_ban_check"]["missing"] > 0 and result["pass"] is False


APP_LOG = ("[20:01:02] INFO     Application startup complete\n"
           "[20:01:09] WARNING  Relay stall watchdog: banned=2 expired=0 active=2   service.py:282\n")


@pytest.mark.parametrize(("opt_in", "log", "expected"), [
    (False, APP_LOG, None),  # key absent: the record is exactly as before (no stall_bans field)
    (True, APP_LOG, 2),
    (True, "[20:01:02] INFO     Application startup complete\n", 0),
])
def test_controller_counts_bans_only_when_the_plan_opts_in(tmp_path, monkeypatch, opt_in, log, expected):
    controller = load("benchmark_sp_playback_controller")
    monkeypatch.syspath_prepend(str(SCRIPTS))  # the controller runs from scripts/ and imports the helper lazily

    def shared_dir(args) -> Path:
        for i, arg in enumerate(args):
            if arg == "-v" and str(args[i + 1]).endswith(":/sp/shared"):
                return Path(str(args[i + 1]).rsplit(":/sp/shared", 1)[0])
        raise AssertionError("no /sp/shared mount")

    def fake_sh(*args, check=True, capture=True, timeout=600):
        if "logs" in args:
            return '{"ready": true}'
        if "/sp/scripts/sp_app_fixture.py" in args:
            return '{"targets": 1}'
        if "/sp/scripts/benchmark_sp_playback.py" in args:
            result = {"start": {"bytes_received": 0}, "first_byte_monotonic": 2.0, "t_start_monotonic": 1.0}
            (shared_dir(args) / "result.json").write_text(json.dumps(result), encoding="utf-8")
            return '{"client": "done"}'
        return ""

    class Sampler:
        def __init__(self, container):
            self.halt = SimpleEvent()

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def summary(self):
            return {"samples": 0}

    class SimpleEvent:
        def set(self):
            pass

    real_run = subprocess.run

    def fake_run(args, *rest, **kwargs):
        if list(args[:2]) == ["docker", "logs"]:
            return subprocess.CompletedProcess(args, 0, stdout=log, stderr="")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(controller, "sh", fake_sh)
    monkeypatch.setattr(controller, "wait_ready", lambda *args: 0.1)
    monkeypatch.setattr(controller, "StatsSampler", Sampler)
    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    plan = {"tool_image": "tool", "total_size": 1 << 30, "profiles": {"mixed": MIXED}}
    if opt_in:
        plan["count_stall_bans"] = True
    cell = {"profile": "mixed", "piece_size": 16 << 20, "bitrate_mbit": 30}
    record = controller.run_one(plan, "CW", "image", cell, 0, 0, tmp_path)
    assert "failed" not in record, record.get("failed")
    assert record["app_log"].get("stall_bans") == expected
    assert ("stall_bans" in record["app_log"]) is opt_in
