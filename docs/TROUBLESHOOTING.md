# Troubleshooting

**Start here:** the **Activity** tab. Its health card says how long the backend has been up, which code it runs, when
GitHub was last read and how the build machines are, and the log below it lists everything Fletchery did and who
asked for it. The backend's own logs are in the state folder and in `%LOCALAPPDATA%\Fletchery\logs`
(`backend-supervisor.log`, `backend-stdout.log`, `backend-stderr.log`); the tray menu's **Open log folder** opens them.

| What you see | What to check |
| --- | --- |
| A red line at the top of Live | It names the cause. "Can't reach the PC": the PC is asleep, Fletchery is not running, or (on the phone) Tailscale is off. "GitHub has not been read": press **Check GitHub now**, then run `gh auth status` on the PC. `python -m fletchery live --profile <file>` does one read and prints the error |
| A runner or repository is missing | Its owner must be in `workers.portal.live.owners` and the repository pushed in the last `activeDays`. Organisation runners need `admin:org` on the PC's `gh` sign-in; repository runners do not |
| A runner says **Not needed** | Its run's pull request is closed but the run is still going. **Cancel run** stops it |
| A runner says **Step too long** | One step has run over 60 minutes. Open the run on GitHub; **Cancel run** if it is stuck |
| A machine shows **Offline** | Its worker (`Fletchery node worker` task) or reporter stopped. Both restart themselves; on that machine, `python node_worker.py check --config <file>` signs in once and prints what the PC answers |
| A build waits and never starts | No online, unpaused machine has the labels it needs (`os:windows`, `role:cook`, plus `workers.builds.nodeLabels`). Check **Pause all machines** is off |
| A build failed | Its **Log** link opens the machine's output; **Build again** queues it again |
| Next build fails to link or write into the workspace | A cancelled run left a process holding the files. See [Cancelled builds and leftover processes](CANCELLATION.md) |
| "Can't reach Fletchery's backend" in the window | The supervisor restarts it by itself. If it stays, quit from the tray and reopen |
| The backend stopped after an install and never came back | It exits (code 75) to reload new code, and only the supervisor (`shell\Start-Fletchery.ps1`, started by the **Fletchery** scheduled task) starts it again. A backend started bare with `python -m fletchery run` has no supervisor. Run the installer with `-AutoStart` once |
| "Fletchery is already running" | Another backend holds `fletchery.lock` in the state folder. There is only ever one; the window attaches to it |
| The phone says "You are signed in to Tailscale as …" | Add that account to `workers.portal.users`, restart the backend. More in [PORTAL.md](PORTAL.md#if-something-is-wrong) |
| A button on the phone says it is read-only | `workers.portal.controls` is `false` in the profile |
| Fletchery did not start: Python | Install Python 3.10+ from python.org with *Add to PATH*, then run the installer again |

## Reporting a bug

Use the repository's **New issue → Bug report** template. Include the steps, what you expected, the Windows version
and the exact error. Screenshots help; hide private project details. Do not attach a profile or a machine key.
