"""Start/sit, evaluated against win probability rather than projected points.

Maximising projected points is the wrong objective and it is worth being precise
about why. If you are a heavy favourite, your job is to not blow it -- take the
boring floor. If you are a heavy underdog, the safe lineup loses slowly; you
need variance, because you only care about the tail where you win. Those two
situations recommend opposite players from the same bench.

So every alternative is scored by what it does to *this week's win probability*,
which automatically produces floor-seeking when ahead and ceiling-chasing when
behind, without special-casing either.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.simulate import FLEX_ELIGIBLE, Simulator
from ff.engines.valuation import ValuationEngine
from ff.logging_setup import get_logger

log = get_logger(__name__)

# Below this change in win probability, a lineup swap is noise, not a decision.
MEANINGFUL_WIN_PROB_DELTA = 2.0


@dataclass
class LineupSwap:
    start_player_id: str
    start_name: str
    bench_player_id: str
    bench_name: str
    slot: str
    projection_delta: float
    win_prob_before: float
    win_prob_after: float
    rationale: str

    @property
    def win_prob_delta(self) -> float:
        return round(self.win_prob_after - self.win_prob_before, 1)

    @property
    def meaningful(self) -> bool:
        return abs(self.win_prob_delta) >= MEANINGFUL_WIN_PROB_DELTA


class LineupEngine:
    def __init__(self, ctx: LeagueContext, valuation: ValuationEngine, simulator: Simulator):
        self.ctx = ctx
        self.valuation = valuation
        self.sim = simulator

    def analyze(self, week: int | None = None) -> dict[str, Any]:
        week = week or self.ctx.current_week()
        team_id = self.ctx.my_team_id()
        if team_id is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no team identified in this league"}

        current = self._current_lineup(week, team_id)
        baseline = self.sim.matchup(week, team_id)
        if baseline.get("status") != "ok":
            return baseline

        optimal_ids, opt_mean, opt_sigma, _ = self.sim.optimal_lineup(team_id, week)
        alternative = self.sim.matchup(
            week, team_id, override_mean=opt_mean, override_sigma=opt_sigma
        )

        swaps = self._find_swaps(week, team_id, current, baseline)
        posture = self._posture_for(baseline["win_probability"])

        return {
            "status": "ok",
            "week": week,
            "league": self.ctx.name,
            "current": {
                "projection": current["mean"],
                "win_probability": baseline["win_probability"],
                "floor": baseline["my_floor"],
                "ceiling": baseline["my_ceiling"],
            },
            "optimal": {
                "projection": round(opt_mean, 1),
                "win_probability": alternative.get("win_probability"),
            },
            "opponent": baseline["opponent"],
            "opponent_projection": baseline["opponent_projection"],
            "strategy": posture,
            "swaps": [s for s in swaps if s.meaningful],
            "marginal_swaps": [s for s in swaps if not s.meaningful],
            "caveats": baseline.get("caveats", []),
        }

    def _posture_for(self, win_pct: float) -> str:
        """What kind of lineup this week actually calls for."""
        if win_pct >= 70:
            return (
                "You are a heavy favourite. Prefer floor over ceiling -- the "
                "downside case is the only one that loses this game."
            )
        if win_pct <= 32:
            return (
                "You are a heavy underdog. Prefer ceiling over floor -- a safe "
                "lineup loses this matchup slowly. You need the tail."
            )
        return "Close matchup. Maximise expected points; neither tail dominates."

    def _current_lineup(self, week: int, team_id: int) -> dict[str, Any]:
        rows = self.ctx.roster(team_id)
        starters, bench = [], []
        for row in rows:
            slot = (row["slot"] or "BE").upper()
            entry = {"player_id": row["player_id"], "name": row["full_name"],
                     "position": row["position"], "slot": slot}
            (starters if slot not in ("BE", "IR", "BENCH") else bench).append(entry)

        mean = sigma_sq = 0.0
        for entry in starters:
            value = self.valuation.value(entry["player_id"], week)
            entry["projection"] = value.projection
            entry["floor"] = value.floor
            entry["ceiling"] = value.ceiling
            entry["injury_status"] = value.injury_status
            if value.projection is not None:
                mean += value.projection
                sigma_sq += (value.sigma or 0) ** 2
        for entry in bench:
            value = self.valuation.value(entry["player_id"], week)
            entry["projection"] = value.projection
            entry["floor"] = value.floor
            entry["ceiling"] = value.ceiling
            entry["injury_status"] = value.injury_status

        return {
            "starters": starters,
            "bench": bench,
            "mean": round(mean, 1),
            "sigma": round(math.sqrt(sigma_sq), 1),
        }

    def _find_swaps(
        self, week: int, team_id: int, current: dict[str, Any], baseline: dict[str, Any]
    ) -> list[LineupSwap]:
        """Every legal single swap, scored by its effect on win probability."""
        swaps: list[LineupSwap] = []
        base_mean = current["mean"]
        base_sigma = current["sigma"] or 1.0
        opp_mean = baseline["opponent_projection"]
        opp_sigma = self.sim.team_strength(
            self.ctx.matchup(week, team_id)["opponent_id"], week
        ).sigma or 1.0

        for starter in current["starters"]:
            if starter["projection"] is None:
                continue
            for benched in current["bench"]:
                if benched["projection"] is None:
                    continue
                if not self._slot_legal(starter["slot"], benched["position"]):
                    continue

                s_val = self.valuation.value(starter["player_id"], week)
                b_val = self.valuation.value(benched["player_id"], week)

                new_mean = base_mean - (s_val.projection or 0) + (b_val.projection or 0)
                new_sigma = math.sqrt(
                    max(0.0, base_sigma**2 - (s_val.sigma or 0) ** 2 + (b_val.sigma or 0) ** 2)
                ) or 1.0

                after = self._win_prob(new_mean, new_sigma, opp_mean, opp_sigma)
                swap = LineupSwap(
                    start_player_id=benched["player_id"],
                    start_name=benched["name"],
                    bench_player_id=starter["player_id"],
                    bench_name=starter["name"],
                    slot=starter["slot"],
                    projection_delta=round((b_val.projection or 0) - (s_val.projection or 0), 1),
                    win_prob_before=baseline["win_probability"],
                    win_prob_after=round(after, 1),
                    rationale=self._explain(s_val, b_val, baseline["win_probability"]),
                )
                if swap.win_prob_delta > 0:
                    swaps.append(swap)

        swaps.sort(key=lambda s: s.win_prob_delta, reverse=True)
        return swaps

    def _slot_legal(self, slot: str, position: str | None) -> bool:
        if not position:
            return False
        position = position.upper()
        slot = slot.upper()
        if slot == "FLEX":
            return position in FLEX_ELIGIBLE
        if slot in ("OP", "SUPERFLEX"):
            return position in FLEX_ELIGIBLE | {"QB"}
        return slot == position

    def _win_prob(self, mean: float, sigma: float, opp_mean: float, opp_sigma: float) -> float:
        diff_sigma = math.sqrt(sigma**2 + opp_sigma**2) or 1.0
        z = (mean - opp_mean) / diff_sigma
        return 100 * 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

    def _explain(self, starter: Any, benched: Any, win_pct: float) -> str:
        bits = []
        if benched.projection and starter.projection:
            bits.append(
                f"{benched.name} projects {benched.projection:.1f} vs "
                f"{starter.name}'s {starter.projection:.1f}"
            )
        if starter.injury_status:
            bits.append(f"{starter.name} is {starter.injury_status}")
        if win_pct >= 70 and (benched.floor or 0) > (starter.floor or 0):
            bits.append("and has the higher floor, which is what matters as a favourite")
        elif win_pct <= 32 and (benched.ceiling or 0) > (starter.ceiling or 0):
            bits.append("and has the higher ceiling, which is what matters as an underdog")
        return "; ".join(bits) or "Projection favours the swap."

    def injured_starters(self, week: int | None = None) -> list[dict[str, Any]]:
        """Starters carrying a designation -- the classic 'act before kickoff' case."""
        week = week or self.ctx.current_week()
        team_id = self.ctx.my_team_id()
        if team_id is None:
            return []
        out = []
        for row in self.ctx.roster(team_id):
            slot = (row["slot"] or "BE").upper()
            if slot in ("BE", "IR", "BENCH"):
                continue
            value = self.valuation.value(row["player_id"], week)
            if value.injury_status and value.injury_status.lower() not in ("active", "healthy"):
                out.append(
                    {
                        "player_id": row["player_id"],
                        "name": value.name,
                        "slot": slot,
                        "status": value.injury_status,
                        "projection": value.projection,
                        "notes": value.notes,
                    }
                )
        return out
