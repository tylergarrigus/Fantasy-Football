"""Waiver wire and FAAB.

Two questions, in order, and skipping the first is how people waste a season's
FAAB in week 3:

  1. Is this player actually better than what I already have? Not "is he good"
     -- is he better than my worst startable option at that position. Most
     waiver adds fail this test and should be ignored.
  2. If yes, what is he worth *in this league*, given my budget, my needs, and
     what this league's managers actually bid?

FAAB advice is a range, never a single number, because the real answer depends
on what eleven other people do and we cannot see that.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.roster import RosterEngine
from ff.engines.valuation import ValuationEngine
from ff.identity import PlayerRegistry
from ff.logging_setup import get_logger

log = get_logger(__name__)


class Tier:
    MUST_ADD = "MUST ADD"
    HIGH_PRIORITY = "HIGH PRIORITY"
    STASH = "STASH"
    STREAMER = "STREAMER"
    SPECULATIVE = "SPECULATIVE"
    IGNORE = "IGNORE"


@dataclass
class WaiverTarget:
    player_id: str
    name: str
    position: str | None
    tier: str
    projection: float | None
    value_over_replacement: float | None
    upgrade_over_my_worst: float | None
    pct_owned: float | None
    reasoning: str
    faab_low: int | None = None
    faab_high: int | None = None
    faab_aggressive: int | None = None
    faab_max: int | None = None
    drop_candidate: str | None = None
    drop_candidate_id: str | None = None

    @property
    def actionable(self) -> bool:
        return self.tier in (Tier.MUST_ADD, Tier.HIGH_PRIORITY)


class WaiverEngine:
    def __init__(
        self,
        ctx: LeagueContext,
        valuation: ValuationEngine,
        roster: RosterEngine,
        registry: PlayerRegistry,
    ):
        self.ctx = ctx
        self.valuation = valuation
        self.roster = roster
        self.registry = registry

    # -- evaluation --------------------------------------------------------

    def evaluate(self, week: int | None = None, limit: int = 60) -> list[WaiverTarget]:
        week = week or self.ctx.current_week()
        team_id = self.ctx.my_team_id()
        if team_id is None:
            return []

        my_players = self._my_players_by_position(week)
        targets: list[WaiverTarget] = []

        for row in self.ctx.free_agents(limit=limit):
            value = self.valuation.value(row["player_id"], week)
            if not value.has_projection:
                continue
            target = self._score(value, my_players, week)
            if target.tier != Tier.IGNORE:
                targets.append(target)

        targets.sort(
            key=lambda t: (
                {Tier.MUST_ADD: 0, Tier.HIGH_PRIORITY: 1, Tier.STASH: 2,
                 Tier.STREAMER: 3, Tier.SPECULATIVE: 4}.get(t.tier, 9),
                -(t.upgrade_over_my_worst or 0),
            )
        )
        return targets

    def _my_players_by_position(self, week: int) -> dict[str, list[tuple[float, str, str]]]:
        team_id = self.ctx.my_team_id()
        out: dict[str, list[tuple[float, str, str]]] = {}
        for row in self.ctx.roster(team_id):
            position = (row["position"] or "").upper()
            value = self.valuation.value(row["player_id"], week)
            out.setdefault(position, []).append(
                (value.ros_value or value.projection or 0.0, row["player_id"], value.name)
            )
        for entries in out.values():
            entries.sort()
        return out

    def _score(
        self, value: Any, my_players: dict[str, list], week: int
    ) -> WaiverTarget:
        position = (value.position or "").upper()
        mine = my_players.get(position, [])
        slots = self.ctx.roster_slots().get(position, 1) or 1

        # The right comparison is not "my worst player at this position" but
        # "the worst player I would actually have to start or drop".
        bench_threshold = mine[0][0] if mine else 0.0
        starter_threshold = mine[-slots][0] if len(mine) >= slots else bench_threshold

        upgrade = (value.ros_value or value.projection or 0) - starter_threshold
        vor = value.value_over_replacement or 0

        tier, reasoning = self._tier_for(value, upgrade, vor, position, week)
        target = WaiverTarget(
            player_id=value.player_id,
            name=value.name,
            position=value.position,
            tier=tier,
            projection=value.projection,
            value_over_replacement=round(vor, 1),
            upgrade_over_my_worst=round(upgrade, 1),
            pct_owned=value.pct_owned,
            reasoning=reasoning,
        )

        if mine and tier in (Tier.MUST_ADD, Tier.HIGH_PRIORITY, Tier.STASH):
            worst = min(
                (p for pos in my_players.values() for p in pos),
                key=lambda p: p[0],
                default=None,
            )
            if worst and worst[0] < (value.ros_value or 0):
                target.drop_candidate = worst[2]
                target.drop_candidate_id = worst[1]

        if self.ctx.uses_faab and tier != Tier.IGNORE:
            self._attach_faab(target, value, upgrade, week)
        return target

    def _tier_for(
        self, value: Any, upgrade: float, vor: float, position: str, week: int
    ) -> tuple[str, str]:
        owned = value.pct_owned or 0

        if upgrade >= 5.0 and vor >= 3.0:
            return Tier.MUST_ADD, (
                f"Projects {upgrade:.1f} points/week better than your worst startable "
                f"{position}, and {vor:.1f} above replacement level in this league. "
                "This is a genuine starter upgrade, not a lottery ticket."
            )
        if upgrade >= 2.5:
            return Tier.HIGH_PRIORITY, (
                f"A real {upgrade:.1f} points/week upgrade over your current worst "
                f"startable {position}. Worth a claim."
            )
        if (value.playoff_value or 0) > (value.projection or 0) * 1.2:
            return Tier.STASH, (
                "Limited value now, materially more during the fantasy playoffs. "
                "Worth a bench spot if you have one to spare."
            )
        if position in ("QB", "TE", "K", "DST") and upgrade > 0:
            return Tier.STREAMER, (
                f"Marginal season-long value but a usable one-week {position} stream."
            )
        if owned and owned < 5 and (value.ros_value or 0) > 0:
            return Tier.SPECULATIVE, (
                f"Rostered in only {owned:.0f}% of leagues. Cheap upside, no obligation."
            )
        return Tier.IGNORE, (
            f"Not better than what you already have at {position} "
            f"({upgrade:+.1f} points/week). Adding him would be activity, not improvement."
        )

    # -- FAAB --------------------------------------------------------------

    def _attach_faab(self, target: WaiverTarget, value: Any, upgrade: float, week: int) -> None:
        budget = self.ctx.faab_remaining()
        settings = self.ctx.settings()
        total_budget = (settings["faab_budget"] if settings else None) or 100
        if budget is None:
            budget = total_budget

        if budget <= 0:
            target.reasoning += " (No FAAB remaining -- waiver priority or free agency only.)"
            return

        reg_weeks = (settings["reg_season_weeks"] if settings else None) or 14
        weeks_left = max(1, reg_weeks - week + 1)

        # Base share of remaining budget, scaled by how big an upgrade this is.
        # Capped hard: no single player is worth the whole season's flexibility.
        if target.tier == Tier.MUST_ADD:
            base_share = 0.30
        elif target.tier == Tier.HIGH_PRIORITY:
            base_share = 0.15
        elif target.tier == Tier.STASH:
            base_share = 0.06
        else:
            base_share = 0.02

        # Late in the season, hoarding budget has no option value left.
        urgency = 1.0 + max(0.0, (1.0 - weeks_left / max(reg_weeks, 1))) * 0.6
        posture_mult = {
            "aggressive": 1.35, "win_now": 1.45, "balanced": 1.0,
            "conservative": 0.75, "playoffs": 1.1,
        }.get(self.ctx.posture(), 1.0)

        centre = budget * base_share * urgency * posture_mult
        market = self._market_adjustment()
        centre *= market["multiplier"]

        low = max(1, int(centre * 0.8))
        high = max(low + 1, int(centre * 1.2))
        aggressive = max(high + 1, int(centre * 1.5))
        maximum = min(budget, max(aggressive + 1, int(centre * 1.85)))

        target.faab_low = low
        target.faab_high = high
        target.faab_aggressive = aggressive
        target.faab_max = maximum
        target.reasoning += (
            f" FAAB: ${low}-${high} recommended out of ${budget} remaining"
            f" ({weeks_left} weeks left). {market['note']}"
        )

    def _market_adjustment(self) -> dict[str, Any]:
        """Calibrate to how this league actually bids, not to a generic rule.

        A league where winning bids are routinely $40 needs different advice
        from one where $8 takes anything.
        """
        rows = self.ctx.query(
            "SELECT bid_amount FROM league_transactions "
            "WHERE league_id = :league_id AND bid_amount IS NOT NULL AND bid_amount > 0"
        )
        bids = [r["bid_amount"] for r in rows]
        if len(bids) < 5:
            return {
                "multiplier": 1.0,
                "note": "Bidding history is thin, so this range is a general prior "
                        "rather than a read on your league.",
            }
        median = statistics.median(bids)
        settings = self.ctx.settings()
        total = (settings["faab_budget"] if settings else None) or 100
        expected_median = total * 0.06

        multiplier = max(0.6, min(1.6, median / expected_median)) if expected_median else 1.0
        if multiplier > 1.15:
            note = f"This league bids high (median winning bid ${median:.0f}); range adjusted up."
        elif multiplier < 0.85:
            note = f"This league bids low (median winning bid ${median:.0f}); range adjusted down."
        else:
            note = f"League bidding is typical (median ${median:.0f})."
        return {"multiplier": multiplier, "note": note}

    # -- blocking ----------------------------------------------------------

    def blocking_candidates(self, week: int | None = None) -> list[dict[str, Any]]:
        """Players worth claiming purely to deny a rival.

        Rarely correct. Only worth it when a specific opponent has an obvious
        hole, the player fills exactly that hole, and you are close enough in
        the standings for their improvement to cost you. Included because the
        spec asks, flagged as usually-wrong because it usually is.
        """
        week = week or self.ctx.current_week()
        my_team = self.ctx.my_team()
        if not my_team:
            return []

        out = []
        targets = self.evaluate(week, limit=30)
        contenders = [
            t for t in self.ctx.opponents()
            if abs((t["wins"] or 0) - (my_team["wins"] or 0)) <= 1
        ]
        for target in targets:
            if target.tier not in (Tier.STREAMER, Tier.SPECULATIVE):
                continue
            for opponent in contenders:
                analysis = self.roster.analyze(opponent["team_id"], week)
                weakness = analysis.get("biggest_weakness") or {}
                if weakness.get("position") == (target.position or "").upper():
                    out.append(
                        {
                            "player_id": target.player_id,
                            "name": target.name,
                            "blocks": opponent["name"],
                            "their_weakness": weakness.get("position"),
                            "caution": (
                                "Blocking spends real budget on a player you do not want. "
                                "Only do this if the claim is cheap and you are genuinely "
                                "racing this specific team for a playoff spot."
                            ),
                        }
                    )
                    break
        return out
