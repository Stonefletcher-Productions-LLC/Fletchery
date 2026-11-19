# Agent presence: which coding agents are working right now

The Live tab's **Agents** card lists the Claude and Codex agents that are working **now**: what each one is on (issue
or PR), its current step and when it last checked in. An agent that says it is done, or stops checking in, leaves the
card. This note is the design; the code is `fletchery/presence.py`, `fletchery/agent_status.py`, the `/v1/agent` route
in `fletchery/machines.py` and `DevWatch._agents()` in `fletchery/devwatch.py`.

## Why not something simpler

| Signal | Why it is not enough on its own |
|---|---|
| Pushes and PR updates | An agent can work an hour before its first push. A push says it *was* working, not that it *is*. |
| `claude.exe` / `codex` processes on a machine | The Claude app runs ~15 processes whatever it is doing; cloud threads run on no machine of ours at all. (Claude Code's own `claude agents --json` does name each open session: see below.) |
| Local worktree file changes | Only local agents, and scanning worktrees costs disk I/O on machines that build. |

So agents **say** what they are doing (a heartbeat), and GitHub activity is only the fallback for an agent that does
not (yet) follow the instruction.

## Two ways in, one record

Every heartbeat becomes the same record: `agent` (claude, codex), `session` (the thread or session id), `work`
(`#123` or `owner/repo#123`), `branch` (optional, the git branch it works on), `step` (one line of text), `title` (optional,
the task's name, shown when no issue or PR is named, 120 characters at most), `state`
(`working`, `waiting` or `done`), and **the time Fletchery or GitHub received it** (never a time the agent claims).

1. **Local agents (Codex, Claude Code on the PC or a node): `POST /v1/agent` on the machines worker API.**
   It is the API the node workers and reporters already use: it listens only on the PC's Tailscale address, and each
   machine signs in with its own key (`Authorization: Bearer`, compared by SHA-256 hash, revocable, ten bad sign-ins
   lock that address out for a minute). No new port, no new secret. `fletchery/agent_status.py` reads the machine's
   existing `node.json` or `reporter.json` for the address and key file, so an agent only runs one command. It fills
   in what it can find itself: the repository of `#123` from the folder's `origin`, the current branch, and a session
   id hashed from the worktree folder (one worktree per task), so every call from one task updates one row.
2. **Cloud agents (Claude threads, which cannot reach the tailnet): one comment per session on a status issue.**
   Each watched repository can name an issue in `workers.portal.live.agentIssues` (`{"Owner/Repo": 123}`). An agent
   posts one comment when it starts and **edits that same comment** at every step and at the end. The comment starts
   with a machine-readable marker, then a line for people:

       <!-- fletchery-agent {"agent":"claude","session":"thread-4f2a","work":"#2350","step":"Importing hair","state":"working"} -->
       **Claude** (thread-4f2a) on #2350: Importing hair

   Why this relay and not another:
   - **Commit statuses or check runs** need a commit, so they cannot say "planning, nothing pushed yet", and check
     runs need a GitHub App.
   - **A status file on a branch** is a commit per step: noise in history, CI on every heartbeat.
   - **A gist** needs a separate token scope the agents do not have.
   - **An edited issue comment** needs only what every agent already has (comment on its repository), is
     one row per session, and its `updated_at` is GitHub's own clock, so the heartbeat time cannot be faked. Edits do
     not send notifications; only the first comment of a session does (people can unsubscribe from the status issue).
     Fletchery reads only the comments updated in the last day (`since=`, rounded to the hour so the ETag holds and an
     unchanged answer costs nothing).

## Open Claude Code sessions on a machine (no heartbeat needed)

Each machine's reporter (`fletchery/reporter.py`) also asks `claude agents --json` at most every 30 seconds and sends
the answer with its report: every Claude Code session open on that machine (interactive, background and Remote
Control), whether it is busy or idle, its name, the last folder name of its working folder (never the full path) and
when it started. The card lists each one as a **Claude** row with **Working** (busy) or **Idle**, the machine, and
"open since", until the session closes. A machine's list counts for two minutes after its report, so a reporter that
stops takes its sessions off the card. This needs no instruction to the agent and no GitHub trace, so a session shows
from its first minute. It is off when `reporter.json` says `"claudeSessions": false`; `"claude": "<path>"` names the
program when it is not on the reporter's PATH.

Cloud threads run on no machine of ours, so they still need the status issue below.

## Safety

- **Who may report.** The endpoint: a registered, unrevoked machine key. The relay: only comments whose author is the
  repository's `OWNER`, `MEMBER` or `COLLABORATOR`; anything else on the issue is ignored.
- **What is accepted.** `agent` from a fixed list; `session` 1-80 of `A-Z a-z 0-9 . _ : -`; `work` must look like
  `#123` or `owner/repo#123`, and is linked only when the repository is one the Live tab watches; `branch` a plain git
  branch name; `step` at most 200
  characters, with folder paths removed like everything else on Live; `state` from a fixed list. Anything else is a
  `400` (endpoint) or skipped (relay). Bodies over 1 MB are refused before parsing.
- **How much.** Per machine at most 60 heartbeats a minute (`429` past that) and 50 live sessions; the relay reads at
  most 300 comments a poll. A record expires 24 hours after its last heartbeat.
- **Where it is kept.** In memory, saved to `<state dir>/agent-presence.json` (replaced atomically) so a restart keeps
  who is working. No secret is stored there.

## What the card shows

- **Working:** a heartbeat in the last 15 minutes (`live.agentActiveMinutes`) and not `done`. The row shows the agent,
  its work (with the PR's title and check state when Fletchery knows the PR), the step, where it runs (the machine, or
  "cloud") and when it last checked in. Heartbeats are listed before guesses.
- **Fallback, "seen on GitHub":** a `claude/` or `codex/` branch with a push, or its PR updated or commented on, in the
  last 15 minutes, when no heartbeat covers that PR or branch. Marked so people know it is a guess. Once a heartbeat
  (any state, `done` included) covers a PR or branch, only a push newer than that heartbeat brings the guess back: a
  comment on a finished PR is not an agent at work.
- **Gone:** `done`, no heartbeat for 15 minutes, a merged or closed PR (closed after the last heartbeat or push), or a
  branch whose PR closed after its last push. A line under the list counts the agents seen earlier today but not in
  the last 15 minutes.

## For agents (copied into each repository's AGENTS.md)

At the start of a task, at each new step (at most once a minute) and at the end (`--state done`):

- **On the PC or a node:** `python <Fletchery>\fletchery\agent_status.py --agent codex --work "#123" --step "what you
  are doing now"` (add `--state waiting` while blocked on checks or a person, `--state done` at the end). If it cannot
  reach Fletchery it says so and exits 1: carry on with the work. Once per machine, someone runs
  `agent_status.py setup --config <reporter.json>` so it knows the machine and its key (or sets
  `FLETCHERY_AGENT_CONFIG`).
- **In a cloud thread:** create one comment on the status issue (number in AGENTS.md) with the marker above, keep its
  id, and edit that comment for every later step; end with `"state":"done"`.

## Setting it up

1. On the PC, add the status issue numbers to the profile, under `workers.portal.live`:
   `"agentIssues": {"Your-Org/YourRepo": 123}`. Optional: `"agentActiveMinutes": 15`.
2. On each machine with a reporter, once: `python <Fletchery>\fletchery\agent_status.py setup --config <reporter.json>`.
3. Copy the "For agents" lines above into each repository's AGENTS.md, with the status issue's number.
