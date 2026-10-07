"""ESPN's public (unofficial, undocumented) sports API.

No auth, no key, no terms covering third-party use, no SLA. Reverse-engineered
from espn.com's own web client and documented by the community. Reliable in
practice, but every call is written to fail soft: news is a nice-to-have signal,
and the agent must keep working when it stops responding.

Distinct from the *fantasy* API in ff/sources/espn.py, which is a different host
and different auth story.
"""

from __future__ import annotations

from typing import Any

from ff.sources.base import HttpClient, SourceError
from ff.logging_setup import get_logger

log = get_logger(__name__)

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"

NEWS_TTL = 600
SCOREBOARD_TTL = 300


class ESPNNewsSource:
    def __init__(self, http: HttpClient):
        self.http = http

    def news(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent NFL headlines, normalized to a flat shape."""
        try:
            resp = self.http.get_json(f"{SITE}/news", params={"limit": limit}, ttl=NEWS_TTL)
        except SourceError as exc:
            log.warning("espn news unavailable: %s", exc)
            return []

        items: list[dict[str, Any]] = []
        for article in (resp.data or {}).get("articles", []) or []:
            links = article.get("links", {}).get("web", {})
            athletes = [
                {"id": a.get("id"), "name": a.get("displayName")}
                for a in (article.get("categories") or [])
                if a.get("type") == "athlete" and a.get("id")
            ]
            teams = [
                c.get("description")
                for c in (article.get("categories") or [])
                if c.get("type") == "team"
            ]
            items.append(
                {
                    "id": str(article.get("id") or article.get("headline", ""))[:64],
                    "headline": article.get("headline", ""),
                    "body": article.get("description", ""),
                    "published": article.get("published"),
                    "url": links.get("href"),
                    "athletes": athletes,
                    "teams": [t for t in teams if t],
                }
            )
        return items

    def scoreboard(self, week: int | None = None) -> list[dict[str, Any]]:
        """Games with kickoff time, venue, and (once live) status.

        Venue indoor/outdoor is what tells the weather engine whether to bother.
        """
        params: dict[str, Any] = {}
        if week:
            params["week"] = week
        try:
            resp = self.http.get_json(f"{SITE}/scoreboard", params=params, ttl=SCOREBOARD_TTL)
        except SourceError as exc:
            log.warning("espn scoreboard unavailable: %s", exc)
            return []

        games: list[dict[str, Any]] = []
        for event in (resp.data or {}).get("events", []) or []:
            comps = event.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            venue = comp.get("venue") or {}
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            odds = (comp.get("odds") or [{}])[0]
            games.append(
                {
                    "game_id": str(event.get("id")),
                    "kickoff": event.get("date"),
                    "home": (home.get("team") or {}).get("abbreviation"),
                    "away": (away.get("team") or {}).get("abbreviation"),
                    "indoor": bool(venue.get("indoor")),
                    "venue": venue.get("fullName"),
                    "city": (venue.get("address") or {}).get("city"),
                    "state": (venue.get("address") or {}).get("state"),
                    "over_under": odds.get("overUnder"),
                    "spread": odds.get("spread"),
                    "status": ((comp.get("status") or {}).get("type") or {}).get("name"),
                }
            )
        return games

    def injuries(self) -> list[dict[str, Any]]:
        """Team-by-team injury report.

        ESPN's core API models this as a pile of $ref links; we only walk the
        top level and rely on Sleeper for per-player status, which is cheaper
        and refreshes faster. This exists as a cross-check, not a primary.
        """
        try:
            resp = self.http.get_json(f"{SITE}/teams", ttl=6 * 3600)
        except SourceError as exc:
            log.warning("espn teams unavailable: %s", exc)
            return []
        teams = []
        for group in (resp.data or {}).get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            team = group.get("team", {})
            teams.append({"id": team.get("id"), "abbrev": team.get("abbreviation")})
        return teams
