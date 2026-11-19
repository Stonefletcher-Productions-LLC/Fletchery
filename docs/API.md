# Fletchery's read API

For scripts, other dashboards (Grafana) and coding agents that want what the Live tab knows. The API answers the same
callers as the page: the PC itself on the desktop port (`127.0.0.1`), and over Tailscale only the accounts listed in
`workers.portal.users`. Nothing in it changes anything: the controls stay behind the page's own buttons.

It holds names, numbers and times only: no logs, tokens, file contents or folder paths.

## `GET /api/v2/state`

The whole Live snapshot as JSON, the same one the page draws. `"schema": 2` names its shape. From schema 2 on, fields
are only ever **added**: a reader that ignores fields it does not know keeps working through every update.

| Field | What |
|---|---|
| `at`, `fresh` | When it was made; when GitHub was last read, the poll interval, GitHub calls left this hour, any error |
| `repos`, `projects` | Watched repositories; projects (`{"name", "repos"}`) from the profile, else one per repository |
| `counts` | The numbers at the top of Live |
| `flow` | Merges per hour/day/week, time since the last merge, PR lead time, wait for a runner, red share, verdict; for `all`, each of `repos` and each of `projects` |
| `bases` | Base-branch health: state, failing checks and tests, since, first bad merge, suspects, fixes in line, PRs held red |
| `problems`, `causes` | Red PR checks grouped by cause (own, base, machine, flaky, cancelled) |
| `queue` | Runner pools (workers, busy, wait p50/p90, utilisation), what runs and waits with estimates, holds, cancelled runs and who cancelled them |
| `alerts` | `firing`, `pending` (with `fires_in`) and `cleared` alerts; each with `key`, `kind`, `group`, `title`, `notify`, `snoozed_until` |
| `blockers`, `parked` | Stuck or blocked items, each with its `alert` state |
| `machines`, `runners`, `building` | Machines (CPU, RAM, disks with `full_in`, disk guard, activities), runners, everything building now |
| `agents`, `changes` | Coding agents working now; changes on their way with their steps |
| `runs`, `prs`, `issues` | Actions runs, open pull requests with checks and cause, open issues |

`/api/live` is the same document (the page reads it); prefer `/api/v2/state` in scripts.

## `GET /api/v2/history`

Rows from the history store (`fletchery.db` in the state folder, kept 90 days), oldest first.

| Parameter | |
|---|---|
| `table` | `merges`, `runs`, `jobs`, `base_checks`, `disks` or `events` (alerts fired and cleared) |
| `since` | Unix seconds (default 0) |
| `repo` | `Owner/Repo`, for tables that have one |
| `limit` | 1 to 5000 (default 1000) |

The answer is `{"schema": 2, "table", "rows": [...], "next"}`. When `next` is a number there may be more: ask again
with `since` just above it. Result values follow OpenTelemetry's CI/CD names: `success`, `failure` (the code),
`error` (the system), `timeout`, `cancellation`, `skip`.

```sh
curl -s "http://127.0.0.1:8770/api/v2/history?table=merges&since=$(date -d '1 day ago' +%s)"
```

## `GET /metrics`

The same numbers in Prometheus text format, for Prometheus, Grafana Agent or any scraper on the PC: machine CPU, RAM
and free disk (with `fletchery_disk_full_in_seconds`), runners online and busy, pool size, wait and utilisation, base
branches red and for how long, merges per hour and day, time since the last merge, lead time, red PRs by cause, alerts
by state and kind, GitHub calls left. Every metric starts with `fletchery_` and is a gauge.

```yaml
scrape_configs:
  - job_name: fletchery
    static_configs: [{targets: ["127.0.0.1:8770"]}]
```
