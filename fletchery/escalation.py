"""The escalation desk: nothing reaches Needs you until a strong agent has had a go at it.

LifeShare 2026-11-15: 31 items sat in Needs you and every worker was idle. 17 were queued tasks whose PR had failed a
check or a merge round; the fixer that tried was the cheap one that wrote the branch (Haiku at its 50-turn cap, Codex
agents whose sandbox could not write the checkout's git folder), and after two such tries the task said "needs a
person" and no agent ever looked again. The other 14 were blocked tasks whose read-only planner, also picked
cheapest-first, wrote a "needs a person" verdict that was then final forever. Most of them were stale merge
conflicts or failures that a strong agent with a fresh merge of the base can fix or classify.

So a task that would say "needs a person" goes to the escalation agents first (`routing.escalationAgents`, default
opus then fable), one try each per failure: a repair session with a fresh merge of the base, a larger turn budget and
the whole history. It may fix and push, close the work as already done, or name the one action only the owner can
take (a settings.json line, a GitHub click, an account or credential, a product decision the docs don't answer). Only
that last verdict shows in Needs you, worded as the action. When every tier fails without such a verdict, the item
shows as needing a person with the escalation's own summary, and goes round again once the base has moved on
(REESCALATE_HOURS).

loop.Engine owns the scheduling (`_escalate_stuck`); pipeline.escalate_pr runs one session."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

DEFAULT_AGENTS = ("opus", "fable")
REESCALATE_HOURS = 24
MAX_TURNS = 150
MAX_PER_PASS = 3
NEEDS_ALEX = "needs a person: "  # same prefix as deadends.NEEDS, so the dashboard counts it in Needs you
ESCALATED = "escalated to "

# What the owner alone can do. A verdict outside these is not accepted as "needs Alex": the next tier tries instead.
OWNER_ONLY = ("settings", "github", "account", "decision", "hardware")


def agents(pool) -> list[str]:
    """The escalation agents in order, from `routing.escalationAgents`, that exist in the pool."""
    if pool is None:
        return []
    wanted = pool.routing.get("escalationAgents") or list(DEFAULT_AGENTS)
    names = []
    for w in wanted:
        w = str(w).strip()
        if w in pool.specs:
            names.append(w)
        else:  # "opus" also matches an agent called e.g. "claude-opus-5-5"
            names += [n for n in pool.specs if w and w.lower() in n.lower() and n not in names]
    return names


def failure_key(pr_head: str, why: str) -> str:
    """One escalation round per failure: the PR head when there is one, else the reason text."""
    basis = pr_head or " ".join((why or "").split())
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _older_than(stamp: str, hours: float) -> bool:
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at >= timedelta(hours=hours)


def next_tier(record: dict | None, key: str, tiers: int) -> int | None:
    """Which escalation tier runs next for this failure, or None when none should run now."""
    if tiers <= 0:
        return None
    if not record or record.get("key") != key:
        return 0
    if record.get("running"):
        return None
    if record.get("verdict") == "needs_alex":
        return None  # a real owner action: it stays in Needs you until the owner acts (new head or new comment)
    nxt = int(record.get("tier", -1)) + 1
    if nxt < tiers:
        return nxt
    # Every tier failed: try again from the first once the base has had time to move on.
    return 0 if _older_than(record.get("at", ""), REESCALATE_HOURS) else None


def wait_text(record: dict | None, key: str, tiers: list[str]) -> str:
    """What the dashboard shows for a task the desk has (or has had) in hand, or "" for none."""
    if not record or record.get("key") != key:
        return ""
    if record.get("running"):
        return f"{ESCALATED}{record.get('agent', 'a stronger agent')}: fixing or classifying it before it reaches you"
    if record.get("verdict") == "needs_alex":
        return NEEDS_ALEX + "only you can do this: " + " ".join(str(record.get("action", "")).split())[:400]
    if int(record.get("tier", -1)) + 1 < len(tiers):
        return f"{ESCALATED}{tiers[int(record.get('tier', -1)) + 1]} next (a stronger agent looks before it reaches you)"
    try:
        at = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
        when = (at + timedelta(hours=REESCALATE_HOURS)).isoformat(timespec="minutes")[:16].replace("T", " ") + "Z"
    except ValueError:
        when = "later"
    return (NEEDS_ALEX + f"{' and '.join(tiers)} could not fix or classify it (escalation retries at {when}). "
            + " ".join(str(record.get("summary", "")).split())[:300])


def accept_owner_action(action: str, category: str) -> bool:
    return bool(action and action.strip()) and (category or "").strip().lower() in OWNER_ONLY


def brief(why: str, base: str, tier_agent: str, earlier: list[str]) -> str:
    """The feedback the escalation session gets, on top of the issue and the fixer's usual instructions."""
    tried = ("\n\nEarlier escalation attempts said:\n" + "\n".join(f"- {e}" for e in earlier)) if earlier else ""
    return (
        f"ESCALATION ({tier_agent}). This task was about to go to the project owner's Needs you list with this reason:\n\n"
        f"{why}\n\n"
        "The owner wants coding and merging to go on without him: you are the last stop before him, and the cheaper "
        "agents that tried before you gave up. Read the issue, every comment on it and on its PR (`gh issue view`, "
        "`gh pr view --comments`), and the last PC-check or merge-round report, then do the first of these that applies:\n"
        f"1. Merge the latest `origin/{base}` into this branch first. Resolve text conflicts keeping both sides' intent. "
        f"For a binary asset conflict take `{base}`'s copy unless the asset is this PR's own subject; then regenerate it "
        "with the project's tool if the issue names one, or list it under unverified.\n"
        "2. Fix what the report says failed, in this PR's code. If it also fails on the base without this PR, say so in "
        "`summary` with the evidence (spec name and log line), and set `base_broken` to that spec or check.\n"
        "3. If the work is already on the base (merged by another PR) or superseded, set `close` to the PR or commit "
        "that did it; Fletchery closes the issue and PR.\n"
        "4. A design or wording question: answer it from the project's docs (Docs/, .fletchery/, the issue chain) and "
        "carry on; write the answer and where it came from in `summary`.\n"
        "5. A missing tool or input (an .exe, a script, an exported file): look for it in this repository's history "
        "(`git log --all -- '*name*'`), in the organisation's tool repos (`gh search code`, `gh repo list`) and in "
        "sibling checkouts on this PC; rebuild it from its source when you find the source; otherwise add a task-sized "
        "note to `summary` naming where it came from last. It is not an owner action.\n"
        "6. Only when the next step truly needs the owner: set `needs_alex` to the exact action (the exact settings.json "
        "line, the GitHub page and button, the account, or the product decision with the options and your "
        "recommendation) and `needs_alex_kind` to one of settings, github, account, decision, hardware. Missing "
        "Build-PC runs, imports, captures or node evidence are not owner actions: list them in the summary as lane "
        "checks and finish the code.\n"
        "Commit on the branch; Fletchery checks and pushes. Use the whole turn budget if you need it." + tried)
