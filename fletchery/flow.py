"""Is work flowing? Idle slots with a queue, why they are idle, and a daily flow report (Fletchery #186).

LifeShare 2026-11-15 02:30Z: 9 of 10 worker slots were empty with 91 tasks queued (67 waiting on a dependency's PR to
merge, 17 needing a person, 7 waiting on a repair) and nothing said so. Earlier stalls (11-03, 11-14 twice) were each
found by a person and fixed one cause at a time (#172, #178, #179, #181). This module gives every stall the same
treatment, whatever its cause:

- each pass, `idle_pass` counts the free slot-minutes and files them under the most common reason the queued tasks
  give for not starting (`cause`);
- when slots stay free for IDLE_ALERT_MINUTES while tasks are queued and nothing starts, it comments once on the
  "Fletchery round alerts" issue with the top causes and their counts, and once more when work starts again;
- `report` turns the ledger and the round history into the "Flow" section of the status page: PRs opened, rounds and
  PRs landed, repairs per PR, give-ups, issue-to-PR time, and idle slot-hours by cause.

Pure functions over plain data plus two small hooks into loop.Engine, so it can be tested without GitHub."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDLE_ALERT_MINUTES = 30
MARKER = "<!-- fletchery-idle-slots -->"
KEEP_DAYS = 14

# Bucket names, in the order a person should look at them.
CAUSES = ("needs a person", "waits for a dependency to merge", "dependency problem", "waits for a repair",
          "waits for an agent", "waits for a machine or lease", "on hold", "other")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def cause(why: str) -> str:
    """The bucket for one queued task's reason for not starting (loop.Engine's `waiting` text)."""
    w = (why or "").lower()
    if "needs a person" in w or "needs you" in w:
        return "needs a person"
    if "missing issue" in w or "cycle" in w or "prerequisite status unavailable" in w:
        return "dependency problem"
    if "repair" in w or "fixer" in w:
        return "waits for a repair"
    if w.startswith("waits for #") or w.startswith("waits for ") and "#" in w:
        return "waits for a dependency to merge"
    if "agent" in w:
        return "waits for an agent"
    if "lease" in w or "machine" in w or "disk" in w or "resource" in w or "heavy" in w:
        return "waits for a machine or lease"
    if w.startswith("on hold") or "paused" in w or "budget" in w or "backlog" in w or "merge priority" in w:
        return "on hold"
    return "other"


def causes(waiting: dict) -> Counter:
    return Counter(cause(w) for w in waiting.values())


def top_text(counts: Counter, n: int = 3) -> str:
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], CAUSES.index(kv[0]) if kv[0] in CAUSES else 99))[:n]
    return ", ".join(f"{k} ({v})" for k, v in rows) or "no reason recorded"


def idle_step(rec: dict, now: datetime, free: int, queued: int, started: int, held: bool,
              waiting: dict) -> tuple[str, str]:
    """Advance the idle record by one pass. Returns (alert, recovered): a comment to post, or "" for each.

    `rec` (state.flow) keeps: last (the previous pass time), idle_since, alerted, minutes {day: {cause: slot-min}}."""
    last = _parse(rec.get("last"))
    rec["last"] = now.isoformat(timespec="seconds")
    counts = causes(waiting)
    if last is not None and free > 0 and queued > 0:
        minutes = max(0.0, min((now - last).total_seconds() / 60, 30.0))  # a long gap is a restart, not idleness
        bucket = "on hold" if held else (max(counts, key=lambda k: (counts[k], -CAUSES.index(k))) if counts else "other")
        day = rec.setdefault("minutes", {}).setdefault(now.date().isoformat(), {})
        day[bucket] = round(day.get(bucket, 0) + minutes * free, 1)
    for d in [d for d in rec.get("minutes", {}) if d < (now - timedelta(days=KEEP_DAYS)).date().isoformat()]:
        del rec["minutes"][d]
    idle = free > 0 and queued > 0 and started == 0 and not held
    if not idle:
        was = rec.pop("idle_since", None)
        alerted = rec.pop("alerted", None)
        if was and alerted and started > 0:
            return "", (f"Workers are starting tasks again (slots were idle since {was}).\n\n{MARKER}")
        return "", ""
    since = rec.setdefault("idle_since", now.isoformat(timespec="seconds"))
    age = (now - (_parse(since) or now)).total_seconds() / 60
    if rec.get("alerted") or age < IDLE_ALERT_MINUTES:
        return "", ""
    rec["alerted"] = now.isoformat(timespec="seconds")
    lines = [f"**{free} worker slot(s) idle for {int(age)} min with {queued} task(s) queued, and none can start.**", "",
             "Why the queued tasks are waiting:", ""]
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        example = next((f"#{n}: {w[:160]}" for n, w in sorted(waiting.items()) if cause(w) == k), "")
        lines.append(f"- **{k}**: {v}" + (f" (e.g. {example})" if example else ""))
    lines += ["", "Top causes: " + top_text(counts) + ".", "", MARKER]
    return "\n".join(lines), ""


def idle_pass(engine, free: int, queued: int, started: int, held: bool, waiting: dict) -> None:
    """loop.Engine hook, once per pass after dispatch."""
    rec = engine.state.flow
    alert, recovered = idle_step(rec, _now(), free, queued, started, held, waiting)
    text = alert or recovered
    if not text:
        return
    engine.ctx.log("flow: " + text.splitlines()[0].strip("*"))
    try:
        import rounds
        rounds.alert_comment(engine.ctx.gh, engine.state_dir, text)
    except Exception as e:
        engine.ctx.log(f"flow: could not post the idle-slot alert: {type(e).__name__}: {e}")


# ---- rounds history and the daily report ----

def record_round(state_dir, round_no, res: dict, batch_numbers: list[int]) -> None:
    """Append one finished round to <state>/rounds.jsonl: when, result, and which PRs it landed."""
    out_of = {str(i.get("pr")) for i in (res.get("left_out") or []) + (res.get("deferred") or []) if isinstance(i, dict)}
    landed = [n for n in batch_numbers if str(n) not in out_of] if res.get("result") == "landed" else []
    row = {"at": res.get("at") or _now().isoformat(timespec="seconds"), "started": res.get("started", ""),
           "round": round_no, "result": res.get("result", ""), "landed": landed}
    with open(Path(state_dir) / "rounds.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path, since: datetime) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                at = _parse(r.get("at"))
                if at is not None and at >= since:
                    rows.append(r)
    except OSError:
        pass
    return rows


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else 0.0


def summary(ledger: list[dict], rounds_rows: list[dict], created: dict[int, str], idle_minutes: dict) -> dict:
    prs = [r for r in ledger if r.get("result") == "pr"]
    repairs = sum(1 for r in ledger if r.get("result") in ("fixed", "retry_fix"))
    lead = []
    for r in prs:
        a, b = _parse(created.get(r.get("task"))), _parse(r.get("at"))
        if a and b and b >= a:
            lead.append((b - a).total_seconds() / 3600)
    landed_rounds = [r for r in rounds_rows if r.get("result") == "landed"]
    hours = []
    for r in rounds_rows:
        a, b = _parse(r.get("started")), _parse(r.get("at"))
        if a and b and b >= a:
            hours.append((b - a).total_seconds() / 3600)
    idle = Counter()
    for day in idle_minutes.values():
        idle.update(day)
    return {"prs": len(prs), "repairs": repairs, "gave_up": sum(1 for r in ledger if r.get("result") == "gave_up"),
            "rounds": len(rounds_rows), "rounds_landed": len(landed_rounds),
            "landed": sum(len(r.get("landed") or []) for r in landed_rounds),
            "round_hours": round(_pct(hours, 0.5), 1),
            "lead_median": round(_pct(lead, 0.5), 1), "lead_p90": round(_pct(lead, 0.9), 1),
            "idle_hours": {k: round(v / 60, 1) for k, v in idle.most_common()}}


def report(state_dir, tasks: dict, rec: dict, now: datetime | None = None) -> list[str]:
    """Lines for the status page's "Flow (last 24 h)" section."""
    now = now or _now()
    since = now - timedelta(hours=24)
    d = Path(state_dir)
    created = {n: t.created_at for n, t in tasks.items() if getattr(t, "created_at", "")}
    days = {k: v for k, v in (rec.get("minutes") or {}).items() if k >= since.date().isoformat()}
    s = summary(_read_jsonl(d / "ledger.jsonl", since), _read_jsonl(d / "rounds.jsonl", since), created, days)
    per = f"{s['repairs'] / s['prs']:.1f}" if s["prs"] else "n/a"
    idle = ", ".join(f"{k} {v} h" for k, v in s["idle_hours"].items()) or "none"
    lines = ["## Flow (last 24 h)",
             f"- PRs opened: {s['prs']} · repairs: {s['repairs']} ({per} per PR) · gave up: {s['gave_up']}",
             f"- Merge rounds: {s['rounds']} ({s['rounds_landed']} landed, {s['landed']} PRs in), median {s['round_hours']} h",
             f"- Issue opened to PR (open tasks): median {s['lead_median']} h, p90 {s['lead_p90']} h",
             f"- Idle slot-hours while tasks were queued (today and yesterday): {idle}"]
    if rec.get("idle_since"):
        lines.append(f"- **Slots idle with a queue since {rec['idle_since']}**")
    return lines + [""]
