"""Sleeper API -- free, public, no auth, no key, no rate-limit ceiling worth worrying about.

Two jobs here:
  1. The player universe. This is our identity backbone: Sleeper's records carry
     espn_id and gsis_id inline, which gives us the ESPN <-> nflverse crosswalk
     for free instead of by fuzzy name matching.
  2. Trending adds/drops. Genuinely useful as an *early* signal -- the fantasy
     population reacts to a beat-writer tweet before the official injury report
     posts. Treated as a signal to go verify, never as a fact on its own.
"""

from __future__ import annotations

from typing import Any

from ff.sources.base import HttpClient, SourceError
from ff.logging_setup import get_logger

log = get_logger(__name__)

BASE = "https://api.sleeper.app/v1"

# The full player dump is several MB and changes slowly. Refetching it every
# 15 minutes would be rude and pointless.
PLAYERS_TTL = 12 * 3600
TRENDING_TTL = 900
STATE_TTL = 1800


class SleeperSource:
    def __init__(self, http: HttpClient):
        self.http = http

    def nfl_state(self) -> dict[str, Any]:
        """Current season, week, and season phase (pre/regular/post)."""
        return self.http.get_json(f"{BASE}/state/nfl", ttl=STATE_TTL).data

    def players(self) -> dict[str, dict[str, Any]]:
        """Every NFL player Sleeper knows about, keyed by sleeper id."""
        resp = self.http.get_json(f"{BASE}/players/nfl", ttl=PLAYERS_TTL)
        data = resp.data
        if not isinstance(data, dict):
            raise SourceError("sleeper players: expected an object")
        return data

    def trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 50) -> list[dict]:
        """Most-added or most-dropped players league-wide.

        Returns [{"player_id": <sleeper id>, "count": int}, ...].
        """
        if kind not in {"add", "drop"}:
            raise ValueError("kind must be 'add' or 'drop'")
        resp = self.http.get_json(
            f"{BASE}/players/nfl/trending/{kind}",
            params={"lookback_hours": lookback_hours, "limit": limit},
            ttl=TRENDING_TTL,
        )
        return resp.data if isinstance(resp.data, list) else []

    def injury_snapshot(self) -> dict[str, dict[str, Any]]:
        """{sleeper_id: {status, body_part, ...}} for anyone carrying a designation.

        Used for stage-1 change detection: hash this, compare to last cycle, and
        only look closer when it moved.
        """
        out: dict[str, dict[str, Any]] = {}
        for sid, rec in self.players().items():
            status = rec.get("injury_status")
            if not status:
                continue
            if (rec.get("position") or "").upper() not in {"QB", "RB", "WR", "TE", "K"}:
                continue
            out[sid] = {
                "status": status,
                "body_part": rec.get("injury_body_part"),
                "notes": rec.get("injury_notes"),
                "team": rec.get("team"),
                "name": rec.get("full_name"),
                "position": rec.get("position"),
            }
        return out

    def depth_chart_snapshot(self) -> dict[str, str]:
        """{sleeper_id: "TEAM:POS:order"} -- movement here is a role change."""
        out: dict[str, str] = {}
        for sid, rec in self.players().items():
            order = rec.get("depth_chart_order")
            pos = rec.get("depth_chart_position")
            if order is None or not pos:
                continue
            out[sid] = f"{rec.get('team')}:{pos}:{order}"
        return out
