# Contributing

Fletchery is small on purpose: one backend (`fletchery/`, Python standard library), one page (`ui/portal/`, no build
step) and one window (`shell/Fletchery.cs`). Keep it that way. Before adding a feature, check whether GitHub, the
runners or an existing control already does it.

## Before you open a pull request

1. Run the Python tests: `python -m pytest fletchery/test_*.py` (or each file with `python`). They need no network.
2. Run the page checks: `npm ci --prefix ui`, then `FLETCHERY_BROWSER=<path to Chromium or Edge> node
   ui/test-portal.cjs`. They serve the page from `ui/test-live-fixture.json`, never a live backend, and cover phone and
   desktop layouts, light and dark, every control, folding cards and long lists, offline use and
   read-only mode.
3. If the page changed, refresh the pictures: set `FLETCHERY_PORTAL_SHOTS` to a folder, run the page checks again and
   copy the shots the README uses into `docs/images/fletchery/`.
4. Update the README and `docs/` in the same pull request when what people see or do changes.

## How the code is laid out

- **Controls** are routes in `App.actions()` (`fletchery/app.py`). `fletchery/portal.py` decides who may press them
  (`can_act()`): the window on `127.0.0.1`, or the phone through Tailscale Serve for the listed accounts. Every action
  is logged with where it came from.
- **The Live feed** is `fletchery/devwatch.py`. It reads GitHub with ETags and builds one snapshot; anything stuck is a
  blocker with a stable key, so a notification goes once.
- **Build machines** are `fletchery/machines.py` (the queue and the worker API) and `fletchery/node_worker.py` (each
  machine). A machine only runs recipes from its own list: add a recipe in both places.
- **The page** is `ui/portal/index.html`, `kit.js` (folding cards and "Show N more"), `portal.js` (Builds, Activity,
  theme, controls), `live.js` (Live) and `portal.css`. The service worker (`sw.js`) caches the shell: bump `CACHE`
  when a shell file changes. No inline scripts or styles (strict CSP).
- **No windows.** Anything the backend starts goes through `fletchery/nowindow.py`, so nothing flashes on the PC.

## Keep the repository public-safe

This repository is meant to be safe to publish. Before you commit:

- No keys, tokens, passwords or machine keys. Machine keys live in a file only the worker's account can read; none
  belongs in a profile or in git.
- No live profile. A project's real profile lives in `%APPDATA%\Fletchery\Projects` (`.gitignore` refuses
  `*.profile.json` outside `examples/`). Examples use placeholders: `100.64.0.10` for a Tailscale address,
  `your-pc.your-tailnet.ts.net`, `you@example.com`, `NODE-01`, `C:\Users\you`.
- No real tailnet names, private IPs, email addresses or Windows user names in code, tests, docs or screenshots.
  Screenshots come from the made-up fixture in `ui/test-live-fixture.json` (`docs/images/README.md`).
