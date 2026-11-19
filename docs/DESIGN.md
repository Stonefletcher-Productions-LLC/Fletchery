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
- Each poll revalidates checks for every distinct open PR head in a repository, including completed checks.
  GitHub's ETag lets the poll reuse unchanged results. The newest attempt for each check determines the PR's state.

## Records

| Record | What it is | Kept |
|---|---|---|
| Project | A named group of repositories, base branches and machines from the profile | profile |
| Change | A pull request from first push to release (pipeline.py) | memory; merges in SQLite |
| Run / Task | An Actions run and its jobs, or a Fletchery build | SQLite |
| Worker | A runner or build machine, in pools by labels | memory |
| Machine | A computer with a reporter | memory; disk readings in SQLite |
| Agent | A Claude thread, Codex task or other coder (presence.py) | memory |
| Problem | One cause of failure, by fingerprint, with every change it hits (Horde-style) | rebuilt each poll; base check history in SQLite |
| Alert | A rule firing, with its wait, keep-firing time, group and snooze | `alerts.json`; fired and cleared in SQLite |

## What each part answers

| Part | Question | File |
|---|---|---|
| Flow strip | Is work moving? Merges per hour and day, time since the last merge per base, PR-to-merge median, wait for a machine, how often a base goes red, and a verdict | `flow.py` |
| Base health | The poller resolves the branch tip and reads checks and statuses by that commit SHA. Required contexts come from branch protection and rulesets, including any required GitHub App. Every required context needs a successful result for green. The banner shows outstanding checks and explains incomplete reads. | `devwatch.py`, `rules.py` |
| Failure causes | Is this red PR its own bug, an inherited red base, flaky, a machine problem, or cancelled? | `rules.py` |
| Queue | What waits for which machines, in what order, starting and finishing when, and who cancelled what? | `rules.py` |
| Alerts | What needs a person now, told once, grouped, snoozable? Will a disk fill? | `alerts.py`, `rules.disk_forecast` |
| Read API | Everything above for scripts, Grafana and agents, in a shape that only grows | `api.py`, [API.md](API.md) |

## Adding things without code

| To add | Do |
|---|---|
| A repository | Push to it under a watched owner (`live.owners`); it appears within 10 minutes |
| A project of several repositories | `live.projects`: `{"Name": ["owner/a", "owner/b"]}` |
| A base branch nobody targets | `live.bases`: `{"owner/repo": ["release"]}` |
| A coding agent | `live.agents`: `[{"name": "Devin", "branches": ["devin/"], "logins": ["devin-ai-integration[bot]"]}]`; heartbeats ([AGENT-PRESENCE.md](AGENT-PRESENCE.md)) say what it is doing |
| A machine | Install the reporter with a key ([MACHINES.md](MACHINES.md)); its runners join it by name |
| A "machine, not code" message | `live.causes.machine`: regular expressions |
| An alert change | `live.alerts`: `{"<kind>": {"for": min, "keepFiring": min, "notify": bool, "off": bool}}` |

Only a new kind of source (another CI system, Perforce, Horde) needs code: one class with `poll()` that writes the same
records.

## Limits that stay

- GitHub has no "cancelled by" field: Fletchery names a person only when GitHub's annotation does, or the press was in
  Fletchery.
- History starts when Fletchery is installed: base health, Flow and queue estimates fill in over the first day.
- Test names come from check annotations; a check that writes none is named by its check only.
- Queue order and start times are estimates from past job times; GitHub does not publish its queue.

## Security

Unchanged rules: the window on `127.0.0.1`; the phone through Tailscale Serve only, for listed accounts; every button
needs the page's key and the same origin; per-machine revocable keys (hashes only); no tokens stored (the `gh`
sign-in); the store holds names, numbers and times only; everything started runs hidden at low priority.
