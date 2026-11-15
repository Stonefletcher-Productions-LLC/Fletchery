"""Idle-slot alert and flow report (Fletchery #186). Run: python fletchery/test_flow.py"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

import flow  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


T0 = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
WAITING = {  # real reasons from LifeShare's status page, 2026-11-15
    1964: "waits for #1737 (queued), #1738 (queued)",
    1907: "waits for #1889 (queued)",
    1934: "needs a person: PR #1962 fails its PR check on GAMING-PC at head aac61bdda0, and its repairs ended",
    2000: "repairing PR #1990: waits for new feedback or a free repair slot",
    1500: "waits for a free agent: every suitable one is off, paused or busy",
    1501: "depends on missing issue #42",
    1502: "on hold: Fletchery is paused",
}


def test_causes():
    c = flow.causes(WAITING)
    check(c["waits for a dependency to merge"] == 2, "dependency waits")
    check(c["needs a person"] == 1 and c["waits for a repair"] == 1, "people and repairs")
    check(c["waits for an agent"] == 1 and c["dependency problem"] == 1 and c["on hold"] == 1, "agents, problems, holds")
    check(flow.top_text(c).startswith("waits for a dependency to merge (2)"), "the top cause comes first")


def test_idle():
    rec = {}
    a, r = flow.idle_step(rec, T0, 9, 91, 0, False, WAITING)
    check(a == "" and rec.get("idle_since"), "idle starts quietly")
    a, r = flow.idle_step(rec, T0 + timedelta(minutes=20), 9, 91, 0, False, WAITING)
    check(a == "", "no alert before 30 minutes")
    a, r = flow.idle_step(rec, T0 + timedelta(minutes=31), 9, 91, 0, False, WAITING)
    check("9 worker slot(s) idle for 31 min with 91 task(s) queued" in a and flow.MARKER in a, "one alert after 30 min")
    check("waits for a dependency to merge**: 2" in a and "#1907" in a, "it names causes, counts and an example")
    a, r = flow.idle_step(rec, T0 + timedelta(minutes=40), 9, 91, 0, False, WAITING)
    check(a == "" and r == "", "and only once")
    minutes = rec["minutes"]["2026-11-15"]
    check(round(minutes["waits for a dependency to merge"]) == 9 * 40, "idle slot-minutes go to the top cause")
    a, r = flow.idle_step(rec, T0 + timedelta(minutes=45), 8, 91, 1, False, WAITING)
    check(r and "starting tasks again" in r and "idle_since" not in rec, "a recovery note when work starts")

    rec = {}
    for m in (0, 40):
        a, _ = flow.idle_step(rec, T0 + timedelta(minutes=m), 9, 91, 0, True, WAITING)
    check(a == "" and rec["minutes"]["2026-11-15"].get("on hold"), "a deliberate hold is counted, not alerted")
    rec = {}
    for m in (0, 40):
        a, _ = flow.idle_step(rec, T0 + timedelta(minutes=m), 9, 0, 0, False, {})
    check(a == "" and not rec.get("minutes"), "free slots with an empty queue are not idleness")
    rec = {"last": (T0 - timedelta(hours=5)).isoformat()}
    flow.idle_step(rec, T0, 2, 5, 0, False, WAITING)
    check(sum(rec["minutes"]["2026-11-15"].values()) == 60, "a long gap (restart) counts at most 30 minutes")


def test_hook():
    posted = []
    import rounds
    old = rounds.alert_comment
    rounds.alert_comment = lambda gh, d, text: posted.append(text) or 1
    try:
        eng = SimpleNamespace(state=SimpleNamespace(flow={"idle_since": (T0 - timedelta(hours=1)).isoformat(),
                                                         "last": (T0 - timedelta(minutes=1)).isoformat()}),
                              ctx=SimpleNamespace(gh=None, log=lambda m: None), state_dir=Path("."))
        flow.idle_pass(eng, 9, 91, 0, False, WAITING)
    finally:
        rounds.alert_comment = old
    check(len(posted) == 1 and flow.MARKER in posted[0], "the engine hook posts to the alerts issue")


def test_report():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        now = datetime.now(timezone.utc)
        iso = lambda h: (now - timedelta(hours=h)).isoformat(timespec="seconds")  # noqa: E731
        rows = [{"at": iso(1), "task": 7, "result": "pr"}, {"at": iso(2), "task": 8, "result": "pr"},
                {"at": iso(3), "task": 7, "result": "fixed"}, {"at": iso(3), "task": 7, "result": "retry_fix"},
                {"at": iso(4), "task": 9, "result": "gave_up"}, {"at": iso(40), "task": 9, "result": "pr"}]
        (d / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        flow.record_round(d, 190, {"result": "landed", "at": iso(2), "started": iso(4),
                                   "left_out": [{"pr": 3, "why": "x"}]}, [1, 2, 3])
        flow.record_round(d, 191, {"result": "failed", "at": iso(1), "started": iso(2)}, [4])
        tasks = {7: SimpleNamespace(created_at=iso(11)), 8: SimpleNamespace(created_at=iso(4))}
        rec = {"minutes": {now.date().isoformat(): {"waits for a dependency to merge": 540}}}
        text = "\n".join(flow.report(d, tasks, rec, now))
    check("PRs opened: 2 · repairs: 2 (1.0 per PR) · gave up: 1" in text, "PRs, repairs and give-ups in 24 h")
    check("Merge rounds: 2 (1 landed, 2 PRs in), median" in text, "rounds and landed PRs, left-out ones not counted")
    check("median 2.0 h, p90 10.0 h" in text or "median 10.0 h" in text, "issue-to-PR time")
    check("waits for a dependency to merge 9.0 h" in text, "idle slot-hours by cause")


if __name__ == "__main__":
    test_causes()
    test_idle()
    test_hook()
    test_report()
    print(f"RESULT: {'FAIL' if FAILS else 'PASS'} (flow report, {len(FAILS)} failed)")
    sys.exit(1 if FAILS else 0)
