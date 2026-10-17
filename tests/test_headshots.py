"""Photos: fetched once, shrunk to a small square, never required."""

from __future__ import annotations

import io
import json

from PIL import Image

from ff.sources import headshots as hs


def png(w, h, color=(200, 50, 50, 255)):
    im = Image.new("RGBA", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_photo_urls_for_players_and_defenses():
    assert hs.photo_url(3139477, "KC").endswith("/full/3139477.png")
    assert hs.photo_url(-16014, "LA").endswith("/500/lar.png")
    assert hs.photo_url(-16028, "WSH").endswith("/500/wsh.png")
    assert hs.photo_url("x", "KC") is None


def test_shrink_makes_a_small_square_webp():
    out = hs.shrink(png(600, 436))
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and im.size == (hs.SIZE, hs.SIZE)
    assert len(out) < 4000


def test_sync_fetches_once_and_remembers_misses(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return png(600, 436) if "3139477" in url else None

    players = [{"espn_id": 3139477, "nfl_team": "KC"}, {"espn_id": 999, "nfl_team": "KC"},
               {"espn_id": 3139477, "nfl_team": "KC"}, {"espn_id": None}]
    c = hs.sync_headshots(players, tmp_path, fetch=fetch, pause=0)
    assert c == {"have": 0, "new": 1, "failed": 1, "skipped": 0}
    assert (tmp_path / "3139477.webp").exists()
    assert json.loads((tmp_path / "_missing.json").read_text()) == ["999"]

    c2 = hs.sync_headshots(players, tmp_path, fetch=fetch, pause=0)
    assert c2 == {"have": 1, "new": 0, "failed": 0, "skipped": 1}
    assert len(calls) == 2, "nothing is fetched twice"
