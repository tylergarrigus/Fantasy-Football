"""Monte Carlo simulation: weekly win probability and championship odds.

This is deliberately code rather than a language model. Asking a model to
produce "18% -> 23%" would be inventing precision -- it has no way to compute
it. So the arithmetic happens here, over an explicit model with stated
assumptions, and the model's job downstream is judgment and prose.

The model, stated plainly so it can be argued with:

  * A team's weekly score is normal, centred on its optimal starting lineup's
    summed projection, with variance summed across starters (independence
    assumed between players).
  * Independence is wrong in the specific case of a QB and his own receivers,
    who correlate positively. That inflates our confidence slightly on stacked
    lineups. It is not worth modelling until it changes a decision.
  * Remaining schedule is taken as-is from the league.
  * Playoff seeding follows record, then points-for as the tiebreak, which is
    the ESPN default. Leagues with unusual tiebreaks will be slightly off.

Outputs are model estimates, and every result says so. They are useful for
*comparing two choices under the same assumptions*, which is all a decision
needs -- not for predicting the future in absolute terms.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.valuation import ValuationEngine
from ff.logging_setup import get_logger

log = get_logger(__name__)

# Slots that actually score. Bench and IR do not.
STARTING_SLOTS = ("QB", "RB", "WR", "TE", "FLEX", "K", "DST", "OP", "SUPERFLEX")
FLEX_ELIGIBLE = {"RB", "WR", "TE"}


@dataclass
class TeamStrength:
    team_id: int
    name: str
    mean: float
    sigma: float
    is_mine: bool = False
    starters: list[str] = field(default_factory=list)
    incomplete: bool = False  # some starters had no projection

    def draw(self, rng: random.Random) -> float:
        return max(0.0, rng.gauss(self.mean, self.sigma))


@dataclass
class SeasonOdds:
    team_id: int
    name: str
    playoff_pct: float
    championship_pct: float
    finals_pct: float
    projected_wins: float
    seed_distribution: dict[int, float]
    iterations: int
    caveats: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.name}: playoffs ~{self.playoff_pct:.0f}%, "
            f"championship ~{self.championship_pct:.1f}% "
            f"(model estimate over {self.iterations:,} simulated seasons)"
        )


class Simulator:
    def __init__(
        self,
        ctx: LeagueContext,
        valuation: ValuationEngine,
        *,
        seed: int = 20260817,
        iterations: int = 10000,
    ):
        self.ctx = ctx
        self.valuation = valuation
        self.seed = seed
        self.iterations = iterations
        self._strength_cache: dict[int, TeamStrength] = {}

    # -- lineup construction ----------------------------------------------

    def optimal_lineup(self, team_id: int, week: int) -> tuple[list[str], float, float, bool]:
        """Best legal starting lineup -> (players, mean, sigma, incomplete).

        Greedy by projection within each slot, then FLEX from what's left. Not
        provably optimal for exotic slot configurations, but it matches how
        these lineups are actually set and is stable enough to compare against
        an alternative, which is the only use it is put to.
        """
        roster = self.ctx.roster(team_id)
        slots = self.ctx.roster_slots() or {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}

        pool: dict[str, list[tuple[float, float, str]]] = {}
        incomplete = False
        for row in roster:
            pid = row["player_id"]
            value = self.valuation.value(pid, week)
            if not value.has_projection:
                incomplete = True
                continue
            position = (value.position or row["position"] or "").upper()
            pool.setdefault(position, []).append(
                (value.projection or 0.0, value.sigma or 0.0, pid)
            )
        for entries in pool.values():
            entries.sort(reverse=True)

        chosen: list[tuple[float, float, str]] = []
        used: set[str] = set()

        for position, count in slots.items():
            position = position.upper()
            if position in ("BE", "IR", "BENCH") or position == "FLEX":
                continue
            available = [e for e in pool.get(position, []) if e[2] not in used]
            for entry in available[: int(count)]:
                chosen.append(entry)
                used.add(entry[2])

        flex_count = int(slots.get("FLEX", 0) or 0)
        if flex_count:
            flex_pool = sorted(
                (e for pos, entries in pool.items() if pos in FLEX_ELIGIBLE
                 for e in entries if e[2] not in used),
                reverse=True,
            )
            for entry in flex_pool[:flex_count]:
                chosen.append(entry)
                used.add(entry[2])

        mean = sum(e[0] for e in chosen)
        sigma = math.sqrt(sum(e[1] ** 2 for e in chosen)) if chosen else 0.0
        return [e[2] for e in chosen], mean, sigma, incomplete

    def team_strength(self, team_id: int, week: int) -> TeamStrength:
        if team_id in self._strength_cache:
            return self._strength_cache[team_id]
        row = self.ctx.one(
            "SELECT * FROM league_teams WHERE league_id = :league_id AND team_id = :tid",
            tid=team_id,
        )
        starters, mean, sigma, incomplete = self.optimal_lineup(team_id, week)
        strength = TeamStrength(
            team_id=team_id,
            name=row["name"] if row else f"Team {team_id}",
            mean=mean,
            # A team with no usable projections would otherwise simulate as a
            # guaranteed loss. Give it league-average uncertainty instead.
            sigma=sigma or 25.0,
            is_mine=bool(row and row["is_mine"]),
            starters=starters,
            incomplete=incomplete,
        )
        self._strength_cache[team_id] = strength
        return strength

    # -- weekly matchup ----------------------------------------------------

    def matchup(
        self,
        week: int,
        team_id: int | None = None,
        *,
        override_mean: float | None = None,
        override_sigma: float | None = None,
    ) -> dict[str, Any]:
        """Win probability for one matchup, optionally under an alternative lineup."""
        team_id = team_id if team_id is not None else self.ctx.my_team_id()
        if team_id is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no team identified in this league"}

        game = self.ctx.matchup(week, team_id)
        if not game or game["opponent_id"] is None:
            return {"status": "DATA UNAVAILABLE", "reason": f"no scheduled opponent for week {week}"}

        mine = self.team_strength(team_id, week)
        theirs = self.team_strength(game["opponent_id"], week)

        mean = override_mean if override_mean is not None else mine.mean
        sigma = override_sigma if override_sigma is not None else mine.sigma

        if mean <= 0 or theirs.mean <= 0:
            return {"status": "DATA UNAVAILABLE",
                    "reason": "not enough projections to simulate this matchup"}

        # Difference of two normals is normal -- closed form, no sampling needed.
        diff_sigma = math.sqrt(sigma**2 + theirs.sigma**2) or 1.0
        z = (mean - theirs.mean) / diff_sigma
        win_pct = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

        caveats = []
        if mine.incomplete or theirs.incomplete:
            caveats.append("Some rostered players had no projection and were excluded.")

        return {
            "status": "ok",
            "week": week,
            "my_projection": round(mean, 1),
            "opponent": theirs.name,
            "opponent_projection": round(theirs.mean, 1),
            "win_probability": round(win_pct * 100, 1),
            "spread": round(mean - theirs.mean, 1),
            "my_floor": round(max(0.0, mean - 1.28 * sigma), 1),
            "my_ceiling": round(mean + 1.28 * sigma, 1),
            "caveats": caveats,
            "basis": "model estimate, normal scoring approximation",
        }

    # -- full season -------------------------------------------------------

    def season(self, week: int | None = None) -> dict[int, SeasonOdds]:
        """Simulate the rest of the season many times; return odds per team.

        The headline championship number comes from here. It is an estimate
        under the assumptions at the top of this file, not a prediction.
        """
        week = week or self.ctx.current_week()
        settings = self.ctx.settings()
        if not settings:
            return {}

        reg_weeks = settings["reg_season_weeks"] or 14
        playoff_teams = settings["playoff_teams"] or 6
        teams = self.ctx.teams()
        if len(teams) < 2:
            return {}

        strengths = {t["team_id"]: self.team_strength(t["team_id"], week) for t in teams}
        records = {
            t["team_id"]: (t["wins"] or 0, t["losses"] or 0, float(t["points_for"] or 0))
            for t in teams
        }

        schedule: dict[int, list[tuple[int, int]]] = {}
        for row in self.ctx.remaining_schedule(week):
            if row["opponent_id"] is None or row["week"] > reg_weeks:
                continue
            schedule.setdefault(row["week"], [])
            pair = tuple(sorted((row["team_id"], row["opponent_id"])))
            if pair not in schedule[row["week"]]:
                schedule[row["week"]].append(pair)

        rng = random.Random(self.seed)
        team_ids = [t["team_id"] for t in teams]
        made_playoffs = dict.fromkeys(team_ids, 0)
        won_title = dict.fromkeys(team_ids, 0)
        made_finals = dict.fromkeys(team_ids, 0)
        total_wins = dict.fromkeys(team_ids, 0.0)
        seeds: dict[int, dict[int, int]] = {tid: {} for tid in team_ids}

        for _ in range(self.iterations):
            wins = {tid: records[tid][0] for tid in team_ids}
            points = {tid: records[tid][2] for tid in team_ids}

            for _wk, pairs in schedule.items():
                for a, b in pairs:
                    score_a = strengths[a].draw(rng)
                    score_b = strengths[b].draw(rng)
                    points[a] += score_a
                    points[b] += score_b
                    if score_a >= score_b:
                        wins[a] += 1
                    else:
                        wins[b] += 1

            # Seed by wins, then points-for (ESPN's default tiebreak).
            standings = sorted(team_ids, key=lambda t: (-wins[t], -points[t]))
            bracket = standings[:playoff_teams]
            for seed_no, tid in enumerate(bracket, start=1):
                made_playoffs[tid] += 1
                seeds[tid][seed_no] = seeds[tid].get(seed_no, 0) + 1
            for tid in team_ids:
                total_wins[tid] += wins[tid]

            champion, finalists = self._simulate_bracket(bracket, strengths, rng)
            won_title[champion] += 1
            for tid in finalists:
                made_finals[tid] += 1

        n = self.iterations
        caveats = ["Model estimate, not a prediction."]
        if any(s.incomplete for s in strengths.values()):
            caveats.append("Some rosters had players with no projection; those were excluded.")
        if not schedule:
            caveats.append("No remaining regular-season games found -- odds reflect current standings only.")

        return {
            tid: SeasonOdds(
                team_id=tid,
                name=strengths[tid].name,
                playoff_pct=round(100 * made_playoffs[tid] / n, 1),
                championship_pct=round(100 * won_title[tid] / n, 1),
                finals_pct=round(100 * made_finals[tid] / n, 1),
                projected_wins=round(total_wins[tid] / n, 1),
                seed_distribution={k: round(100 * v / n, 1) for k, v in sorted(seeds[tid].items())},
                iterations=n,
                caveats=caveats,
            )
            for tid in team_ids
        }

    def _simulate_bracket(
        self, bracket: list[int], strengths: dict[int, TeamStrength], rng: random.Random
    ) -> tuple[int, list[int]]:
        """Single-elimination with byes for the top seeds. Returns (champion, finalists)."""
        if not bracket:
            return 0, []
        if len(bracket) == 1:
            return bracket[0], bracket

        field_ = list(bracket)
        # Byes so the field is a power of two.
        target = 1 << (len(field_).bit_length() - 1)
        if target < len(field_):
            target <<= 1
        byes = target - len(field_)
        round_teams = field_[:byes] + [None] * 0  # top seeds advance automatically
        playing = field_[byes:]

        while len(round_teams) + len(playing) > 1:
            winners = list(round_teams)
            for i in range(0, len(playing) - 1, 2):
                a, b = playing[i], playing[i + 1]
                winners.append(a if strengths[a].draw(rng) >= strengths[b].draw(rng) else b)
            if len(playing) % 2:
                winners.append(playing[-1])
            if len(winners) == 1:
                break
            round_teams = []
            playing = winners

        finalists = playing[:2] if len(playing) >= 2 else playing
        if len(playing) == 1:
            return playing[0], finalists
        champion = playing[0] if strengths[playing[0]].draw(rng) >= strengths[playing[1]].draw(rng) else playing[1]
        return champion, finalists

    # -- decision support --------------------------------------------------

    def championship_delta(
        self, week: int, *, mean_delta: float, team_id: int | None = None
    ) -> dict[str, Any]:
        """How much does adding `mean_delta` expected points/week move title odds?

        This is what converts "he's worth 4 more points" into "that's +2.1
        percentage points of championship probability", which is the only
        framing that lets two completely different actions be compared.
        """
        team_id = team_id if team_id is not None else self.ctx.my_team_id()
        if team_id is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no team identified"}

        before = self.season(week)
        if team_id not in before:
            return {"status": "DATA UNAVAILABLE", "reason": "simulation produced no result"}

        baseline = before[team_id].championship_pct
        original = self._strength_cache.get(team_id)
        if original is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no strength estimate"}

        # Re-run with the improved team, everything else held fixed.
        self._strength_cache[team_id] = TeamStrength(
            team_id=original.team_id,
            name=original.name,
            mean=original.mean + mean_delta,
            sigma=original.sigma,
            is_mine=original.is_mine,
            starters=original.starters,
            incomplete=original.incomplete,
        )
        try:
            after = self.season(week)
            improved = after[team_id].championship_pct
        finally:
            self._strength_cache[team_id] = original

        return {
            "status": "ok",
            "before": baseline,
            "after": improved,
            "delta": round(improved - baseline, 2),
            "playoff_before": before[team_id].playoff_pct,
            "playoff_after": after[team_id].playoff_pct,
            "iterations": self.iterations,
            "basis": "model estimate; compares two scenarios under identical assumptions",
        }

    def invalidate(self) -> None:
        """Drop cached strengths -- call after any roster or projection change."""
        self._strength_cache.clear()
