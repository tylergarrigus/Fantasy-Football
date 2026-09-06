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
