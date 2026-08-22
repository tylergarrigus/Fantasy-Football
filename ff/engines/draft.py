"""Draft board and live draft assistance.

The two calculations that actually decide a draft, and that most tools do badly:

  1. VALUE OVER REPLACEMENT, not raw projection. A 280-point RB and a 280-point
     WR are not equivalent picks. What matters is the gap to the player you
     could get at that position instead -- and that gap depends entirely on how
     many of each position your league starts.

  2. WILL THIS TIER SURVIVE UNTIL MY NEXT PICK? Everything else is noise. In a
     snake draft your next pick is a known number of selections away, and ADP
     tells you roughly how many of the players you want will be gone by then.
     If the tier empties before you pick again, you take from it now. If it
     won't, you take the scarcer position and come back. That single question
     answers "reach or wait?" better than any ranking list.

Nothing here invents a projection. If ESPN gave us no number for a player, he is
absent from the board rather than silently assigned a guess.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.logging_setup import get_logger

log = get_logger(__name__)

DRAFTABLE = ("QB", "RB", "WR", "TE", "K", "DST")

# Positions where a top-end player is genuinely scarce, and positions where
# waiting is nearly free. Drafting a K or DST before the last two rounds is
# lighting a pick on fire, and the board should say so.
STREAMABLE = {"K", "DST"}


@dataclass
class BoardPlayer:
    player_id: str
    name: str
    position: str
    projected: float
    vor: float
    adp: float | None
    draft_rank: int | None
    tier: int = 0
    tier_size_remaining: int = 0
    drafted: bool = False
    injury_status: str | None = None

    @property
    def adp_value(self) -> float | None:
        """Positive means he is lasting past where he usually goes."""
        return None if self.adp is None else self.adp

    def describe(self) -> str:
        adp = f"ADP {self.adp:.0f}" if self.adp else "ADP n/a"
        return (
            f"{self.name:<24} {self.position:<4} "
            f"proj {self.projected:>6.1f}  VOR {self.vor:>+6.1f}  "
            f"T{self.tier}  {adp}"
        )


@dataclass
class DraftAdvice:
    pick_number: int | None
    round_num: int | None
    recommendation: BoardPlayer | None
    alternatives: list[BoardPlayer] = field(default_factory=list)
    reasoning: str = ""
    roster_needs: list[str] = field(default_factory=list)
    tier_warnings: list[str] = field(default_factory=list)
    next_pick: int | None = None
    picks_until_next: int | None = None
    caveats: list[str] = field(default_factory=list)


class DraftEngine:
    """Draft board for one league, under that league's scoring and roster rules."""

    def __init__(self, ctx: LeagueContext, draft_slot: int | None = None):
        self.ctx = ctx
        # Before the draft starts no picks exist, so the slot cannot be inferred
        # from pick history -- it has to come from ESPN's published draft order.
        self._draft_slot = draft_slot
        self._board: list[BoardPlayer] | None = None

    # -- board construction ------------------------------------------------

    def build_board(self, refresh: bool = False) -> list[BoardPlayer]:
        if self._board is not None and not refresh:
            return self._board

        rows = self.ctx.query(
            "SELECT d.*, p.full_name, p.position, p.injury_status "
            "FROM draft_rankings d JOIN nfl_players p ON p.player_id = d.player_id "
            "WHERE d.league_id = :league_id AND d.projected IS NOT NULL "
            "ORDER BY d.projected DESC"
        )
        if not rows:
            self._board = []
            return []

        drafted = self.drafted_player_ids()
        by_position: dict[str, list[dict]] = {}
        for row in rows:
            position = (row["position"] or "").upper()
            if position not in DRAFTABLE:
                continue
            by_position.setdefault(position, []).append(dict(row))

        replacement = self._replacement_levels(by_position)

        board: list[BoardPlayer] = []
        for position, players in by_position.items():
            baseline = replacement.get(position)
            if baseline is None:
                continue
            for row in players:
                board.append(
                    BoardPlayer(
                        player_id=row["player_id"],
                        name=row["full_name"],
                        position=position,
                        projected=float(row["projected"]),
                        vor=round(float(row["projected"]) - baseline, 1),
                        adp=row["adp"],
                        draft_rank=row["draft_rank"],
                        drafted=row["player_id"] in drafted,
                        injury_status=row["injury_status"],
                    )
                )

        board.sort(key=lambda p: p.vor, reverse=True)
        self._assign_tiers(board)
        self._board = board
        return board

    def _replacement_levels(self, by_position: dict[str, list[dict]]) -> dict[str, float]:
        """Projection of the last starter-quality player at each position.

        This is what makes cross-position comparison honest. In a 12-team league
        starting 2 RB and a flex, roughly the 30th RB is replacement level; the
        14th QB is. That difference is why an elite QB is usually a worse pick
        than a good RB, and it falls straight out of the arithmetic rather than
        needing a rule of thumb.
        """
        teams = len(self.ctx.teams()) or 12
        slots = self.ctx.roster_slots() or {}
        flex = int(slots.get("FLEX", 0) or 0)

        levels: dict[str, float] = {}
        for position, players in by_position.items():
            starters = int(slots.get(position, 0) or 0)
            if starters == 0 and position not in STREAMABLE:
                continue
            # Flex demand lands on RB/WR/TE. Split it by how these leagues
            # actually use it rather than evenly: flex is mostly RB and WR.
            flex_share = {"RB": 0.45, "WR": 0.45, "TE": 0.10}.get(position, 0.0)
            effective = starters + (flex * flex_share)
            index = max(0, int(round(teams * effective)) - 1)
            projections = sorted(
                (float(p["projected"]) for p in players if p["projected"] is not None),
                reverse=True,
            )
            if not projections:
                continue
            levels[position] = projections[min(index, len(projections) - 1)]
        return levels

    def _assign_tiers(self, board: list[BoardPlayer]) -> None:
        """Cluster by value gaps, per position.

        A tier break is where the drop to the next player is unusually large --
        that is the moment waiting actually costs you something. Fixed-size
        tiers ("top 12") hide exactly the information you need.
        """
        for position in DRAFTABLE:
            players = [p for p in board if p.position == position]
            if len(players) < 3:
                for p in players:
                    p.tier = 1
                continue

            players.sort(key=lambda p: p.projected, reverse=True)
            gaps = [
                players[i].projected - players[i + 1].projected
                for i in range(len(players) - 1)
            ]
            if not gaps:
                continue
            # A gap is a tier break if it is meaningfully larger than typical.
            try:
                threshold = statistics.mean(gaps) + statistics.stdev(gaps)
            except statistics.StatisticsError:
                threshold = statistics.mean(gaps) * 2

            tier = 1
            for i, player in enumerate(players):
                player.tier = tier
                if i < len(gaps) and gaps[i] >= threshold:
                    tier += 1

            # How many undrafted players are left in each tier -- the number
            # that actually drives the "take him now or wait" call.
            counts: dict[int, int] = {}
            for player in players:
                if not player.drafted:
                    counts[player.tier] = counts.get(player.tier, 0) + 1
            for player in players:
                player.tier_size_remaining = counts.get(player.tier, 0)

    # -- draft state -------------------------------------------------------

    def drafted_player_ids(self) -> set[str]:
        rows = self.ctx.query(
            "SELECT player_id FROM draft_picks "
            "WHERE league_id = :league_id AND player_id IS NOT NULL"
        )
        return {r["player_id"] for r in rows}

    def picks_made(self) -> int:
        row = self.ctx.one(
            "SELECT COUNT(*) AS n FROM draft_picks WHERE league_id = :league_id"
        )
        return row["n"] if row else 0

    def my_draft_slot(self) -> int | None:
        """Which seat I'm drafting from."""
        if self._draft_slot:
            return self._draft_slot
        my_team = self.ctx.my_team_id()
        if my_team is None:
            return None
        row = self.ctx.one(
            "SELECT round_pick FROM draft_picks "
            "WHERE league_id = :league_id AND team_id = :tid AND round_num = 1",
            tid=my_team,
        )
        return row["round_pick"] if row else None

    def snake_picks(self, slot: int, rounds: int = 16) -> list[int]:
        """Every overall pick number I own, in a snake draft."""
        teams = len(self.ctx.teams()) or 12
        picks = []
        for rnd in range(1, rounds + 1):
            if rnd % 2 == 1:
                picks.append((rnd - 1) * teams + slot)
            else:
                picks.append((rnd - 1) * teams + (teams - slot + 1))
        return picks

    def available(self) -> list[BoardPlayer]:
        return [p for p in self.build_board() if not p.drafted]

    # -- roster construction ----------------------------------------------

    def my_roster_counts(self) -> dict[str, int]:
        my_team = self.ctx.my_team_id()
        if my_team is None:
            return {}
        rows = self.ctx.query(
            "SELECT p.position FROM draft_picks d "
            "JOIN nfl_players p ON p.player_id = d.player_id "
            "WHERE d.league_id = :league_id AND d.team_id = :tid",
            tid=my_team,
        )
        counts: dict[str, int] = {}
        for row in rows:
            position = (row["position"] or "").upper()
            counts[position] = counts.get(position, 0) + 1
        return counts

    def roster_needs(self) -> list[str]:
        """Positions where I still need a starter, most urgent first."""
        slots = self.ctx.roster_slots() or {}
        have = self.my_roster_counts()
        needs = []
        for position in ("RB", "WR", "QB", "TE", "K", "DST"):
            required = int(slots.get(position, 0) or 0)
            if required and have.get(position, 0) < required:
                needs.append(position)
        flex = int(slots.get("FLEX", 0) or 0)
        if flex:
            flex_capable = sum(have.get(p, 0) for p in ("RB", "WR", "TE"))
            flex_required = sum(
                int(slots.get(p, 0) or 0) for p in ("RB", "WR", "TE")
            ) + flex
            if flex_capable < flex_required:
                needs.append("FLEX")
        return needs

    # -- the actual advice -------------------------------------------------

    def advise(self, pick_number: int | None = None) -> DraftAdvice:
        board = self.build_board(refresh=True)
        if not board:
            return DraftAdvice(
                pick_number=None, round_num=None, recommendation=None,
                caveats=[
                    "DATA UNAVAILABLE -- no draft rankings stored for this league. "
                    "Run `ff draft sync` first."
                ],
            )

        teams = len(self.ctx.teams()) or 12
        made = self.picks_made()
        pick_number = pick_number or (made + 1)
        round_num = ((pick_number - 1) // teams) + 1

        slot = self.my_draft_slot()
        next_pick = picks_until = None
        if slot:
            mine = [p for p in self.snake_picks(slot) if p > pick_number]
            if mine:
                next_pick = mine[0]
                picks_until = next_pick - pick_number

        available = self.available()
        needs = self.roster_needs()
        advice = DraftAdvice(
            pick_number=pick_number,
            round_num=round_num,
            recommendation=None,
            roster_needs=needs,
            next_pick=next_pick,
            picks_until_next=picks_until,
        )

        # Late-round K/DST discipline. Taking either early is a real, common,
        # entirely avoidable mistake.
        total_rounds = sum(int(v or 0) for v in (self.ctx.roster_slots() or {}).values()) or 16
        candidates = [
            p for p in available
            if not (p.position in STREAMABLE and round_num < total_rounds - 1)
        ]
        if not candidates:
            candidates = available

        scored = [(self._score(p, needs, picks_until, available), p) for p in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        if scored:
            advice.recommendation = scored[0][1]
            advice.alternatives = [p for _s, p in scored[1:5]]
            advice.reasoning = self._explain(
                scored[0][1], needs, picks_until, available, round_num
            )

        advice.tier_warnings = self._tier_warnings(available, picks_until, needs)
        if any(p.adp is None for p in available[:20]):
            advice.caveats.append("Some top players have no ADP; wait/reach calls are weaker.")
        return advice

    def _score(
        self, player: BoardPlayer, needs: list[str], picks_until: int | None,
        available: list[BoardPlayer],
    ) -> float:
        """Rank candidates. VOR is the spine; everything else adjusts it."""
        score = player.vor

        # Filling a starting slot you still need is worth more than depth.
        if player.position in needs:
            score += 8.0
        elif "FLEX" in needs and player.position in ("RB", "WR", "TE"):
            score += 3.0

        # Scarcity: if his tier will not survive to my next pick, take him now.
        if picks_until and player.tier_size_remaining:
            if player.tier_size_remaining <= picks_until * 0.4:
                score += 10.0
            elif player.tier_size_remaining <= picks_until * 0.8:
                score += 4.0

        # Value against the market. Lasting well past ADP is a real edge;
        # reaching well before it is a real cost.
        if player.adp:
            expected_pick = player.adp
            actual = (self.picks_made() + 1)
            slide = expected_pick - actual
            score += max(-8.0, min(8.0, slide * 0.4))

        if (player.injury_status or "").upper() in ("OUT", "INJURY_RESERVE", "DOUBTFUL"):
            score -= 12.0
        return score

    def _explain(
        self, player: BoardPlayer, needs: list[str], picks_until: int | None,
        available: list[BoardPlayer], round_num: int,
    ) -> str:
        bits = [
            f"{player.name} ({player.position}) projects {player.projected:.0f} points, "
            f"{player.vor:+.0f} over replacement at his position."
        ]
        if player.position in needs:
            bits.append(f"You still need a starting {player.position}.")

        if picks_until:
            same_tier = [
                p for p in available
                if p.position == player.position and p.tier == player.tier
            ]
            if len(same_tier) <= picks_until * 0.4:
                bits.append(
                    f"Only {len(same_tier)} player(s) left in this tier and "
                    f"{picks_until} picks until you're up again -- this tier will be "
                    "gone. Take him now."
                )
            elif len(same_tier) > picks_until:
                bits.append(
                    f"{len(same_tier)} players remain in his tier against {picks_until} "
                    "picks before your next -- you could wait on this position and "
                    "still get comparable value."
                )

        if player.adp:
            actual = self.picks_made() + 1
            slide = player.adp - actual
            if slide > 8:
                bits.append(f"He typically goes around {player.adp:.0f} -- clear value here.")
            elif slide < -8:
                bits.append(
                    f"Note this is a reach: ADP {player.adp:.0f} against pick {actual}. "
                    "Justified only by your roster needs."
                )
        if player.injury_status and player.injury_status.upper() != "ACTIVE":
            bits.append(f"FACT: currently listed {player.injury_status}.")
        return " ".join(bits)

    def _tier_warnings(
        self, available: list[BoardPlayer], picks_until: int | None, needs: list[str]
    ) -> list[str]:
        """Cliffs about to fall off before your next pick."""
        if not picks_until:
            return []
        warnings = []
        for position in ("RB", "WR", "TE", "QB"):
            players = [p for p in available if p.position == position]
            if not players:
                continue
            top_tier = min(p.tier for p in players)
            remaining = [p for p in players if p.tier == top_tier]
            if len(remaining) <= max(1, int(picks_until * 0.4)):
                urgency = "you need one" if position in needs else "positional run risk"
                warnings.append(
                    f"{position}: only {len(remaining)} left in the top available tier "
                    f"({', '.join(p.name for p in remaining[:3])}) with {picks_until} "
                    f"picks until you're up -- {urgency}."
                )
        return warnings

    # -- pre-draft strategy ------------------------------------------------

    def strategy(self) -> dict[str, Any]:
        """A read on this specific league before the draft starts.

        The point is that the strategy is derived from *this* league's scoring
        and roster rules, not from generic advice. A superflex league and a
        TE-premium league want completely different things.
        """
        board = self.build_board(refresh=True)
        if not board:
            return {
                "status": "DATA UNAVAILABLE",
                "reason": "no rankings stored -- run `ff draft sync` first",
            }

        slots = self.ctx.roster_slots() or {}
        teams = len(self.ctx.teams()) or 12
        scoring = self.ctx.scoring()

        positional: dict[str, Any] = {}
        for position in ("QB", "RB", "WR", "TE"):
            players = sorted(
                (p for p in board if p.position == position),
                key=lambda p: p.vor, reverse=True,
            )
            if not players:
                continue
            starters = int(slots.get(position, 0) or 0)
            elite = [p for p in players if p.tier == 1]
            positional[position] = {
                "starters_required": starters,
                "elite_tier_size": len(elite),
                "elite_names": [p.name for p in elite[:6]],
                "vor_of_best": players[0].vor,
                # How fast value falls off is what tells you whether to attack
                # a position early or punt it.
                "cliff_after": len(elite),
                "drop_after_elite": round(
                    players[0].projected - players[min(len(elite), len(players) - 1)].projected, 1
                ),
            }

        # Where is the steepest early value? That's where the draft is won.
        priority = sorted(
            positional.items(),
            key=lambda kv: kv[1]["vor_of_best"],
            reverse=True,
        )

        superflex = bool(slots.get("OP") or slots.get("SUPERFLEX"))
        notes = []
        if superflex:
            notes.append(
                "This is a superflex league -- QB value is dramatically higher than "
                "standard. Two startable QBs is close to mandatory."
            )
        if int(slots.get("TE", 0) or 0) > 1:
            notes.append("Multiple TE slots -- TE scarcity is much sharper than usual.")
        if scoring.get("ppr") or "ppr" in str(scoring).lower():
            notes.append("PPR scoring -- pass-catching backs and volume receivers gain.")

        return {
            "status": "ok",
            "league": self.ctx.name,
            "teams": teams,
            "roster_slots": slots,
            "positional": positional,
            "priority_order": [p[0] for p in priority],
            "notes": notes,
            "board_size": len(board),
        }
