"""GSP04 harness: synthetic payload, the bulk reader's byte-exact check and the decider's hard checks.

All of it is opt-in (torrent ``payload``, controller ``downloaded``/``bulk_read_args``, decider
``hard_checks``). Without the keys the H1 chain must stay byte-identical: the zero-payload
torrent equals the one the registered gates used, and the committed gsp02-v4 decision
reproduces from its evidence.
"""

import asyncio
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
EVIDENCE = REPO / "docs/feature/stream-performance/evidence"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"gsp04_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


synthetic = load(SCRIPTS / "fixtures/synthetic_torrent.py")
bulk = load(SCRIPTS / "benchmark_sp_bulk_read.py")
decider = load(SCRIPTS / "decide_sp_gate_bootstrap.py")


def test_zero_payload_torrent_is_the_one_the_registered_gates_used():
    # Digest of the same call with the pre-GSP04 synthetic_torrent.py (feature/stream-performance 06b790d).
    torrent = synthetic.build("sp-1024k.mkv", 1 << 20, 8 << 20, "http://10.255.255.1:6969/announce")
    assert hashlib.sha256(torrent.torrent_bytes).hexdigest() == (
        "4e516344e0620840b21d001ec5e11a877a07bf9fb717f681e86c3b7e1cd5e6e3")


def serve(body: bytes, first: int, last: int, size: int) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(206, headers={"content-range": f"bytes {first}-{last}/{size}"},
                              stream=httpx.ByteStream(body))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def read(body: bytes, start: int, length: int, payload: str, claimed_length: int | None = None) -> dict:
    size = 4 << 20
    last = start + (claimed_length or length) - 1

    async def scenario() -> dict:
        async with serve(body, start, last, size) as client:
            return await bulk.read(client, "https://app.invalid/x", start, length, 1 << 16, payload)

    return asyncio.run(scenario())


START, LENGTH = 100_003, 300_000


def test_bulk_reader_accepts_only_the_byte_exact_body():
    exact = synthetic.payload_bytes("block-index", START, LENGTH)
    result = read(exact, START, LENGTH, "block-index")
    assert result["complete"] and result["payload_errors"] == 0 and result["read_s"] is not None
    assert result["bytes_received"] == result["expected_bytes"] == LENGTH


def swapped_blocks(data: bytes, at: int) -> bytes:
    block = synthetic.BLOCK
    return data[:at] + data[at + block : at + 2 * block] + data[at : at + block] + data[at + 2 * block :]


@pytest.mark.parametrize("damage", ["two-blocks-swapped", "one-byte-shift", "one-block-repeated"])
def test_bulk_reader_flags_a_body_with_the_right_length_but_wrong_order(damage):
    whole = synthetic.payload_bytes("block-index", START, LENGTH + 1)
    exact = whole[:LENGTH]
    body = {"two-blocks-swapped": swapped_blocks(exact, 5 * synthetic.BLOCK),
            "one-byte-shift": whole[1:],
            "one-block-repeated": exact[: 6 * synthetic.BLOCK] + exact[5 * synthetic.BLOCK : LENGTH - synthetic.BLOCK],
            }[damage]
    assert len(body) == LENGTH
    result = read(body, START, LENGTH, "block-index")
    assert result["bytes_received"] == LENGTH
    assert result["payload_errors"] > 0
    assert result["first_mismatch_offset"] is not None


