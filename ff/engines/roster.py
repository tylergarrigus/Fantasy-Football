"""Roster analysis: what this team is good at, bad at, and blind to.

The interesting output is the *hidden* weakness -- the position that looks fine
on the surface because the starter is producing, but where one injury drops you
straight to waiver-wire replacement level. That is where a season quietly dies,
and it never shows up in a standings table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.valuation import ValuationEngine
from ff.logging_setup import get_logger

log = get_logger(__name__)

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")


@dataclass
class PositionGroup:
    position: str
    starters_required: int
    players: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_projection(self) -> float:
        return sum(p["projection"] or 0 for p in self.players)

    @property
    def starter_projection(self) -> float:
        return sum(
            p["projection"] or 0 for p in self.players[: self.starters_required]
        )

    @property
    def depth_after_starters(self) -> list[dict[str, Any]]:
        return self.players[self.starters_required :]

    @property
    def injured_count(self) -> int:
        return sum(
            1 for p in self.players
            if (p.get("injury_status") or "").lower() in ("out", "doubtful", "ir")
        )


class RosterEngine:
    def __init__(self, ctx: LeagueContext, valuation: ValuationEngine):
        self.ctx = ctx
        self.valuation = valuation

    def analyze(self, team_id: int | None = None, week: int | None = None) -> dict[str, Any]:
        week = week or self.ctx.current_week()
        team_id = team_id if team_id is not None else self.ctx.my_team_id()
        if team_id is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no team identified in this league"}

        groups = self._position_groups(team_id, week)
        strengths, weaknesses = self._rank_positions(groups, week)
        hidden = self._hidden_weaknesses(groups, week)

        return {
            "status": "ok",
            "league": self.ctx.name,
            "week": week,
            "positions": {
                pos: {
                    "starters_required": g.starters_required,
                    "rostered": len(g.players),
                    "starter_projection": round(g.starter_projection, 1),
                    "depth": [p["name"] for p in g.depth_after_starters],
                    "injured": g.injured_count,
                }
                for pos, g in groups.items()
            },
            "biggest_strength": strengths[0] if strengths else None,
            "biggest_weakness": weaknesses[0] if weaknesses else None,
            "hidden_weaknesses": hidden,
            "bye_week_gaps": self._bye_gaps(team_id, week),
            "injury_exposure": self._injury_exposure(groups),
            "biggest_opportunity": self._opportunity(groups, weaknesses, hidden),
        }

    def _position_groups(self, team_id: int, week: int) -> dict[str, PositionGroup]:
        slots = self.ctx.roster_slots() or {}
        groups: dict[str, PositionGroup] = {}
        for position in FANTASY_POSITIONS:
            required = int(slots.get(position, 0) or 0)
            if position in ("RB", "WR", "TE") and slots.get("FLEX"):
                pass  # FLEX handled implicitly via depth
            groups[position] = PositionGroup(position, max(required, 0))

        for row in self.ctx.roster(team_id):
            position = (row["position"] or "").upper()
            if position not in groups:
                continue
            value = self.valuation.value(row["player_id"], week)
            groups[position].players.append(
                {
                    "player_id": row["player_id"],
                    "name": value.name,
                    "projection": value.projection,
                    "vor": value.value_over_replacement,
                    "injury_status": value.injury_status,
                    "slot": row["slot"],
                    "ros_value": value.ros_value,
                }
            )

        for group in groups.values():
            group.players.sort(key=lambda p: p["projection"] or -1, reverse=True)
        return groups

    def _rank_positions(
        self, groups: dict[str, PositionGroup], week: int
    ) -> tuple[list[dict], list[dict]]:
        """Rank positions by starter value over replacement, not raw points.

        20 points from a QB is unremarkable; 20 from a TE is a weapon. Only
        comparing against replacement makes positions commensurable.
        """
        scored = []
        for position, group in groups.items():
            if not group.starters_required or not group.players:
                continue
            replacement = self.valuation.replacement_level(position, week)
            if replacement is None:
                continue
            surplus = group.starter_projection - (replacement * group.starters_required)
            scored.append(
                {
                    "position": position,
                    "surplus_over_replacement": round(surplus, 1),
                    "starter_projection": round(group.starter_projection, 1),
                    "replacement_level": round(replacement, 1),
                    "detail": ", ".join(p["name"] for p in group.players[: group.starters_required]),
                }
            )
        scored.sort(key=lambda s: s["surplus_over_replacement"], reverse=True)
        return scored, list(reversed(scored))

    def _hidden_weaknesses(
        self, groups: dict[str, PositionGroup], week: int
    ) -> list[dict[str, Any]]:
        """Positions that are fine now and catastrophic one injury later."""
        out = []
        for position, group in groups.items():
            if not group.starters_required or not group.players:
                continue
            depth = group.depth_after_starters
            replacement = self.valuation.replacement_level(position, week)
            if replacement is None:
                continue

            best_backup = depth[0]["projection"] if depth and depth[0]["projection"] else 0.0
            starter_avg = (
                group.starter_projection / group.starters_required
                if group.starters_required else 0.0
            )
            if starter_avg <= 0:
                continue

            # A backup no better than what's on waivers means you have no depth,
            # you just have a starter and a hope.
            cliff = starter_avg - max(best_backup, replacement)
            if cliff >= starter_avg * 0.45:
                out.append(
                    {
                        "position": position,
                        "severity": "high" if cliff >= starter_avg * 0.6 else "moderate",
                        "starter_avg": round(starter_avg, 1),
                        "next_man_up": round(max(best_backup, replacement), 1),
                        "drop_off": round(cliff, 1),
                        "explanation": (
                            f"Your {position} starters average {starter_avg:.1f}, but the next "
                            f"man up is worth {max(best_backup, replacement):.1f}. One injury here "
                            f"costs about {cliff:.1f} points a week and there is nothing behind it."
                        ),
                    }
                )
        out.sort(key=lambda x: x["drop_off"], reverse=True)
        return out

    def _injury_exposure(self, groups: dict[str, PositionGroup]) -> list[dict[str, Any]]:
        out = []
        for position, group in groups.items():
            for player in group.players[: max(group.starters_required, 1)]:
                status = (player.get("injury_status") or "").lower()
                if status in ("questionable", "doubtful", "out", "ir"):
                    out.append(
                        {
                            "position": position,
                            "name": player["name"],
                            "player_id": player["player_id"],
                            "status": player["injury_status"],
                            "projection": player["projection"],
                        }
                    )
        return out

    def _bye_gaps(self, team_id: int, week: int) -> list[dict[str, Any]]:
        """Weeks where byes leave a position short. Placeholder until bye data lands.

        ESPN exposes bye weeks per NFL team; wiring that in is a small follow-up.
        Reported honestly as unavailable rather than silently returning [].
        """
        return [{"status": "DATA UNAVAILABLE",
                 "reason": "NFL bye-week schedule not yet ingested"}]

    def _opportunity(
        self, groups: dict[str, PositionGroup], weaknesses: list[dict], hidden: list[dict]
    ) -> dict[str, Any] | None:
        """The single highest-leverage thing to fix."""
        if hidden and hidden[0]["severity"] == "high":
            return {
                "type": "depth",
                "position": hidden[0]["position"],
                "summary": (
                    f"Add {hidden[0]['position']} depth. "
                    f"{hidden[0]['explanation']}"
                ),
            }
        if weaknesses:
            worst = weaknesses[0]
            if worst["surplus_over_replacement"] < 0:
                return {
                    "type": "starter_upgrade",
                    "position": worst["position"],
                    "summary": (
                        f"Your {worst['position']} starters are "
                        f"{abs(worst['surplus_over_replacement']):.1f} points *below* what a "
                        f"waiver-wire replacement would give you. This is the position to fix."
                    ),
                }
        surplus = [w for w in weaknesses if w["surplus_over_replacement"] > 0]
        if surplus:
            best = max(surplus, key=lambda s: s["surplus_over_replacement"])
            return {
                "type": "trade_from_surplus",
                "position": best["position"],
                "summary": (
                    f"{best['position']} is your deepest position "
                    f"({best['surplus_over_replacement']:+.1f} over replacement). "
                    "That surplus is only worth something if you convert it in a trade."
                ),
            }
        return None
