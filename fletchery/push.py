"""Notifications on the phone for the portal (portal.py): Web Push, which iOS 16.4+ delivers to a web app that was added
to the Home Screen.

What is sent: a task that needs you, a merge round that finished, a build that finished (with where to download it),
and an agent that needs signing in again. Each message is encrypted for that one phone (RFC 8291, aes128gcm), so the
push service (Apple, Google, Mozilla) relays it without being able to read it, and signed with this install's own key
(VAPID, RFC 8292) so nobody else can send to the phone. The phone does not need to be on the private network to get one.

Needs the `cryptography` package (`python -m pip install --user cryptography`); without it the portal works and says
notifications are unavailable. State (the key, the subscribed phones, what was already announced) is one JSON file in
the state folder; it never leaves this PC.

    "push": {"contact": "mailto:you@example.com",   # who the push service may contact about this sender (required by Apple)
             "events": ["needs", "merge", "build", "signin"]}   # default: all four"""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

EVENTS = ("needs", "merge", "build", "signin")
# Only real push services are ever contacted: a subscription cannot point Fletchery at another address.
SERVICES = (".push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com", ".notify.windows.com")
MAX_SUBSCRIPTIONS = 10
POLL_SECONDS = 30
BUILD_DONE = ("done", "failed", "cancelled")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def crypto_ready() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def new_key() -> tuple[str, str]:
    """(private, public) for VAPID: a P-256 key, as base64url of the raw scalar and of the uncompressed point."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64(key.private_numbers().private_value.to_bytes(32, "big")), b64(public)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(plaintext: bytes, p256dh: bytes, auth: bytes, salt: bytes | None = None, sender=None) -> bytes:
    """One aes128gcm record for the phone whose keys are `p256dh` and `auth` (RFC 8291 section 3.4)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise ValueError("That is not a push subscription's key.")
    if len(plaintext) > 3000:
        raise ValueError("The notification is too long.")
    salt = salt or os.urandom(16)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    sender_public = sender.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh))
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + p256dh + sender_public, 32)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(key).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: the last (only) record
    return salt + struct.pack(">IB", 4096, len(sender_public)) + sender_public + body


