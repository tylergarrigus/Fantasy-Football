"""Player photos from ESPN's CDN, shrunk for the page.

The page cannot load images from ESPN (its viewer only allows embedded
data), so the Actions runner -- the one machine with network access --
fetches each player's headshot once, crops it to a small square and keeps it
in data/headshots. The page builder inlines whatever is there; a player with
no photo simply shows initials. Nothing here is required for a build to
succeed.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from ff.logging_setup import get_logger

log = get_logger(__name__)

HEADSHOT = "https://a.espncdn.com/i/headshots/nfl/players/full/{id}.png"
LOGO = "https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"
# ESPN's logo paths use a few abbreviations that differ from the roster feed.
LOGO_ABBR = {"LA": "lar", "LAR": "lar", "LAC": "lac", "WSH": "wsh", "WAS": "wsh",
             "JAC": "jax", "JAX": "jax"}
SIZE = 80
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"


def photo_url(espn_id: int | str, nfl_team: str | None) -> str | None:
    try:
        eid = int(espn_id)
    except (TypeError, ValueError):
        return None
    if eid < 0:  # D/ST: ESPN ids are -16000 - teamId; the team logo is the photo
        if not nfl_team:
            return None
        return LOGO.format(abbr=LOGO_ABBR.get(nfl_team.upper(), nfl_team.lower()))
    return HEADSHOT.format(id=eid)


def shrink(png: bytes, size: int = SIZE) -> bytes:
    """Square crop (faces sit top-centre in ESPN's 600x436 shots) and shrink to WebP."""
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    x0 = (w - side) // 2
    im = im.crop((x0, 0, x0 + side, side)).resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=78, method=6)
    return out.getvalue()


def _fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - a missing photo is not an error
        log.debug("no photo at %s: %s", url, exc)
        return None


def sync_headshots(
    players: Iterable[dict[str, Any]],
    out_dir: Path,
    fetch: Callable[[str], bytes | None] = _fetch,
    cap: int = 400,
    pause: float = 0.15,
) -> dict[str, int]:
    """Fetch photos we don't have yet. Remembers 404s so they are not retried."""
    out_dir.mkdir(parents=True, exist_ok=True)
    missing_path = out_dir / "_missing.json"
    try:
        missing = set(json.loads(missing_path.read_text()))
    except (OSError, ValueError):
        missing = set()
    counts = {"have": 0, "new": 0, "failed": 0, "skipped": 0}
    seen: set[str] = set()
    for p in players:
        eid = p.get("espn_id")
        if eid is None or str(eid) in seen:
            continue
        seen.add(str(eid))
        path = out_dir / f"{eid}.webp"
        if path.exists():
            counts["have"] += 1
            continue
        if str(eid) in missing:
            counts["skipped"] += 1
            continue
        if counts["new"] + counts["failed"] >= cap:
            counts["skipped"] += 1
            continue
        url = photo_url(eid, p.get("nfl_team"))
        raw = fetch(url) if url else None
        if pause:
            time.sleep(pause)
        if not raw:
            missing.add(str(eid))
            counts["failed"] += 1
            continue
        try:
            path.write_bytes(shrink(raw))
            counts["new"] += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("could not shrink photo for %s: %s", eid, exc)
            missing.add(str(eid))
            counts["failed"] += 1
    missing_path.write_text(json.dumps(sorted(missing)))
    log.info("headshots: %s", counts)
    return counts
