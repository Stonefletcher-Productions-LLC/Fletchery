"""Dead ends: queued work that nothing in Fletchery will ever start, found and repaired or surfaced.

LifeShare 2026-11-14: every worker sat idle for hours with 91 queued tasks and a healthy backend, because none could
start and nothing said so:
- 22 tasks depended on issues Fletchery could not see. Some had lost the plain task label while keeping `<label>:queued`
  (a thread held them, then re-queued them by the state label only); some were done by hand in a PR that merged
  without "Closes #n"; some pointed at a PR that was closed unmerged. All showed "depends on missing issue", which no
  one reads.
- 16 tasks sat at "repairing PR #n: waits for new feedback". Their PR failed its PR check, the fixer read the report
  and ended without a new head, and a failed check at an unchanged head never produced feedback again.

This module decides what to do about each; loop.Engine applies it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import graph

# A PR whose PR-check report a fixer already took, still failing at the same head, gets one fresh repair after this long.
# Short, because the usual cause is drift against a fast-moving base ("Merge the PR onto the base failed").
CHECK_RETRY_HOURS = 3
NEEDS = "needs a person: "


def _older_than(stamp: str, hours: float) -> bool:
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at >= timedelta(hours=hours)


def check_failed_at_head(pr, chk: dict) -> bool:
    return bool(pr is not None and chk and chk.get("head") == pr.head_sha and chk.get("passed") is False)


def check_report_taken(chk: dict, seen: str | None) -> bool:
    """A fixer was already handed this check report (or anything newer) and ended without a new head."""
    return bool(seen and str(chk.get("at", "")) <= seen)


def check_repair_due(pr, chk: dict, seen: str | None, hours: float = CHECK_RETRY_HOURS) -> bool:
    """One fresh repair for a PR stuck at a failed PR check: the report was taken, no new head came, time has passed."""
    return (check_failed_at_head(pr, chk) and check_report_taken(chk, seen) and not chk.get("retried")
            and _older_than(chk.get("at", ""), hours))


def check_stuck_wait(pr, chk: dict, seen: str | None, hours: float = CHECK_RETRY_HOURS) -> str:
    """Why a queued task whose PR failed its PR check is not moving, or "" when a fixer can still take it."""
    if not check_failed_at_head(pr, chk) or not check_report_taken(chk, seen):
        return ""
    where = chk.get("machine") or "the build PC"
    if chk.get("retried"):
        return (f"{NEEDS}PR #{pr.number} fails its PR check on {where} at head {pr.head_sha[:10]}, and its repairs "
                "(including a fresh one) ended without a new commit. Read the last PR-check comment on the PR.")
    try:
        at = datetime.fromisoformat(str(chk.get("at", "")).replace("Z", "+00:00"))
        when = (at + timedelta(hours=hours)).isoformat(timespec="minutes")[:16].replace("T", " ") + "Z"
    except ValueError:
        when = "soon"
    return (f"PR #{pr.number} failed its PR check on {where} and the repair ended without a new commit; "
            f"one fresh repair starts at {when}")


def fresh_check_text(chk: dict, base: str) -> str:
    return ("Fresh repair: this PR failed its PR check at the current head and the last repair ended without a new "
            f"commit, so nothing would ever re-check it. Merge the latest {base} into this branch and resolve any "
            "conflicts, fix what the PR check reported (the last \"PC check failed\" comment on the PR has the log), "
            "re-run the issue's named checks, and push. Check summary: "
            + " ".join(str(chk.get("output") or chk.get("summary") or "failed").split())[-1500:])


# ---- dependencies Fletchery can't see ----

def classify_dependency(info: dict | None) -> tuple[str, str]:
    """What to do about an open dependency that is not one of this project's task issues.

    `info` is GitHub's issue object (PRs are issues too, with "pull_request"), plus "merged" for a PR. Returns
    (action, text): "relabel" (it lost the plain task label but kept a state label: give it back), "wait" (a real
    wait, e.g. an open PR), or "dead" (nothing will ever finish it: a person must fix the Depends on line or the issue).
    A closed and done dependency never reaches here (it counts as finished)."""
    if not info:
        return "dead", "does not exist (or Fletchery can't read it)"
    labels = [(l.get("name") if isinstance(l, dict) else str(l)) for l in info.get("labels") or []]
    if "pull_request" in info:
        if info.get("state") == "open":
            return "wait", "is an open PR (it counts as done once it merges)"
        return "dead", "is a PR that was closed without merging"
    if info.get("state") == "closed":
        return "dead", f"was closed as {info.get('state_reason') or 'not done'}"
    if graph.PREFIX in labels:
        return "wait", "is a task here (it appears next pass)"
    if any(graph.is_status_label(l) for l in labels):
        return "relabel", f"has `{next(l for l in labels if graph.is_status_label(l))}` but lost the `{graph.PREFIX}` label"
    return "dead", f"is open but not a Fletchery task (no `{graph.PREFIX}` label)"


def dead_end_reason(dead: list[tuple[int, str]]) -> str:
    parts = "; ".join(f"#{d}, which {why}" for d, why in dead)
    return (f"{NEEDS}depends on {parts}. Point the Depends on line at the work that replaces it, label it "
            f"`{graph.PREFIX}`, or close it if it is done")


def dead_end_comment(dead: list[tuple[int, str]]) -> str:
    lines = "\n".join(f"- #{d} {why}" for d, why in dead)
    return (f"Fletchery: this task can't start, and nothing will start it, because a prerequisite will never finish "
            f"here:\n\n{lines}\n\nFix it in one of these ways: point `Depends on:` at the issue or PR that replaces "
            f"it; label the prerequisite `{graph.PREFIX}` so Fletchery works it; or close it as completed if the work "
            "already landed. The task starts on the next pass after that.")
