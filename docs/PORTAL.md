# Phone portal: follow Fletchery from your phone

The portal is a small web app served by Fletchery itself. On an iPhone you add it to the Home Screen from Safari and it
opens like an app: no App Store, no Apple developer account. It shows what needs you, the workers, the task queue, the
merge round, builds with their download links, the machines and the activity log, and it can send notifications.

It is **read-only**. There is no pause, no settings, no keys and no "add task" in it, and the server behind it has no
route that changes anything in Fletchery. Folder paths on the PC are removed from what it shows.

<p><img src="images/fletchery/portal.png" width="260" alt="The phone portal: needs you, working, queued and to merge, then what is working now and the merge round (demo data)"></p>

## How it is kept safe

- **Private network only.** The portal listens on `127.0.0.1` on the PC. [Tailscale Serve](https://tailscale.com/kb/1312/serve)
  puts it on your tailnet with a real HTTPS certificate. No router port is opened and nothing is on the public internet.
- **Only your Tailscale account.** Serve tells the portal which Tailscale account each request comes from (and removes
  that header from anything a client sends). The portal answers only the accounts in `users`. Tailscale Funnel (public)
  traffic and tagged devices carry no account, so they are refused. Do not turn on Funnel for this port; if someone
  does, the portal still refuses every request.
- **Only its own address.** Requests for any other host name are refused (DNS rebinding).
- **Nothing to steal on the phone.** There is no password or token in the page. Access is your Tailscale sign-in.
- **The dashboard stays where it was**, on `127.0.0.1:8770`, with all its controls. The portal is a separate port.

Why Tailscale and not a Cloudflare tunnel: the PC and the nodes are already on Tailscale (see [MACHINES.md](MACHINES.md)),
it needs no domain and no public address at all, and it tells the portal who is asking. A Cloudflare tunnel with
Access would also work but puts a public URL in front of the PC and needs a domain on Cloudflare.

## Set it up (about ten minutes, once)

**1. On the PC: turn the portal on.** Add this under `workers` in the project's profile, then restart Fletchery
(Settings > Restart, or it restarts itself between passes after an update):

```json
"portal": {
  "port": 8772,
  "hosts": ["YOUR-PC.YOUR-TAILNET.ts.net"],
  "users": ["the-account-you-sign-in-to-tailscale-with"],
  "push": {"contact": "mailto:you@example.com"}
}
```

- `hosts`: the PC's Tailscale name. `tailscale status --json | findstr DNSName` prints it (leave off the last dot).
- `users`: your Tailscale sign-in, as shown at the top of the Tailscale admin page. If it is wrong, the portal's
  refusal names the account you are signed in with, so you can copy it from there.
- `push` is optional; leave it out for no notifications. `contact` is required by Apple's push service: a `mailto:`
  address or an `https://` page. It is sent to the push service, not shown anywhere.

Check it on the PC itself: open `http://127.0.0.1:8772`.

**2. On the PC: share it on the tailnet.** In a terminal:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8772
```

The first time, Tailscale prints a link to switch on HTTPS certificates for the tailnet; open it and allow it, then run
the command again. `tailscale serve status` shows it; `tailscale serve --https=443 http://127.0.0.1:8772 off` stops it.
If port 443 is already used by another Serve, use `--https=8443` and add `:8443` to the address below.

**3. On the iPhone.**

1. Install **Tailscale** from the App Store and sign in with the same account. Leave it connected.
2. In **Safari**, open `https://YOUR-PC.YOUR-TAILNET.ts.net`.
3. Tap **Share**, then **Add to Home Screen**. Open **Fletchery** from the Home Screen from now on.
4. For notifications (iOS 16.4 or newer): tap the bell, **Turn on notifications**, allow them, then **Send a test**.

Android and desktop browsers work the same way (Chrome: **Install app**).

## Notifications

With `push` in the profile the PC sends a notification when:

| Event | `events` name |
|---|---|
| a task needs you | `needs` |
| a merge round finishes | `merge` |
| a build finishes (once its download link is ready) or fails | `build` |
| a coding agent needs signing in again | `signin` |

`"push": {"contact": "...", "events": ["needs", "build"]}` limits them. Each message is encrypted for that one phone,
so Apple's push service relays it without being able to read it, and signed with a key made on the PC. Notifications
arrive even when Tailscale is off on the phone; opening the app needs Tailscale on.

Notifications need the Python package `cryptography` on the PC. It is usually there already (the Claude Agent SDK
brings it). If the bell says it is missing: `python -m pip install --user cryptography`, then restart Fletchery.

The key and the subscribed phones are kept in `portal-push.json` in the state folder. Delete that file to forget every
phone and make a new key.

## If something is wrong

| You see | Do this |
|---|---|
| "Can't reach the PC" | Tailscale is off on the phone, the PC is asleep, or Fletchery is not running |
| "You are signed in to Tailscale as …" | Add that account to `workers.portal.users`, restart Fletchery |
| "This address is not one of the portal's hosts" | Add the name in the address bar (no `https://`, no port) to `workers.portal.hosts` |
| "No Tailscale sign-in came with this request" | The device is tagged or the request came through Funnel; use a device signed in as you |
| The bell says to add it to the Home Screen | Notifications on iPhone only work from the Home Screen app, not from a Safari tab |
| Fletchery's log says `portal: not started` | The line says why: a bad `portal` block, or port 8772 is in use (choose another `port`) |

## For developers

`fletchery/portal.py` (the server and `summary()`, which builds what the phone sees field by field), `fletchery/push.py`
(Web Push: RFC 8291 encryption, RFC 8292 VAPID), `ui/portal/` (the page, no inline code, strict CSP). Checks:
`python fletchery/test_portal.py` and `node ui/test-portal.cjs`. To look at it with made-up data:
`python fletchery demo --serve --profile <a profile with workers.portal>`.
