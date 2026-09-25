"""Trade discovery, scored in championship probability rather than "who won".

Two things make a trade proposal worth sending, and most trade tools only check
the first:

  1. It improves your title odds. Not your projected points -- a trade that adds
     points at a position where you were already fine can be worth nothing.
  2. The other manager plausibly says yes. A theoretically fair deal that no
     human would accept is noise, and sending it costs credibility.

So every candidate is filtered through both, and anything that passes the value
test but fails the acceptance test is reported as such rather than dressed up.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.opponent import OpponentEngine, OpponentProfile
from ff.engines.roster import RosterEngine
from ff.engines.simulate import Simulator
from ff.engines.valuation import ValuationEngine
from ff.logging_setup import get_logger

log = get_logger(__name__)

# Don't propose anything that doesn't move the needle by at least this much.
MIN_CHAMPIONSHIP_DELTA = 0.8
MAX_PLAYERS_PER_SIDE = 2


@dataclass
class TradeProposal:
    partner_team_id: int
    partner_name: str
    i_send: list[dict[str, Any]] = field(default_factory=list)
    i_receive: list[dict[str, Any]] = field(default_factory=list)

    my_value_delta: float = 0.0
    their_value_delta: float = 0.0
    championship_before: float | None = None
    championship_after: float | None = None
    acceptance: str = "unknown"
    acceptance_reason: str = ""
    rationale: str = ""
    category: str = "balanced"  # best | realistic | buy_low | sell_high | avoid

    @property
    def championship_delta(self) -> float | None:
        if self.championship_before is None or self.championship_after is None:
            return None
        return round(self.championship_after - self.championship_before, 2)

    @property
    def worth_proposing(self) -> bool:
        delta = self.championship_delta
        return (
            delta is not None
            and delta >= MIN_CHAMPIONSHIP_DELTA
            and self.acceptance in ("moderate", "high")
        )

    def describe(self) -> str:
        send = ", ".join(p["name"] for p in self.i_send)
        recv = ", ".join(p["name"] for p in self.i_receive)
        delta = self.championship_delta
        line = f"Send {send} -> receive {recv} ({self.partner_name})"
        if delta is not None:
            line += f" | title odds {self.championship_before:.1f}% -> {self.championship_after:.1f}% ({delta:+.1f})"
        line += f" | acceptance: {self.acceptance}"
        return line


class TradeEngine:
    def __init__(
        self,
        ctx: LeagueContext,
        valuation: ValuationEngine,
        roster: RosterEngine,
        opponent: OpponentEngine,
        simulator: Simulator,
    ):
        self.ctx = ctx
        self.valuation = valuation
        self.roster = roster
        self.opponent = opponent
        self.sim = simulator

    # -- discovery ---------------------------------------------------------

    def discover(self, week: int | None = None, max_proposals: int = 5) -> dict[str, Any]:
        week = week or self.ctx.current_week()
        my_team_id = self.ctx.my_team_id()
        if my_team_id is None:
            return {"status": "DATA UNAVAILABLE", "reason": "no team identified in this league"}

        my_analysis = self.roster.analyze(my_team_id, week)
        if my_analysis.get("status") != "ok":
            return my_analysis

        my_needs = self._needs_from(my_analysis)
        my_surplus = self._surplus_from(my_analysis)
        if not my_needs and not my_surplus:
            return {
                "status": "ok",
                "proposals": [],
                "note": "Roster is balanced -- no obvious trade angle. Doing nothing is correct.",
            }

        untouchable = self.ctx.untouchable_players()
        avoided = {t.lower() for t in self.ctx.avoided_teams()}
        baseline = self.sim.season(week).get(my_team_id)
        baseline_pct = baseline.championship_pct if baseline else None

        proposals: list[TradeProposal] = []
        for profile in self.opponent.profile_all(week):
            if profile.name.lower() in avoided:
                continue
            proposals.extend(
                self._proposals_with(
                    profile, my_needs, my_surplus, untouchable, week, baseline_pct
                )
            )

        for proposal in proposals:
            self._categorize(proposal)

        viable = [p for p in proposals if p.worth_proposing]
        viable.sort(key=lambda p: p.championship_delta or 0, reverse=True)

        return {
            "status": "ok",
            "league": self.ctx.name,
            "week": week,
            "my_needs": my_needs,
            "my_surplus": my_surplus,
            "baseline_championship": baseline_pct,
            "best": viable[0] if viable else None,
            "most_realistic": self._most_realistic(viable),
            "proposals": viable[:max_proposals],
            "rejected": [
                {"summary": p.describe(), "why": p.acceptance_reason}
                for p in proposals
                if not p.worth_proposing
            ][:5],
            "note": (
                "No trade currently improves your title odds enough to be worth "
                "proposing. Holding is the right move."
                if not viable else None
            ),
        }

    def _proposals_with(
        self,
        profile: OpponentProfile,
        my_needs: list[str],
        my_surplus: list[str],
        untouchable: set[str],
        week: int,
        baseline_pct: float | None,
    ) -> list[TradeProposal]:
        """Candidate one-for-one and two-for-two deals with one manager."""
        my_team_id = self.ctx.my_team_id()
        mine = self._tradeable(my_team_id, week, positions=my_surplus, exclude=untouchable)
        theirs = self._tradeable(profile.team_id, week, positions=my_needs, exclude=set())
        if not mine or not theirs:
            return []

        # Cap the search. Every combination costs a season simulation, and the
        # marginal value of the 200th candidate deal is zero.
        mine = sorted(mine, key=lambda p: p["trade_score"], reverse=True)[:5]
        theirs = sorted(theirs, key=lambda p: p["trade_score"], reverse=True)[:5]

        out: list[TradeProposal] = []
        for send_count in range(1, MAX_PLAYERS_PER_SIDE + 1):
            for recv_count in range(1, MAX_PLAYERS_PER_SIDE + 1):
                for send in itertools.combinations(mine, send_count):
                    for recv in itertools.combinations(theirs, recv_count):
                        proposal = self._evaluate(
                            profile, list(send), list(recv), week, baseline_pct
                        )
                        if proposal:
                            out.append(proposal)
        return out

    def _tradeable(
        self, team_id: int, week: int, positions: list[str], exclude: set[str]
    ) -> list[dict[str, Any]]:
        out = []
        for row in self.ctx.roster(team_id):
            if row["player_id"] in exclude:
                continue
            position = (row["position"] or "").upper()
            if positions and position not in positions:
                continue
            trade_value = self.valuation.trade_value(row["player_id"], week)
            if trade_value.get("status") != "ok":
                continue
            out.append(
                {
                    "player_id": row["player_id"],
                    "name": row["full_name"],
                    "position": position,
                    "trade_score": trade_value["score"],
                    "vor": trade_value.get("value_over_replacement") or 0,
                }
            )
        return out

    def _evaluate(
        self,
        profile: OpponentProfile,
        send: list[dict],
        receive: list[dict],
        week: int,
        baseline_pct: float | None,
    ) -> TradeProposal | None:
        send_value = sum(p["trade_score"] for p in send)
        recv_value = sum(p["trade_score"] for p in receive)

        # A wildly lopsided ask wastes the proposal; skip before simulating.
        if recv_value > send_value * 1.9:
            return None

        proposal = TradeProposal(
            partner_team_id=profile.team_id,
            partner_name=profile.name,
            i_send=send,
            i_receive=receive,
            my_value_delta=round(recv_value - send_value, 2),
            their_value_delta=round(send_value - recv_value, 2),
        )

        # Net change to my weekly starting strength -- what actually moves odds.
        net = sum(p["vor"] for p in receive) - sum(p["vor"] for p in send)
        if abs(net) < 0.5:
            return None

        delta = self.sim.championship_delta(week, mean_delta=net)
        if delta.get("status") == "ok":
            proposal.championship_before = delta["before"]
            proposal.championship_after = delta["after"]
        elif baseline_pct is not None:
            proposal.championship_before = baseline_pct

        acceptance, reason = self.opponent.acceptance_probability(
            profile, proposal.their_value_delta, [p["position"] for p in send]
        )
        proposal.acceptance = acceptance
        proposal.acceptance_reason = reason
        proposal.rationale = self._rationale(proposal, profile, net)
        return proposal

    def _rationale(self, proposal: TradeProposal, profile: OpponentProfile, net: float) -> str:
        send = ", ".join(p["name"] for p in proposal.i_send)
        recv = ", ".join(p["name"] for p in proposal.i_receive)
        bits = [
            f"You give up {send} from a position of surplus and get {recv} where you are thin.",
            f"Net effect on your weekly starting lineup: {net:+.1f} points.",
        ]
        if profile.needs:
            bits.append(f"{profile.name} needs {'/'.join(profile.needs)}.")
        if not profile.confident:
            bits.append(
                f"Behavioural read on {profile.name} is weak "
                f"({profile.sample_size} transactions observed) -- treat acceptance "
                "odds as a rough guess."
            )
        return " ".join(bits)

    def _categorize(self, proposal: TradeProposal) -> None:
        delta = proposal.championship_delta
        if delta is None:
            proposal.category = "unknown"
        elif delta < 0:
            proposal.category = "avoid"
        elif proposal.my_value_delta > 3 and proposal.acceptance in ("moderate", "high"):
            proposal.category = "buy_low"
        elif proposal.their_value_delta > 3 and delta > 0:
            proposal.category = "sell_high"
        elif proposal.acceptance == "high":
            proposal.category = "realistic"
        else:
            proposal.category = "balanced"

    def _most_realistic(self, proposals: list[TradeProposal]) -> TradeProposal | None:
        high = [p for p in proposals if p.acceptance == "high"]
        if high:
            return max(high, key=lambda p: p.championship_delta or 0)
        return proposals[0] if proposals else None

    # -- incoming offers ---------------------------------------------------

    def evaluate_incoming(
        self, send_ids: list[str], receive_ids: list[str], week: int | None = None
    ) -> dict[str, Any]:
        """Should I accept this? Answered in title odds, not vibes."""
        week = week or self.ctx.current_week()
        send, receive = [], []
        for pid in send_ids:
            value = self.valuation.trade_value(pid, week)
            if value.get("status") != "ok":
                return {"status": "DATA UNAVAILABLE",
                        "reason": f"cannot value {self.valuation.registry.name_of(pid)}"}
            send.append((pid, value))
        for pid in receive_ids:
            value = self.valuation.trade_value(pid, week)
            if value.get("status") != "ok":
                return {"status": "DATA UNAVAILABLE",
                        "reason": f"cannot value {self.valuation.registry.name_of(pid)}"}
            receive.append((pid, value))

        net_vor = sum(v.get("value_over_replacement") or 0 for _, v in receive) - sum(
            v.get("value_over_replacement") or 0 for _, v in send
        )
        delta = self.sim.championship_delta(week, mean_delta=net_vor)

        untouchable = self.ctx.untouchable_players()
        blocked = [pid for pid in send_ids if pid in untouchable]

        if blocked:
            names = ", ".join(self.valuation.registry.name_of(p) for p in blocked)
            verdict = f"REJECT -- you asked me never to trade {names}."
        elif delta.get("status") != "ok":
            verdict = "DATA UNAVAILABLE -- not enough projection data to judge this."
        elif delta["delta"] >= 1.0:
            verdict = f"ACCEPT -- raises your title odds by about {delta['delta']:.1f} points."
        elif delta["delta"] <= -1.0:
            verdict = f"REJECT -- lowers your title odds by about {abs(delta['delta']):.1f} points."
        else:
            verdict = "NEUTRAL -- roughly a wash. No reason to accept, no reason to be offended."

        return {
            "status": "ok",
            "verdict": verdict,
            "championship_before": delta.get("before"),
            "championship_after": delta.get("after"),
            "championship_delta": delta.get("delta"),
            "net_value_over_replacement": round(net_vor, 2),
            "sending": [self.valuation.registry.name_of(p) for p in send_ids],
            "receiving": [self.valuation.registry.name_of(p) for p in receive_ids],
        }
