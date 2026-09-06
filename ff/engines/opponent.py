"""Opponent modelling: what each manager needs, and how they actually behave.

A trade that is fair on a value chart but obviously unacceptable to the person
receiving it is worth nothing. So this tracks observed behaviour -- how often
they trade, how hard they bid, which positions they hoard -- and uses it to
judge whether a proposal has any realistic chance of being accepted.

Everything here is inferred from transactions the league has actually recorded.
Early in a season there is very little of it, and this module says so rather
than dressing up three data points as a personality profile.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.db.store import utcnow
from ff.engines.roster import RosterEngine
from ff.engines.valuation import ValuationEngine
from ff.logging_setup import get_logger

log = get_logger(__name__)

MIN_TXNS_FOR_PROFILE = 6


@dataclass
class OpponentProfile:
    team_id: int
    name: str
    wins: int
    losses: int
    standing: int | None
    faab_remaining: int | None

    needs: list[str] = field(default_factory=list)
    surplus: list[str] = field(default_factory=list)
    injured_starters: list[str] = field(default_factory=list)

    trade_activity: str = "unknown"
    waiver_aggression: str = "unknown"
    avg_bid: float | None = None
    max_bid: int | None = None
    position_bias: dict[str, float] = field(default_factory=dict)
    sample_size: int = 0
    confident: bool = False

    likely_strategy: str = "unknown"

    def describe(self) -> str:
        parts = [f"{self.name} ({self.wins}-{self.losses})"]
        if self.needs:
            parts.append(f"needs {'/'.join(self.needs)}")
        if self.surplus:
            parts.append(f"surplus at {'/'.join(self.surplus)}")
        if not self.confident:
            parts.append(f"behaviour profile based on only {self.sample_size} transactions")
        return "; ".join(parts)


class OpponentEngine:
    def __init__(self, ctx: LeagueContext, valuation: ValuationEngine, roster: RosterEngine):
        self.ctx = ctx
        self.valuation = valuation
        self.roster = roster

    def profile_all(self, week: int | None = None) -> list[OpponentProfile]:
        week = week or self.ctx.current_week()
        return [self.profile(t["team_id"], week) for t in self.ctx.opponents()]

    def profile(self, team_id: int, week: int | None = None) -> OpponentProfile:
        week = week or self.ctx.current_week()
        row = self.ctx.one(
            "SELECT * FROM league_teams WHERE league_id = :league_id AND team_id = :tid",
            tid=team_id,
        )
        if not row:
            return OpponentProfile(team_id, f"Team {team_id}", 0, 0, None, None)

        profile = OpponentProfile(
            team_id=team_id,
            name=row["name"],
            wins=row["wins"] or 0,
            losses=row["losses"] or 0,
            standing=row["standing"],
            faab_remaining=row["faab_remaining"],
        )

        analysis = self.roster.analyze(team_id, week)
        if analysis.get("status") == "ok":
            weakness = analysis.get("biggest_weakness") or {}
            strength = analysis.get("biggest_strength") or {}
            if weakness.get("position"):
                profile.needs.append(weakness["position"])
            for hidden in analysis.get("hidden_weaknesses", []):
                if hidden["position"] not in profile.needs:
                    profile.needs.append(hidden["position"])
            if strength.get("position") and strength.get("surplus_over_replacement", 0) > 0:
                profile.surplus.append(strength["position"])
            profile.injured_starters = [i["name"] for i in analysis.get("injury_exposure", [])]

        self._apply_behaviour(profile)
        profile.likely_strategy = self._infer_strategy(profile, week)
        self._persist(profile)
        return profile

    def _apply_behaviour(self, profile: OpponentProfile) -> None:
        rows = self.ctx.query(
            "SELECT * FROM league_transactions "
            "WHERE league_id = :league_id AND team_id = :tid",
            tid=profile.team_id,
        )
        profile.sample_size = len(rows)
        profile.confident = len(rows) >= MIN_TXNS_FOR_PROFILE

        if not rows:
            return

        bids = [r["bid_amount"] for r in rows if r["bid_amount"]]
        if bids:
            profile.avg_bid = round(statistics.mean(bids), 1)
            profile.max_bid = max(bids)

        trades = sum(1 for r in rows if (r["type"] or "").upper().startswith("TRADE"))
        adds = sum(1 for r in rows if (r["type"] or "").upper() in ("ADD", "WAIVER", "FA ADDED"))

        if not profile.confident:
            profile.trade_activity = "unknown (too few transactions)"
            profile.waiver_aggression = "unknown (too few transactions)"
            return

        profile.trade_activity = (
            "active" if trades >= 3 else "occasional" if trades >= 1 else "never trades"
        )
        profile.waiver_aggression = (
            "very active" if adds >= 12 else "active" if adds >= 5 else "passive"
        )

        counts: dict[str, int] = {}
        for row in rows:
            if not row["player_id"]:
                continue
            player = self.ctx.store.one(
                "SELECT position FROM nfl_players WHERE player_id = ?", (row["player_id"],)
            )
            if player and player["position"]:
                counts[player["position"]] = counts.get(player["position"], 0) + 1
        total = sum(counts.values())
        if total:
            profile.position_bias = {k: round(v / total, 2) for k, v in counts.items()}

    def _infer_strategy(self, profile: OpponentProfile, week: int) -> str:
        settings = self.ctx.settings()
        playoff_spots = (settings["playoff_teams"] if settings else None) or 6
        played = profile.wins + profile.losses

        if played < 3:
            return "too early to read"
        win_pct = profile.wins / played if played else 0
        contending = profile.standing is not None and profile.standing <= playoff_spots

        if win_pct >= 0.65:
            return "contending -- will pay up for a genuine upgrade, unlikely to sell"
        if win_pct <= 0.30 and week >= 7:
            return "out of it -- may sell veterans, and is the natural buy-low counterparty"
        if contending:
            return "on the playoff bubble -- most motivated team in the league to make a move"
        return "middling -- open to a fair trade but not desperate"

    def _persist(self, profile: OpponentProfile) -> None:
        self.ctx.execute(
            """
            INSERT INTO opponent_profiles(league_id, team_id, trades_proposed,
                                          trades_accepted, waiver_claims, avg_faab_bid,
                                          max_faab_bid, faab_spent, position_bias,
                                          notes, updated_at)
            VALUES(:league_id, :tid, 0, 0, :claims, :avg, :max, 0, :bias, :notes, :ts)
            ON CONFLICT(league_id, team_id) DO UPDATE SET
                avg_faab_bid = excluded.avg_faab_bid,
                max_faab_bid = excluded.max_faab_bid,
                position_bias = excluded.position_bias,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            tid=profile.team_id,
            claims=profile.sample_size,
            avg=profile.avg_bid,
            max=profile.max_bid,
            bias=json.dumps(profile.position_bias),
            notes=profile.likely_strategy,
            ts=utcnow(),
        )
        self.ctx.commit()

    def acceptance_probability(
        self, profile: OpponentProfile, their_gain: float, positions_sent: list[str]
    ) -> tuple[str, str]:
        """Realistic chance they say yes, as a word rather than a fake percentage.

        We have no model that supports "73.4%". What we can say honestly is
        whether this is likely, plausible, or a waste of everyone's time.
        """
        if profile.trade_activity == "never trades":
            return "very low", "This manager has not made a single trade all season."

        fills_need = any(p in profile.needs for p in positions_sent)

        if their_gain < 0:
            return "very low", "The deal is worse for them on value; there is no reason to accept."
        if their_gain < 2 and not fills_need:
            return "low", "Roughly neutral for them and it does not address a need they have."
        if fills_need and their_gain >= 2:
            return "high", (
                f"It fills their {'/'.join(p for p in positions_sent if p in profile.needs)} "
                "need and gains them value. This is the kind of offer that gets accepted."
            )
        if fills_need:
            return "moderate", "Addresses a real need for them, though the value is close."
        if their_gain >= 5:
            return "moderate", "Clear value gain for them, but not at a position they need."
        return "low", "No compelling reason for them to say yes."
