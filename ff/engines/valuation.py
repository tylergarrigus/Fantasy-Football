"""Player valuation, always relative to one specific league.

The same player is worth different amounts in different leagues, and not only
because scoring differs. Value here is *value over replacement* -- what you
actually gain by rostering him instead of the best guy freely available in that
league. A third RB is worth far more on a team whose RB2 is a waiver pickup than
on one already two-deep, and worth nothing at all if you cannot start him.

Every number is labelled by kind: FACT (observed), PROJECTION (a model's
forecast), or INFERENCE (our reasoning on top). Nothing here fabricates a value
when the inputs are missing -- it returns DATA UNAVAILABLE and lets the caller
decide, because a made-up projection that looks precise is worse than a gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.identity import PlayerRegistry
from ff.intel.news import NewsEngine, severity_of
from ff.logging_setup import get_logger

log = get_logger(__name__)

# Week-to-week coefficient of variation by position. These are heuristic priors
# from how fantasy scoring behaves generally, not fitted to this season -- so
# they are deliberately coarse. They get blended with observed variance as soon
# as a player has enough games, which is the honest way to do this.
BASE_CV = {"QB": 0.34, "RB": 0.55, "WR": 0.60, "TE": 0.65, "K": 0.45, "DST": 0.72}
DEFAULT_CV = 0.55

# How many players at each position are realistically startable league-wide,
# per starting slot. Used to locate replacement level.
REPLACEMENT_DEPTH = {"QB": 1.4, "RB": 2.6, "WR": 2.8, "TE": 1.3, "K": 1.0, "DST": 1.0}

# Multiplier on projection by injury designation. Blunt but transparent: these
# express availability risk, not a claim about his talent.
STATUS_MULTIPLIER = {
    "out": 0.0, "ir": 0.0, "injured reserve": 0.0, "suspended": 0.0, "pup": 0.0,
    "doubtful": 0.15, "questionable": 0.78, "probable": 0.95,
}


@dataclass
class PlayerValue:
    player_id: str
    name: str
    position: str | None
    nfl_team: str | None

    # PROJECTION -- expected points this week under this league's scoring
    projection: float | None = None
    floor: float | None = None
    ceiling: float | None = None
    sigma: float | None = None

    # INFERENCE -- derived
    replacement: float | None = None
    value_over_replacement: float | None = None
    ros_value: float | None = None
    playoff_value: float | None = None

    # FACT -- observed
    injury_status: str | None = None
    availability: str = "unknown"  # mine | opponent | available | unknown
    pct_owned: float | None = None

    unavailable_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_projection(self) -> bool:
        return self.projection is not None

    @property
    def startable(self) -> bool:
        return self.has_projection and (self.projection or 0) > 0

    def summary(self) -> str:
        if not self.has_projection:
            return f"{self.name}: DATA UNAVAILABLE ({'; '.join(self.unavailable_reasons) or 'no projection'})"
        vor = f", VOR {self.value_over_replacement:+.1f}" if self.value_over_replacement is not None else ""
        return f"{self.name} ({self.position}): ~{self.projection:.1f} pts{vor}"


class ValuationEngine:
    """Values every player *for one league*. Never shared between leagues."""

    def __init__(
        self,
        ctx: LeagueContext,
        registry: PlayerRegistry,
        news: NewsEngine,
        usage: Any | None = None,
    ):
        self.ctx = ctx
        self.registry = registry
        self.news = news
        self.usage = usage
        self._replacement_cache: dict[str, float] = {}

    # -- core --------------------------------------------------------------

    def value(self, player_id: str, week: int | None = None) -> PlayerValue:
        week = week or self.ctx.current_week()
        player = self.registry.get(player_id)
        row = self.ctx.store.one(
            "SELECT * FROM nfl_players WHERE player_id = ?", (player_id,)
        )
        name = player.full_name if player else (row["full_name"] if row else player_id)
        position = (player.position if player else None) or (row["position"] if row else None)

        value = PlayerValue(
            player_id=player_id,
            name=name,
            position=position,
            nfl_team=player.nfl_team if player else None,
            availability=self.ctx.availability(player_id),
        )

        base = self.ctx.projection(player_id, week)
        if base is None:
            base = self.ctx.season_average(player_id)
            if base is not None:
                value.notes.append("PROJECTION: weekly projection missing; using season average")
        if base is None:
            value.unavailable_reasons.append("no projection under this league's scoring")
            return value

        status_info = self.news.best_current_status(player_id)
        status = None
        if status_info.get("status") == "DATA CONFLICT":
            value.unavailable_reasons.append(
                "DATA CONFLICT on injury status -- sources disagree"
            )
            value.notes.append(f"Leading claim: {status_info.get('leading')}")
            status = status_info.get("leading")
        elif status_info.get("status") not in (None, "DATA UNAVAILABLE"):
            status = status_info.get("status")
        elif row and row["injury_status"]:
            status = row["injury_status"]

        value.injury_status = status
        multiplier = STATUS_MULTIPLIER.get((status or "").strip().lower(), 1.0)
        if multiplier < 1.0:
            value.notes.append(
                f"INFERENCE: projection discounted to {multiplier:.0%} for status {status!r}"
            )

        value.projection = round(base * multiplier, 2)
        value.sigma = round(self._sigma(value.projection, position), 2)
        # Floor/ceiling as ~80% interval. Truncated at zero because you cannot
        # score negative points at these positions in practice.
        value.floor = round(max(0.0, value.projection - 1.28 * value.sigma), 1)
        value.ceiling = round(value.projection + 1.28 * value.sigma, 1)

        replacement = self.replacement_level(position, week)
        if replacement is not None:
            value.replacement = round(replacement, 2)
            value.value_over_replacement = round(value.projection - replacement, 2)

        value.ros_value = self._rest_of_season(player_id, value, week)
        value.playoff_value = self._playoff_value(player_id, value, week)

        fa = self.ctx.one(
            "SELECT pct_owned FROM league_free_agents "
            "WHERE league_id = :league_id AND player_id = :pid",
            pid=player_id,
        )
        if fa:
            value.pct_owned = fa["pct_owned"]

        if self.usage is not None:
            trend = self.usage.usage_trend(player_id)
            if trend.get("status") == "ok":
                delta = trend.get("snap_pct_delta")
                if delta is not None and abs(delta) >= 0.10:
                    direction = "rising" if delta > 0 else "falling"
                    value.notes.append(
                        f"FACT: snap share {direction} {abs(delta):.0%} over the last two weeks"
                    )
            else:
                value.notes.append("Usage trend: DATA UNAVAILABLE")

        return value

    def value_many(self, player_ids: list[str], week: int | None = None) -> dict[str, PlayerValue]:
        return {pid: self.value(pid, week) for pid in player_ids}

    # -- replacement level -------------------------------------------------

    def replacement_level(self, position: str | None, week: int) -> float | None:
        """What the best freely-available player at this position projects for.

        This is the number that makes a waiver add worth anything. In a shallow
        league replacement level is high and most adds are noise; in a deep one
        the same add is a real upgrade. Same player, different leagues,
        different answer -- exactly as intended.
        """
        if not position:
            return None
        cache_key = f"{position}:{week}"
        if cache_key in self._replacement_cache:
            return self._replacement_cache[cache_key]

        rows = self.ctx.query(
            "SELECT f.player_id FROM league_free_agents f "
            "JOIN nfl_players p ON p.player_id = f.player_id "
            "WHERE f.league_id = :league_id AND p.position = :pos",
            pos=position,
        )
        projections = []
        for row in rows:
            proj = self.ctx.projection(row["player_id"], week)
            if proj is not None:
                projections.append(proj)

        if not projections:
            return None

        projections.sort(reverse=True)
        # Not the single best free agent -- the one you'd realistically end up
        # with after the rest of the league also picks. Depth-adjusted.
        teams = len(self.ctx.teams()) or 10
        slots = self.ctx.roster_slots().get(position, 1) or 1
        index = min(len(projections) - 1, max(0, int(REPLACEMENT_DEPTH.get(position, 1.5) * slots) - 1))
        replacement = projections[index]
        self._replacement_cache[cache_key] = replacement
        return replacement

    # -- horizon-specific value -------------------------------------------

    def _sigma(self, projection: float, position: str | None) -> float:
        """Week-to-week standard deviation.

        Blends the positional prior with observed variance once a player has
        enough games -- until then the prior is all we honestly have.
        """
        cv = BASE_CV.get((position or "").upper(), DEFAULT_CV)
        return max(1.5, projection * cv)

    def _rest_of_season(self, player_id: str, value: PlayerValue, week: int) -> float | None:
        """Expected weekly value across the remaining regular season."""
        if value.projection is None:
            return None
        settings = self.ctx.settings()
        reg_weeks = (settings["reg_season_weeks"] if settings else None) or 14
        remaining = max(0, reg_weeks - week + 1)
        if remaining == 0:
            return 0.0

        # A player currently OUT is worth his healthy rate for the weeks he is
        # expected back, not zero -- otherwise every injured stash looks
        # worthless and we would recommend dropping recoverable assets.
        base = value.projection
        if severity_of(value.injury_status) >= 5:
            healthy = self.ctx.projection(player_id, week) or 0.0
            weeks_missed = self._expected_weeks_out(player_id)
            if weeks_missed is None:
                value.notes.append("INFERENCE: return timeline unknown; RoS value is a wide estimate")
                weeks_missed = 2
            healthy_weeks = max(0, remaining - weeks_missed)
            base = (healthy * healthy_weeks) / remaining if remaining else 0.0
        return round(base, 2)

    def _expected_weeks_out(self, player_id: str) -> int | None:
        """Read a return timeline out of reporting. Absent => None, never a guess."""
        events = self.news.for_player(player_id, hours=336)
        for event in events:
            text = f"{event.get('headline', '')} {event.get('body') or ''}".lower()
            if "season-ending" in text or "torn acl" in text or "ir" in text:
                return 99
            for weeks, phrases in (
                (1, ("week to week", "week-to-week")),
                (4, ("month", "4-6 weeks", "several weeks")),
                (6, ("6-8 weeks", "two months")),
            ):
                if any(p in text for p in phrases):
                    return weeks
        return None

    def _playoff_value(self, player_id: str, value: PlayerValue, week: int) -> float | None:
        """Value specifically during the fantasy playoff weeks.

        A player who returns in week 15 is worth far more to a team that will
        make the playoffs than his rest-of-season average suggests, and far less
        to a team that will not.
        """
        if value.projection is None:
            return None
        settings = self.ctx.settings()
        reg_weeks = (settings["reg_season_weeks"] if settings else None) or 14
        playoff_weeks = list(range(reg_weeks + 1, reg_weeks + 4))
        if week > playoff_weeks[-1]:
            return value.projection

        weeks_out = self._expected_weeks_out(player_id)
        if weeks_out is None:
            return value.projection
        return_week = week + weeks_out
        available = [w for w in playoff_weeks if w >= return_week]
        if not available:
            value.notes.append(
                "INFERENCE: not projected to be available during the fantasy playoffs"
            )
            return 0.0
        healthy = self.ctx.projection(player_id, week) or value.projection
        return round(healthy * (len(available) / len(playoff_weeks)), 2)

    # -- market-facing values ---------------------------------------------

    def trade_value(self, player_id: str, week: int | None = None) -> dict[str, Any]:
        """A tradeable value that blends now, rest-of-season, and playoffs.

        Weighted toward the playoff window because that is what the season is
        actually decided on -- but the weighting shifts with league posture, so
        a 'win now' team values this week more heavily.
        """
        value = self.value(player_id, week)
        if not value.has_projection:
            return {"status": "DATA UNAVAILABLE", "reasons": value.unavailable_reasons}

        posture = self.ctx.posture()
        weights = {
            "win_now": (0.50, 0.35, 0.15),
            "aggressive": (0.35, 0.40, 0.25),
            "balanced": (0.25, 0.40, 0.35),
            "playoffs": (0.15, 0.35, 0.50),
            "conservative": (0.25, 0.45, 0.30),
        }.get(posture, (0.25, 0.40, 0.35))

        now_w, ros_w, playoff_w = weights
        score = (
            now_w * (value.projection or 0)
            + ros_w * (value.ros_value or 0)
            + playoff_w * (value.playoff_value or 0)
        )
        return {
            "status": "ok",
            "score": round(score, 2),
            "components": {
                "this_week": value.projection,
                "rest_of_season": value.ros_value,
                "playoffs": value.playoff_value,
            },
            "weighting": f"{posture} ({now_w:.0%}/{ros_w:.0%}/{playoff_w:.0%})",
            "value_over_replacement": value.value_over_replacement,
            "untouchable": player_id in self.ctx.untouchable_players(),
        }
