# Fletchery's design

One live view of all development, for any number of projects, repositories, coding agents and build machines, that
should not need new code when one of those is added. The full write-up, with the research behind it, is the design
doc linked from the pull request that added this file; this is the short version kept with the code.

## Shape

```
sources                       core (the PC's backend)                    what reads it
GitHub poller (ETags)  ─┐     live state (memory, rebuilt each poll)  ┌─ /api/v2/state, /api/v2/history (schema 2)
machine reporters      ─┼──▶  rules.py (causes, problems, base        ├─ the page (window and phone)
agent heartbeats       ─┤     health, queue estimates, alerts)        ├─ phone notifications (push.py)
Fletchery's own builds ─┘     store.py (SQLite history, 90 days)      └─ /metrics (Prometheus text)
```

- Sources only write; readers only read. The rules are the one place that decides what is stuck, red or late.
- Names and result values follow OpenTelemetry's CI/CD conventions: a run's result is success, failure, error, timeout,
  cancellation or skip. **error** means the system broke (a machine problem), **failure** that the code did.
- Every project-specific rule lives in the profile (projects, base branches, fix labels, quick-check names, change
  kinds, agent branch prefixes, alert limits, failure-cause patterns). A new repository, project, agent or machine needs
  no code change; a new kind of source (Perforce, Horde, Jenkins) is one class with `poll()`.
- Polling, not webhooks: a webhook needs a public address, and Fletchery is tailnet-only.

## Records

| Record | What it is | Kept |
|---|---|---|
| Project | A named group of repositories, base branches and machines from the profile | profile |
| Change | A pull request from first push to release (pipeline.py) | memory; merges in SQLite |
| Run / Task | An Actions run and its jobs, or a Fletchery build | SQLite |
| Worker | A runner or build machine, in pools by labels | memory |
| Machine | A computer with a reporter | memory; disk readings in SQLite |
| Agent | A Claude thread, Codex task or other coder (presence.py) | memory |
| Problem | One cause of failure, by fingerprint, with every change it hits (Horde-style) | SQLite |
| Alert | A rule firing, with its wait, keep-firing time, group and snooze | SQLite |

## What each part answers

| Part | Question | File |
|---|---|---|
| Flow strip | Is work moving? Merges per hour and day, time since the last merge per base, PR-to-merge median, wait for a machine, how often a base goes red, and a verdict | `flow.py` |
| Base health | Is a base branch red, since when, which merge broke it, and is a fix in line? Only the branch's required checks count when GitHub says which (rulesets need only read access). | `rules.py` |
| Failure causes | Is this red PR its own bug, an inherited red base, flaky, a machine problem, or cancelled? | `rules.py` |
| Queue | What waits for which machines, in what order, starting and finishing when, and who cancelled what? | `rules.py` |
| Alerts | What needs a person now, told once, grouped, snoozable? | `alerts.py` |

## Security

Unchanged rules: the window on `127.0.0.1`; the phone through Tailscale Serve only, for listed accounts; every button
needs the page's key and the same origin; per-machine revocable keys (hashes only); no tokens stored (the `gh`
sign-in); the store holds names, numbers and times only; everything started runs hidden at low priority.
