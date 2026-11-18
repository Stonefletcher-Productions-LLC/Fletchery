# Documentation screenshots

`fletchery/*.png` are the real Fletchery page in headless Chromium, saved by `ui/test-portal.cjs` with
`FLETCHERY_PORTAL_SHOTS` set. The data is the made-up fixture in `ui/test-live-fixture.json`; nothing in them comes
from a real project, machine, address or account.

| File | What |
|---|---|
| `desktop-live.png`, `desktop-builds.png`, `desktop-activity.png` | The desktop window's three tabs at 1400 px |
| `portal-live.png`, `portal-builds.png`, `portal-activity.png` | The phone app's three tabs at iPhone size |
| `portal-live-dark.png` | The phone app's Live tab in dark mode |
| `icon.png` | The app icon |

To refresh them: `FLETCHERY_PORTAL_SHOTS=<folder> FLETCHERY_BROWSER=<Chromium> node ui/test-portal.cjs`, then copy the
files above from that folder. Marketing banners built from them are in
[marketing/fletchery](../../marketing/fletchery/README.md). `logo.png` and `social-preview.png` in this folder are the
repository's logo and GitHub social preview.
