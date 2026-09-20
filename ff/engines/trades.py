"""Lineup construction and trade discovery over a whole league.

The only trade worth proposing is one the other manager would actually accept,
so every candidate here is scored from both sides. A deal that helps us and
hurts them is not a trade, it is a message they will ignore.

Value is measured as *starting lineup points*, never as raw player projection.
Two running backs are worth one to a team that can only start two of them, and
that gap is the entire reason a trade exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ESPN names the defense slot "D/ST" in roster settings and gives its players
# the same position string. Spelling it "DST" silently drops the slot.
FIXED_SLOTS = ("QB", "RB", "WR", "TE", "K", "D/ST")
FLEX_SLOT = "RB/WR/TE"
FLEX_POSITIONS = ("RB", "WR", "TE")

# Managers judge offers by name value before lineup value. An offer where the
# raw projection you send is far below what you ask for reads as an insult and
# gets declined on sight, no matter how sound the lineup math is -- learned the
# hard way. Below this send/receive ratio a deal is never proposed.
FAIRNESS_FLOOR = 0.7


def _looks_fair(send, receive) -> bool:
    asked = sum(p.get("projected") or 0 for p in receive)
    offered = sum(p.get("projected") or 0 for p in send)
    return asked <= 0 or offered / asked >= FAIRNESS_FLOOR


@dataclass
class Lineup:
    starters: list[dict]
    bench: list[dict]
    projected: float
    unfilled: list[str] = field(default_factory=list)


def best_lineup(players: Iterable[dict], slots: dict[str, Any]) -> Lineup:
    """Fill the starting slots greedily by projection.

    Greedy is correct here: no fixed slot competes with another for the same
    player, and the flex takes whoever the fixed slots left behind. There is no
    arrangement where benching a better player at a fixed slot frees up more
    points somewhere else.
    """
    pool = sorted(
        (p for p in players if p.get("projected") is not None),
        key=lambda p: -p["projected"],
    )
    used: set[str] = set()
    starters: list[dict] = []
    unfilled: list[str] = []

    for pos in FIXED_SLOTS:
        count = int(slots.get(pos, 0) or 0)
        taken = 0
        for p in pool:
            if taken >= count:
                break
            if p["player_id"] in used or p["position"] != pos:
                continue
            used.add(p["player_id"])
            starters.append({**p, "slot": pos})
            taken += 1
        if taken < count:
            unfilled.extend([pos] * (count - taken))

    flex_count = int(slots.get(FLEX_SLOT, 0) or 0)
    taken = 0
    for p in pool:
        if taken >= flex_count:
            break
        if p["player_id"] in used or p["position"] not in FLEX_POSITIONS:
            continue
        used.add(p["player_id"])
        starters.append({**p, "slot": "FLEX"})
        taken += 1
    if taken < flex_count:
        unfilled.extend(["FLEX"] * (flex_count - taken))

    return Lineup(
        starters=starters,
        bench=[p for p in pool if p["player_id"] not in used],
        projected=round(sum(p["projected"] for p in starters), 1),
        unfilled=unfilled,
    )


def lineup_value(players: Iterable[dict], slots: dict[str, Any]) -> float:
    return best_lineup(players, slots).projected


@dataclass
class TradeIdea:
    partner_id: int
    partner_name: str
    send: list[dict]
    receive: list[dict]
    my_gain: float
    their_gain: float

    @property
    def total_gain(self) -> float:
        return self.my_gain + self.their_gain

    def as_dict(self) -> dict[str, Any]:
        return {
            "partner_id": self.partner_id,
            "partner_name": self.partner_name,
            "send": [
                {"name": p["name"], "position": p["position"],
                 "projected": p.get("projected")}
                for p in self.send
            ],
            "receive": [
                {"name": p["name"], "position": p["position"],
                 "projected": p.get("projected")}
                for p in self.receive
            ],
            "my_gain": round(self.my_gain, 1),
            "their_gain": round(self.their_gain, 1),
        }


def _swap(roster: Sequence[dict], out: Sequence[dict], into: Sequence[dict]) -> list[dict]:
    gone = {p["player_id"] for p in out}
    return [p for p in roster if p["player_id"] not in gone] + list(into)


def find_trades(
    my_players: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    min_my_gain: float = 8.0,
    min_their_gain: float = 5.0,
    untouchable: Sequence[str] = (),
    limit: int = 12,
) -> list[TradeIdea]:
    """Every 1-for-1 and 2-for-1 that makes both starting lineups better.

    Both thresholds matter. Ours keeps us from trading for noise -- a projection
    is not precise enough for a two-point edge to mean anything. Theirs is what
    makes the offer plausible: a manager who gains nothing says no, and an offer
    that is obviously lopsided costs credibility for the next one.
    """
    protected = set(untouchable)
    mine = [p for p in my_players if p.get("projected") is not None]
    my_base = lineup_value(mine, slots)

    ideas: list[TradeIdea] = []
    for opp in opponents:
        theirs = [p for p in opp.get("players", []) if p.get("projected") is not None]
        if not theirs:
            continue
        their_base = lineup_value(theirs, slots)

        for give in _packages(mine, protected):
            for get in theirs:
                if not _looks_fair(give, [get]):
                    continue
                # Straight positional duplicates rarely help anyone, but let the
                # lineup math decide rather than guessing from position alone.
                my_after = lineup_value(_swap(mine, give, [get]), slots)
                my_gain = my_after - my_base
                if my_gain < min_my_gain:
                    continue
                their_after = lineup_value(_swap(theirs, [get], give), slots)
                their_gain = their_after - their_base
                if their_gain < min_their_gain:
                    continue
                ideas.append(
                    TradeIdea(
                        partner_id=opp["team_id"],
                        partner_name=opp["name"],
                        send=list(give),
                        receive=[get],
                        my_gain=my_gain,
                        their_gain=their_gain,
                    )
                )

    # Rank by our gain, but break ties toward the deal the other side likes
    # more -- that is the one that actually gets accepted.
    ideas.sort(key=lambda t: (-t.my_gain, -t.their_gain))
    return _dedupe(ideas)[:limit]


@dataclass
class TradeChain:
    """Two trades where the first makes the second possible.

    The classic shape: buy a piece cheaply from a manager who has it going
    spare (their backup QB, their buried third receiver), which turns one of
    our own starters redundant -- and a redundant starter is a trade chip in a
    way a needed starter never is.
    """

    step1: TradeIdea
    step2: TradeIdea
    total_gain: float  # measured from the ORIGINAL roster, not step-wise sums

    def as_dict(self) -> dict[str, Any]:
        return {
            "step1": self.step1.as_dict(),
            "step2": self.step2.as_dict(),
            "total_gain": round(self.total_gain, 1),
        }


def best_single_gain(
    my_players: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    min_their_gain: float = 3.0,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
) -> float:
    """The bar a chain has to clear: what one plausible trade already gets us."""
    ideas = _step_candidates(
        my_players, opponents, slots,
        min_my_gain=0.0, min_their_gain=min_their_gain,
        untouchable=untouchable, rejected=rejected,
    )
    return max((i.my_gain for i in ideas), default=0.0)


def find_trade_chains(
    my_players: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
    min_step1_my: float = -3.0,
    min_their_gain: float = 3.0,
    min_step2_my: float = 5.0,
    margin: float = 5.0,
    step1_cap: int = 24,
    per_partner_cap: int = 4,
    limit: int = 3,
) -> list[TradeChain]:
    """Two-move sequences that clearly beat the best single trade.

    Every step must stand on its own: the other manager only ever sees their
    own deal, so each needs their_gain >= min_their_gain. Step 1 may be
    near-neutral for us (>= min_step1_my) when it buys the piece that makes
    step 2 possible. A chain is only worth proposing when its total beats the
    best available single trade by `margin` -- two negotiations are twice the
    ways to be told no, and that cost has to buy something.
    """
    mine = [p for p in my_players if p.get("projected") is not None]
    base = lineup_value(mine, slots)
    single_bar = best_single_gain(
        mine, opponents, slots,
        min_their_gain=min_their_gain, untouchable=untouchable, rejected=rejected,
    )

    step1_all = _step_candidates(
        mine, opponents, slots,
        min_my_gain=min_step1_my, min_their_gain=min_their_gain,
        untouchable=untouchable, rejected=rejected,
    )
    step1_all.sort(key=lambda i: -(i.my_gain + i.their_gain))
    per_partner: dict[int, int] = {}
    step1: list[TradeIdea] = []
    for idea in step1_all:
        if per_partner.get(idea.partner_id, 0) >= per_partner_cap:
            continue
        per_partner[idea.partner_id] = per_partner.get(idea.partner_id, 0) + 1
        step1.append(idea)
        if len(step1) >= step1_cap:
            break

    chains: list[TradeChain] = []
    for first in step1:
        mine_after = _swap(mine, first.send, first.receive)
        opponents_after = []
        for opp in opponents:
            if opp["team_id"] == first.partner_id:
                theirs = [p for p in opp.get("players", [])
                          if p.get("projected") is not None]
                opponents_after.append(
                    {**opp, "players": _swap(theirs, first.receive, first.send)}
                )
            else:
                opponents_after.append(opp)

        for second in _step_candidates(
            mine_after, opponents_after, slots,
            min_my_gain=min_step2_my, min_their_gain=min_their_gain,
            untouchable=untouchable, rejected=rejected,
        ):
            final = _swap(mine_after, second.send, second.receive)
            total = lineup_value(final, slots) - base
            if total >= single_bar + margin:
                chains.append(TradeChain(first, second, total))

    chains.sort(key=lambda c: -c.total_gain)
    seen: set[tuple] = set()
    out: list[TradeChain] = []
    for c in chains:
        key = (
            c.step1.partner_id, frozenset(p["player_id"] for p in c.step1.receive),
            c.step2.partner_id, frozenset(p["player_id"] for p in c.step2.receive),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _step_candidates(
    mine: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    min_my_gain: float,
    min_their_gain: float,
    untouchable: Sequence[str],
    rejected: Sequence[tuple[str, str]],
) -> list[TradeIdea]:
    """One side of a chain: 1-for-1 and 2-for-1 offers, both thresholds applied.

    Receives are singles only -- that is what keeps the chain search bounded,
    and a 2-for-2 rarely survives two rounds of negotiation anyway.
    """
    rejected_set = set(rejected)
    protected = set(untouchable)
    base = lineup_value(mine, slots)
    found: list[TradeIdea] = []
    for opp in opponents:
        if opp.get("is_me"):
            continue
        theirs = [p for p in opp.get("players", []) if p.get("projected") is not None]
        if not theirs:
            continue
        their_base = lineup_value(theirs, slots)
        for give in _packages(mine, protected):
            for get in theirs:
                if (opp["name"], get["name"]) in rejected_set:
                    continue
                if not _looks_fair(give, [get]):
                    continue
                my_gain = lineup_value(_swap(mine, give, [get]), slots) - base
                if my_gain < min_my_gain:
                    continue
                their_gain = (
                    lineup_value(_swap(theirs, [get], give), slots) - their_base
                )
                if their_gain < min_their_gain:
                    continue
                found.append(TradeIdea(
                    partner_id=opp["team_id"], partner_name=opp["name"],
                    send=list(give), receive=[get],
                    my_gain=my_gain, their_gain=their_gain,
                ))
    return found


def _packages(mine: Sequence[dict], protected: set[str]) -> list[list[dict]]:
    """What we might send: any one player, or any two."""
    sendable = [p for p in mine if p["player_id"] not in protected]
    singles = [[p] for p in sendable]
    pairs = [
        [a, b]
        for i, a in enumerate(sendable)
        for b in sendable[i + 1:]
    ]
    return singles + pairs


def _dedupe(ideas: Sequence[TradeIdea]) -> list[TradeIdea]:
    """One idea per (partner, player we are trying to get)."""
    seen: set[tuple[int, str]] = set()
    out: list[TradeIdea] = []
    for idea in ideas:
        key = (idea.partner_id, idea.receive[0]["player_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(idea)
    return out