def test_bulk_reader_flags_a_short_body_and_a_non_zero_byte_in_a_zero_file():
    short = read(bytes(LENGTH - 1), START, LENGTH, "zero", claimed_length=LENGTH)
    assert not short["complete"] and short["payload_errors"] >= 1 and short["read_s"] is None
    dirty = bytearray(LENGTH)
    dirty[LENGTH // 2] = 1
    assert read(bytes(LENGTH), START, LENGTH, "zero")["payload_errors"] == 0
    assert read(bytes(dirty), START, LENGTH, "zero")["payload_errors"] == 1


def gate_records(rng: random.Random) -> list[dict]:
    records = []
    for label, mean in (("C0", 2.0), ("C4", 1.5)):
        for rep in range(10):
            records.append({
                "label": label, "rep": rep, "cell": {"name": "dl/1MiB/30Mbit", "profile": "warm",
                                                     "piece_size": 1 << 20, "bitrate_mbit": 30},
                "warm": {"payload_errors": 0, "error_count": 0},
                "result": {"bulk": {"read_s": mean + rng.uniform(-0.05, 0.05), "payload_errors": 0},
                           "start": {"ttfb_s": 0.05 + rng.uniform(-0.01, 0.01), "stall_s": 0.0}},
                "app_log": {"app_tracebacks": 0},
            })
    return records


def decide(tmp_path: Path, monkeypatch, records: list[dict], hard_checks: dict | None) -> dict:
    rule = {"primary_metric": "bulk.read_s", "candidates": ["C4"],
            "key_cells": ["dl/1MiB/30Mbit"], "improved_required": 1, "alpha": 0.05, "resamples": 300,
            "seed": 1, "guard_metrics": ["bulk.read_s", "start.ttfb_s", "start.stall_s"]}
    if hard_checks is not None:
        rule["hard_checks"] = hard_checks
    protocol, complete, out = tmp_path / "protocol.json", tmp_path / "complete.json", tmp_path / "decision.json"
    protocol.write_text(json.dumps({"id": "gsp04-test", "decision_rule": rule}), encoding="utf-8")
    complete.write_text(json.dumps({"records": records}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["decide", str(protocol), str(complete), str(out)])
    decider.main()
    return json.loads(out.read_text(encoding="utf-8"))


HARD = {"max_failed_runs": 0, "zero_every_run": ["warm.payload_errors", "result.bulk.payload_errors"]}


def test_hard_checks_pass_a_clean_faster_candidate(tmp_path, monkeypatch):
    without = decide(tmp_path, monkeypatch, gate_records(random.Random(3)), None)
    assert without["decision"] == "PASS:C4" and "hard_checks" not in without
    clean = decide(tmp_path, monkeypatch, gate_records(random.Random(3)), HARD)
    assert clean["decision"] == "PASS:C4"
    assert clean["hard_checks"]["C4"]["pass"] is True


@pytest.mark.parametrize("fault", ["failed-c0-run", "payload-error-in-c0", "payload-error-in-c4",
                                   "missing-warm-check"])
def test_hard_checks_fail_the_gate_on_any_failed_run_or_non_exact_payload(tmp_path, monkeypatch, fault):
    records = gate_records(random.Random(3))
    c0 = next(r for r in records if r["label"] == "C0")
    c4 = next(r for r in records if r["label"] == "C4")
    if fault == "failed-c0-run":
        c0["failed"] = "RuntimeError: warm-up read not complete and byte-exact"
    elif fault == "payload-error-in-c0":
        c0["result"]["bulk"]["payload_errors"] = 1
    elif fault == "payload-error-in-c4":
        c4["result"]["bulk"]["payload_errors"] = 2
    else:
        del c4["warm"]
    # Without the hard checks the candidate would pass (a failed C0 run is even "in its favour").
    assert decide(tmp_path, monkeypatch, records, None)["decision"] == "PASS:C4"
    result = decide(tmp_path, monkeypatch, records, HARD)
    assert result["decision"] == "FAIL"
    assert result["hard_checks"]["C4"]["pass"] is False


def test_committed_gsp02_v4_decision_reproduces_byte_identically(tmp_path, monkeypatch):
    out = tmp_path / "gsp02-v4-decision.json"
    monkeypatch.setattr(sys, "argv", ["decide", str(EVIDENCE / "gsp02-v4-protocol.json"),
                                      str(EVIDENCE / "gsp02-v4-complete.json"), str(out)])
    decider.main()
    assert out.read_bytes() == (EVIDENCE / "gsp02-v4-decision.json").read_bytes()
