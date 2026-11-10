"""GSP10 (SP10 stall watchdog): ban counter for H1 runs and the gate's hard checks.

Opt-in only; absent keys leave the H1 chain byte-identical:
- controller plan key ``count_stall_bans: true`` -> ``record["app_log"]["stall_bans"]``: the sum of
  ``banned=<n>`` over the product's "Relay stall watchdog: ..." log lines (counts only; the product
  never logs an address). The product logs a new ban at WARNING, so it is visible with NODE_ENV=prod.
- decider rule key ``stall_ban_check: {"counter": "app_log.stall_bans", "bounded_profiles": ["mixed"]}``:
  (a) every non-failed candidate run in a cell whose profile is not bounded (no stalled peer: fast,
      thin, slowlive) has 0 bans;
  (b) every non-failed candidate run in a bounded profile has at most as many bans as the profile has
      stalled peers (peer i of a profile gets ``profiles[i % len(profiles)]``, as in the controller);
  (c) every non-failed C0 run has 0 bans (wiring: the switch is off there);
  a missing counter in any non-failed run fails the check. Runs with at least one ban in bounded
  cells are reported, not required (attribution).

Limit: counts cannot tell a stalled peer's ban from a serving peer's ban inside a stalled cell;
(b) bounds the damage, (a) is the false-ban check.
"""

import re

BAN_LINE = re.compile(r"Relay stall watchdog: banned=(\d+)")


def count_stall_bans(log_text: str) -> int:
    return sum(int(match.group(1)) for match in BAN_LINE.finditer(log_text))


def stalled_peers(profile: dict) -> int:
    profiles = profile["profiles"]
    return sum(1 for i in range(profile["peers"]) if profiles[i % len(profiles)].get("stall"))


def _value(record: dict, dotted: str):
    value = record
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def stall_ban_check(protocol: dict, records: list[dict], candidate: str, cell_of) -> dict:
    """Hard checks (a)-(c) for one candidate against C0; ``cell_of`` names a record's cell."""
    spec = protocol["decision_rule"]["stall_ban_check"]
    counter = spec.get("counter", "app_log.stall_bans")
    bounded = set(spec.get("bounded_profiles", []))
    limits = {name: stalled_peers(profile) for name, profile in protocol["plan"]["profiles"].items()}
    ok = [r for r in records if not r.get("failed") and r["label"] in ("C0", candidate)]
    report: dict = {"counter": counter, "missing": 0, "c0_runs_with_bans": 0, "zero_cell_runs_with_bans": 0,
                    "bounded_cell_runs_over_limit": 0, "bounded_cell_runs_with_a_ban": 0,
                    "bounded_cell_runs": 0, "bans_total": {"C0": 0, candidate: 0}, "violations": []}
    for record in ok:
        value = _value(record, counter)
        cell = cell_of(record)
        if value is None:
            report["missing"] += 1
            report["violations"].append(f"{record['label']} {cell} rep{record.get('rep')}: counter missing")
            continue
        report["bans_total"][record["label"]] += value
        profile = record["cell"]["profile"]
        if record["label"] == "C0":
            if value:
                report["c0_runs_with_bans"] += 1
                report["violations"].append(f"C0 {cell} rep{record.get('rep')}: {value} bans with the switch off")
        elif profile in bounded:
            report["bounded_cell_runs"] += 1
            report["bounded_cell_runs_with_a_ban"] += value > 0
            if value > limits[profile]:
                report["bounded_cell_runs_over_limit"] += 1
                report["violations"].append(
                    f"{candidate} {cell} rep{record.get('rep')}: {value} bans > {limits[profile]} stalled peers")
        elif value:
            report["zero_cell_runs_with_bans"] += 1
            report["violations"].append(f"{candidate} {cell} rep{record.get('rep')}: {value} false bans")
    report["pass"] = not report["violations"]
    return report
