"""ESPN draft data: rankings/ADP, and live pick polling.

Two undocumented views, both reverse-engineered from ESPN's own draft room:

  kona_player_info  -- the player pool with `averageDraftPosition`, ESPN's own
                       draft rank, auction values, and season projections.
                       Crucially, the rank is sortable by scoring format, so a
                       PPR league and a standard league get genuinely different
                       boards rather than one board pretending to fit both.

  mDraftDetail      -- picks made so far. Polls fine during a live draft; this
                       is how we know who is gone.

Neither is supported by ESPN and both can change without notice, so every read
degrades to DATA UNAVAILABLE rather than guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ff.config import ESPNCredentials
from ff.db.context import LeagueContext
from ff.db.store import Store, utcnow
from ff.identity import PlayerRegistry, normalize_team
from ff.logging_setup import get_logger
from ff.sources.base import HttpClient, SourceError

log = get_logger(__name__)

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"

# ESPN's own scoring buckets for draft ranks. Picking the wrong one is the
# single easiest way to produce a board that is confidently wrong.
SCORING_BUCKETS = {"standard": "STANDARD", "ppr": "PPR", "half": "HALF_PPR"}

DRAFTABLE_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


@dataclass
class DraftPick:
    overall: int
    round_num: int
    round_pick: int
    team_id: int
    espn_player_id: int
    keeper: bool = False
    bid_amount: int | None = None


class ESPNDraftSource:
    def __init__(
        self,
        http: HttpClient,
        store: Store,
        registry: PlayerRegistry,
        creds: ESPNCredentials,
    ):
        self.http = http
        self.store = store
        self.registry = registry
        self.creds = creds

    # -- rankings / ADP ----------------------------------------------------

    def fetch_rankings(
        self, league_id: int, season: int, scoring: str = "ppr", limit: int = 400
    ) -> list[dict[str, Any]]:
        """Player pool with ADP and draft rank, ranked for this league's scoring."""
        bucket = SCORING_BUCKETS.get(scoring.lower(), "PPR")

        # ESPN takes the real query in a header, not the query string. The
        # filter shape is finicky; this is the form the draft room itself sends.
        fantasy_filter = {
            "players": {
                "limit": limit,
                "sortDraftRanks": {
                    "sortPriority": 100,
                    "sortAsc": True,
                    "value": bucket,
                },
            }
        }

        try:
            response = self.http.get_json(
                f"{BASE}/{season}/segments/0/leagues/{league_id}",
                params={"view": "kona_player_info"},
                headers={"x-fantasy-filter": json.dumps(fantasy_filter)},
                cookies=self.creds.as_cookies(),
                ttl=3600,  # ADP moves slowly; hourly is plenty
            )
        except SourceError as exc:
            log.warning("draft rankings unavailable: %s", exc)
            return []

        players = (response.data or {}).get("players") or []
        out: list[dict[str, Any]] = []
        for entry in players:
            player = entry.get("player") or {}
            ownership = player.get("ownership") or {}
            draft_ranks = player.get("draftRanksByRankType") or {}
            rank_entry = draft_ranks.get(bucket) or draft_ranks.get("PPR") or {}

            position = DRAFTABLE_POSITIONS.get(player.get("defaultPositionId"))
            if not position:
                continue

            out.append(
                {
                    "espn_id": player.get("id"),
                    "name": player.get("fullName"),
                    "position": position,
                    "team_id": player.get("proTeamId"),
                    "adp": ownership.get("averageDraftPosition"),
                    "adp_change": ownership.get("averageDraftPositionPercentChange"),
                    "pct_drafted": ownership.get("percentOwned"),
                    "draft_rank": rank_entry.get("rank"),
                    "auction_value": rank_entry.get("auctionValue"),
                    "projected": self._season_projection(player, season),
                    "injury_status": player.get("injuryStatus"),
                }
            )
        log.info("draft: fetched %d ranked players (%s scoring)", len(out), bucket)
        return out

    def _season_projection(self, player: dict[str, Any], season: int) -> float | None:
        """ESPN's own full-season projection, under this league's scoring."""
        for stat in player.get("stats") or []:
            # statSourceId 1 = projection, statSplitTypeId 0 = full season
            if stat.get("statSourceId") == 1 and stat.get("statSplitTypeId") == 0:
                if stat.get("seasonId") == season:
                    return stat.get("appliedTotal")
        return None

    def sync_rankings(
        self, ctx: LeagueContext, season: int, scoring: str = "ppr"
    ) -> int:
        """Store the board for this league. Scoped by league_id, as always."""
        rankings = self.fetch_rankings(ctx.league_id, season, scoring)
        if not rankings:
            return 0

        entries = [
            (r["espn_id"], r["name"], r["position"], None)
            for r in rankings
            if r.get("espn_id") and r.get("name")
        ]
        id_map = self.registry.bulk_resolve_espn(entries)

        now = utcnow()
        stored = 0
        for row in rankings:
            player_id = id_map.get(row.get("espn_id"))
            if not player_id:
                continue
            ctx.execute(
                """
                INSERT INTO draft_rankings(league_id, player_id, adp, adp_change,
                                           draft_rank, auction_value, pct_drafted,
                                           projected, updated_at)
                VALUES(:league_id, :pid, :adp, :chg, :rank, :auction, :pct, :proj, :ts)
                ON CONFLICT(league_id, player_id) DO UPDATE SET
                    adp = excluded.adp, adp_change = excluded.adp_change,
                    draft_rank = excluded.draft_rank,
                    auction_value = excluded.auction_value,
                    pct_drafted = excluded.pct_drafted,
                    projected = excluded.projected,
                    updated_at = excluded.updated_at
                """,
                pid=player_id, adp=row.get("adp"), chg=row.get("adp_change"),
                rank=row.get("draft_rank"), auction=row.get("auction_value"),
                pct=row.get("pct_drafted"), proj=row.get("projected"), ts=now,
            )
            stored += 1
        ctx.commit()
        return stored

    # -- live picks --------------------------------------------------------

    def fetch_picks(self, league_id: int, season: int) -> list[DraftPick]:
        """Picks made so far. Safe to poll during a live draft."""
        try:
            response = self.http.get_json(
                f"{BASE}/{season}/segments/0/leagues/{league_id}",
                params={"view": "mDraftDetail"},
                cookies=self.creds.as_cookies(),
                ttl=0,  # never cache during a draft -- staleness is the whole risk
            )
        except SourceError as exc:
            log.warning("draft picks unavailable: %s", exc)
            return []

        detail = (response.data or {}).get("draftDetail") or {}
        picks = []
        for pick in detail.get("picks") or []:
            # ESPN pre-creates every pick slot as soon as a draft is scheduled --
            # a 12-team, 16-round draft returns 192 entries before anyone has
            # picked. Unfilled slots carry playerId 0 or -1. Counting those as
            # made picks makes a draft that has not started look complete.
            player_id = pick.get("playerId") or 0
            if player_id <= 0:
                continue
            picks.append(
                DraftPick(
                    overall=pick.get("overallPickNumber", 0),
                    round_num=pick.get("roundId", 0),
                    round_pick=pick.get("roundPickNumber", 0),
                    team_id=pick.get("teamId", 0),
                    espn_player_id=player_id,
                    keeper=bool(pick.get("keeper")),
                    bid_amount=pick.get("bidAmount") or None,
                )
            )
        return picks

    def draft_order(self, league_id: int, season: int) -> list[tuple[int, int]]:
        """Round-1 slot order as [(round_pick, team_id), ...].

        Available as soon as the draft is scheduled, because ESPN creates the
        empty pick slots up front. That is what tells us which seat we draft
        from, and therefore how long the wait is between picks.
        """
        try:
            response = self.http.get_json(
                f"{BASE}/{season}/segments/0/leagues/{league_id}",
                params={"view": "mDraftDetail"},
                cookies=self.creds.as_cookies(),
                ttl=300,
            )
        except SourceError:
            return []

        detail = (response.data or {}).get("draftDetail") or {}
        order = [
            (p.get("roundPickNumber", 0), p.get("teamId", 0))
            for p in detail.get("picks") or []
            if p.get("roundId") == 1 and p.get("teamId")
        ]
        return sorted(order)

    def draft_in_progress(self, league_id: int, season: int) -> dict[str, Any]:
        """Has the draft started, and is it finished?"""
        try:
            response = self.http.get_json(
                f"{BASE}/{season}/segments/0/leagues/{league_id}",
                params={"view": "mDraftDetail"},
                cookies=self.creds.as_cookies(),
                ttl=0,
            )
        except SourceError as exc:
            return {"status": "DATA UNAVAILABLE", "reason": str(exc)}

        detail = (response.data or {}).get("draftDetail") or {}
        picks = detail.get("picks") or []
        return {
            "status": "ok",
            "in_progress": bool(detail.get("inProgress")),
            "complete": bool(detail.get("drafted")),
            "picks_made": len(picks),
        }

    def sync_picks(self, ctx: LeagueContext, season: int) -> int:
        picks = self.fetch_picks(ctx.league_id, season)
        if not picks:
            return 0

        now = utcnow()
        for pick in picks:
            hit = self.registry.by_source_id("espn", pick.espn_player_id)
            ctx.execute(
                """
                INSERT INTO draft_picks(league_id, overall_pick, round_num, round_pick,
                                        team_id, player_id, keeper, bid_amount, updated_at)
                VALUES(:league_id, :overall, :rnd, :rpick, :tid, :pid, :keeper, :bid, :ts)
                ON CONFLICT(league_id, overall_pick) DO UPDATE SET
                    team_id = excluded.team_id, player_id = excluded.player_id,
                    bid_amount = excluded.bid_amount, updated_at = excluded.updated_at
                """,
                overall=pick.overall, rnd=pick.round_num, rpick=pick.round_pick,
                tid=pick.team_id, pid=hit.player_id if hit else None,
                keeper=int(pick.keeper), bid=pick.bid_amount, ts=now,
            )
        ctx.commit()
        return len(picks)
