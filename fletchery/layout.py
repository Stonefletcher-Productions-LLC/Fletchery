"""Where the cards sit on the page: the person's own arrangement, kept in a small JSON file on this PC.

The file sits next to the project profile (`<Name>.layout.json` in `%APPDATA%\\Fletchery\\Projects`), so a new install
(which mirrors only the program folder) keeps it. The page reads it with `GET /api/layout` and saves it with
`POST /api/layout`; saving is a control like the others (the window always, the phone unless controls are off).

    {"version": 1,
     "columns": 2,                                         # how many columns on a wide window (1-3)
     "views": {"live": [["liveBuildingCard", ...], [...]]},  # per tab: the cards in each column, top to bottom
     "folded": ["h-liss", "h-lrun"]}                       # folded cards (their heading ids)

Only names are kept: card ids and tab names. Anything else in a saved body is dropped, and a card the page no longer
has is ignored when the page lays itself out, so an old file never breaks a newer page."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

VERSION = 1
MAX_COLUMNS = 3
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_VIEW = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def clean(data) -> dict:
    """The parts of `data` worth keeping, or ValueError when it is not a layout at all."""
    if not isinstance(data, dict):
        raise ValueError("A layout is a JSON object.")
    try:
        columns = int(data.get("columns", 2))
    except (TypeError, ValueError):
        raise ValueError("columns is 1, 2 or 3.")
    if not 1 <= columns <= MAX_COLUMNS:
        raise ValueError("columns is 1, 2 or 3.")
    views = {}
    raw = data.get("views") or {}
    if not isinstance(raw, dict):
        raise ValueError("views maps a tab to its columns.")
    for view, cols in list(raw.items())[:16]:
        if not isinstance(view, str) or not _VIEW.match(view) or not isinstance(cols, list):
            continue
        seen, out = set(), []
        for col in cols[:MAX_COLUMNS]:
            ids = []
            for i in (col if isinstance(col, list) else [])[:64]:
                if isinstance(i, str) and _ID.match(i) and i not in seen:
                    seen.add(i)
                    ids.append(i)
            out.append(ids)
        views[view] = out
    folded = []
    for i in (data.get("folded") or [])[:128] if isinstance(data.get("folded"), list) else []:
        if isinstance(i, str) and _ID.match(i) and i not in folded:
            folded.append(i)
    return {"version": VERSION, "columns": columns, "views": views, "folded": folded}


def path_for(profile: Path | None, state_dir: Path) -> Path:
    """`LifeShare.profile.json` -> `LifeShare.layout.json` beside it; no profile -> `layout.json` in the state folder."""
    if profile:
        name = profile.name
        stem = name[: -len(".profile.json")] if name.lower().endswith(".profile.json") else profile.stem
        return profile.with_name(stem + ".layout.json")
    return Path(state_dir) / "layout.json"


class Layout:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> dict | None:
        """The saved layout, or None when there is none yet (or it cannot be read: the page then uses its own)."""
        try:
            return clean(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            return None

    def save(self, data) -> dict:
        out = clean(data)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        return out

    def reset(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