def decrypt(message: bytes, receiver, auth: bytes) -> bytes:
    """What the phone does with `encrypt`'s output. Used by the checks."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, (_, idlen) = message[:16], struct.unpack(">IB", message[16:21])
    sender_public = message[21:21 + idlen]
    mine = receiver.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = receiver.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + mine + sender_public, 32)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(key).decrypt(nonce, message[21 + idlen:], None)
    return plain.rstrip(b"\x00")[:-1]


def vapid_header(endpoint: str, private: str, public: str, contact: str, now: float | None = None) -> str:
    """The Authorization header that proves to the push service which install is sending (RFC 8292)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": contact}
    signing = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + \
        b64(json.dumps(claims, separators=(",", ":")).encode())
    key = ec.derive_private_key(int.from_bytes(unb64(private), "big"), ec.SECP256R1())
    r, s = decode_dss_signature(key.sign(signing.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={signing}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={public}"


def endpoint_ok(endpoint: str) -> bool:
    parts = urlsplit(str(endpoint or ""))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and parts.port in (None, 443) and not parts.username and any(
        host == s or (s.startswith(".") and host.endswith(s)) for s in SERVICES)


def post(endpoint: str, body: bytes, headers: dict, timeout: float = 20) -> int:
    """The HTTP status the push service answered with."""
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ---- what is worth a notification ----
def marks(view: dict) -> dict:
    """What the portal's summary says now, as {key: notification}. A key that was not there at the last look is news."""
    out = {}
    for t in view.get("needs") or []:
        out[f"needs:{t.get('number')}"] = {"event": "needs", "title": "Needs you", "tab": "now",
                                           "body": f"#{t.get('number')} {t.get('title') or ''}".strip()}
    prev = (view.get("merge") or {}).get("previous") or {}
    if prev.get("round"):
        out[f"merge:{prev['round']}"] = {"event": "merge", "title": f"Merge round {prev['round']} finished", "tab": "merges",
                                         "body": str(prev.get("result") or "It ended.")}
    for b in view.get("builds") or []:
        if b.get("state") not in BUILD_DONE:
            continue
        if b.get("state") == "done" and b.get("publish") in ("uploading", "published") and not b.get("links"):
            continue  # the download is on its way: say it once, with the link
        ready = b.get("state") == "done"
        out[f"build:{b.get('id')}:{'links' if b.get('links') else b.get('state')}"] = {
            "event": "build", "tab": "builds",
            "title": f"Build {b.get('version')} " + ("is ready to download" if ready and b.get("links") else "finished" if ready else str(b.get("state"))),
            "body": ("Open Fletchery for the download link." if ready and b.get("links") else str(b.get("reason") or ""))[:160]}
    for p in view.get("pauses") or []:
        if p.get("kind") == "signed_out":
            out["signin"] = {"event": "signin", "title": "An agent needs signing in", "tab": "now", "body": str(p.get("text") or "")[:160]}
    return out


class Push:
    def __init__(self, path: Path, conf: dict, view_fn, log=None, repo: str = ""):
        self.path = Path(path)
        self.conf = conf or {}
        self.view_fn = view_fn
        self.log = log or (lambda line: None)
        self.contact = str(self.conf.get("contact") or "").strip()
        events = self.conf.get("events")
        self.events = [e for e in events if e in EVENTS] if isinstance(events, list) else list(EVENTS)
        self.lock = threading.RLock()
        self.state = {"private": "", "public": "", "subscriptions": [], "seen": None}
        self._stop = threading.Event()
        self.send = post  # replaced in the checks
        self._load()

    # ---- state ----
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.state.update({k: data[k] for k in self.state if k in data})
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def why_not(self) -> str:
        if not crypto_ready():
            return "Notifications need one more Python package on the PC: python -m pip install --user cryptography"
        if not (self.contact.startswith("mailto:") and "@" in self.contact and " " not in self.contact
                or self.contact.startswith("https://")):
            return 'Notifications need a contact in the profile: workers.portal.push.contact, e.g. "mailto:you@example.com".'
        return ""

    def _keys(self) -> tuple[str, str]:
        with self.lock:
            if not self.state["private"]:
                self.state["private"], self.state["public"] = new_key()
                self._save()
            return self.state["private"], self.state["public"]

    # ---- what the portal page calls ----
    def info(self) -> dict:
        why = self.why_not()
        if why:
            return {"available": False, "why": why}
        with self.lock:
            return {"available": True, "key": self._keys()[1], "events": self.events,
                    "phones": len(self.state["subscriptions"])}

    @staticmethod
    def _subscription(data: dict) -> dict:
        sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else data
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        endpoint, p256dh, auth = str(sub.get("endpoint") or ""), str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
        if len(endpoint) > 1000 or not endpoint_ok(endpoint):
            raise ValueError("That is not a push service Fletchery sends to.")
        try:
            ok = len(unb64(p256dh)) == 65 and len(unb64(auth)) == 16
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("That is not a push subscription's key.")
        return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}

    def subscribe(self, data: dict) -> dict:
        why = self.why_not()
        if why:
            raise ValueError(why)
        sub = self._subscription(data)
        sub["name"] = str(data.get("name") or "")[:60]
        sub["added"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        with self.lock:
            rest = [s for s in self.state["subscriptions"] if s["endpoint"] != sub["endpoint"]]
            if len(rest) >= MAX_SUBSCRIPTIONS:
                raise ValueError(f"{MAX_SUBSCRIPTIONS} phones are already subscribed. Turn notifications off on one first.")
            self.state["subscriptions"] = rest + [sub]
            self._save()
        self.log("portal: a phone turned notifications on")
        return {"subscribed": True, "phones": len(rest) + 1}

    def unsubscribe(self, data: dict) -> dict:
        endpoint = str((data.get("subscription") or data).get("endpoint") or "")
        with self.lock:
            before = len(self.state["subscriptions"])
            self.state["subscriptions"] = [s for s in self.state["subscriptions"] if s["endpoint"] != endpoint]
            if len(self.state["subscriptions"]) != before:
                self._save()
                self.log("portal: a phone turned notifications off")
        return {"subscribed": False}

    def test(self, data: dict) -> dict:
        endpoint = str((data.get("subscription") or data).get("endpoint") or "")
        with self.lock:
            subs = [s for s in self.state["subscriptions"] if s["endpoint"] == endpoint]
        if not subs:
            raise ValueError("This phone is not subscribed yet.")
        sent = self.deliver({"title": "Fletchery", "body": "Notifications work on this phone.", "tab": "now", "tag": "test"}, subs)
        if not sent:
            raise ValueError("The push service did not accept the test. Turn notifications off and on again.")
        return {"sent": sent}

    # ---- sending ----
    def deliver(self, note: dict, subs: list | None = None) -> int:
        """Send one notification to every subscribed phone (or `subs`). A phone whose subscription the push service
        says is gone (404, 410) is forgotten."""
        if self.why_not():
            return 0
        private, public = self._keys()
        with self.lock:
            targets = list(self.state["subscriptions"] if subs is None else subs)
        payload = json.dumps({k: note.get(k, "") for k in ("title", "body", "tab", "tag")}).encode("utf-8")
        sent, gone = 0, []
        for sub in targets:
            try:
                body = encrypt(payload, unb64(sub["p256dh"]), unb64(sub["auth"]))
                code = self.send(sub["endpoint"], body, {
                    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "86400",
                    "Urgency": "normal", "Authorization": vapid_header(sub["endpoint"], private, public, self.contact)})
            except Exception as e:
                self.log(f"portal: a notification was not sent ({type(e).__name__}: {e})"[:200])
                continue
            if code in (404, 410):
                gone.append(sub["endpoint"])
            elif 200 <= code < 300:
                sent += 1
            else:
                self.log(f"portal: the push service answered {code}")
        if gone:
            with self.lock:
                self.state["subscriptions"] = [s for s in self.state["subscriptions"] if s["endpoint"] not in gone]
                self._save()
        return sent

    def check(self, view: dict | None = None) -> list:
        """Compare the portal's summary with what was already announced; send what is new. The first look after
        notifications are switched on announces nothing: it only records what is already there."""
        now = marks(view if view is not None else self.view_fn())
        with self.lock:
            seen = self.state["seen"]
            first = seen is None
            seen = set(seen or [])
            news = [(k, n) for k, n in now.items() if k not in seen]
            # Keep what is still showing, plus merge and build marks (a finished round must not be announced again
            # when a newer one replaces it and then the list is trimmed).
            kept = [k for k in seen if k in now or k.startswith(("merge:", "build:"))][-400:]
            fresh = kept + [k for k, _ in news]
            if first or news or len(fresh) != len(seen):
                self.state["seen"] = fresh
                self._save()
            has_phones = bool(self.state["subscriptions"])
        if first or not has_phones:
            return []
        out = []
        for key, note in news:
            if note["event"] in self.events:
                self.deliver({**note, "tag": key})
                out.append(key)
        return out

    def start(self) -> None:
        def loop():
            while not self._stop.wait(POLL_SECONDS):
                try:
                    self.check()
                except Exception as e:
                    self.log(f"portal: notifications check failed ({type(e).__name__}: {e})"[:200])
        threading.Thread(target=loop, name="fletchery-push", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
