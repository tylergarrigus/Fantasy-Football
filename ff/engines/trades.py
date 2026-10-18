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
# Name value is draft position, not projection: a backup QB with a big
# projection is still a backup QB to the manager looking at the offer. When
# both sides have ADP, the offer must be worth MARKET_FLOOR of what is asked
# on a standard draft-value curve (value halves roughly every 28 picks).
MARKET_FLOOR = 0.85


def market_value(p: dict) -> float:
    import math
    adp = p.get("adp")
    return 100.0 * math.exp(-(float(adp) if adp else 170.0) / 40.0)


def _looks_fair(send, receive) -> bool:
    asked = sum(p.get("projected") or 0 for p in receive)
    offered = sum(p.get("projected") or 0 for p in send)
    if asked > 0 and offered / asked < FAIRNESS_FLOOR:
        return False
    if all(p.get("adp") for p in list(send) + list(receive)):
        m_ask = sum(market_value(p) for p in receive)
        m_off = sum(market_value(p) for p in send)
        return m_ask <= 0 or m_off / m_ask >= MARKET_FLOOR
    return True


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
    """What we might send: any one healthy player, or any two.

    An injured player is never offered -- the other manager sees the red tag
    before the projection, and the offer reads as a dump.
    """
    sendable = [p for p in mine if p["player_id"] not in protected and _healthy(p)]
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


# ---------------------------------------------------------------------------
# Rest-of-season value, packages, buy-low, and multi-step plans
# ---------------------------------------------------------------------------

LAST_WEEK = 17  # fantasy playoffs run through NFL week 17


def games_left(p: dict, week: int, last_week: int = LAST_WEEK) -> int:
    byes = set(p.get("bye_weeks") or [])
    return sum(1 for w in range(week, last_week + 1) if w not in byes)


def ros_value(p: dict, week: int, last_week: int = LAST_WEEK) -> float | None:
    """Points this player projects for from now through the playoffs.

    ESPN's season projection counts games already played; a trade only buys
    the future. Per-game projection times the games he actually has left --
    a bye still ahead is one fewer game -- is the number both sides are really
    exchanging. Falls back to the season total spread over 12 games when ESPN
    gives no per-game figure.
    """
    avg = p.get("proj_avg")
    if avg is None:
        if p.get("projected") is None:
            return None
        avg = float(p["projected"]) / 12.0
    return round(float(avg) * games_left(p, week, last_week), 1)


def with_ros_values(state: dict, week: int | None = None) -> dict:
    """A copy of the league state where `projected` means rest-of-season.

    `season_proj` keeps ESPN's full-season number for display.
    """
    week = int(week or state.get("current_week") or 1)

    def conv(p: dict) -> dict:
        ros = ros_value(p, week)
        return {**p, "season_proj": p.get("projected"), "projected": ros, "ros": ros}

    out = dict(state)
    out["teams"] = [{**t, "players": [conv(p) for p in t.get("players", [])]} for t in state["teams"]]
    out["free_agents"] = [conv(p) for p in state.get("free_agents", [])]
    return out


def _healthy(p: dict) -> bool:
    return (p.get("injury_status") or "").upper().replace(" ", "_") not in {
        "OUT", "DOUBTFUL", "INJURY_RESERVE", "IR", "PUP", "NA", "SUSPENSION",
    }


def find_packages(
    my_players: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    min_my_gain: float = 10.0,
    min_their_gain: float = 5.0,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
    limit: int = 6,
) -> list[TradeIdea]:
    """Consolidation deals: two or three of ours for one or two of theirs.

    Depth is worth little when only nine players start, and a manager who is
    thin wants bodies. Two decent starters for one great one is how a lineup
    gets a star without giving one up. Search is kept small on purpose: our
    top ten by value in pairs and top eight in threes, against their top eight
    singly and top six in pairs.
    """
    from itertools import combinations

    protected = set(untouchable)
    rejected_set = set(rejected)
    mine = sorted(
        (p for p in my_players if p.get("projected") is not None
         and p["player_id"] not in protected and _healthy(p)),
        key=lambda p: -p["projected"],
    )
    all_mine = [p for p in my_players if p.get("projected") is not None]
    base = lineup_value(all_mine, slots)
    # Two-for-one is already find_trades' territory; this is two-for-two
    # (star plus a piece they need back), three-for-one and three-for-two.
    sends = [list(c) for c in combinations(mine[:10], 2)] + [list(c) for c in combinations(mine[:9], 3)]

    ideas: list[TradeIdea] = []
    for opp in opponents:
        if opp.get("is_me"):
            continue
        theirs = sorted(
            (p for p in opp.get("players", []) if p.get("projected") is not None and _healthy(p)),
            key=lambda p: -p["projected"],
        )
        if not theirs:
            continue
        all_theirs = [p for p in opp.get("players", []) if p.get("projected") is not None]
        their_base = lineup_value(all_theirs, slots)
        gets = [[p] for p in theirs[:8]] + [list(c) for c in combinations(theirs[:6], 2)]
        for get in gets:
            if any((opp["name"], g["name"]) in rejected_set for g in get):
                continue
            for give in sends:
                if len(get) > len(give) or (len(give) == 2 and len(get) != 2):
                    continue  # 2-for-2, 3-for-1, 3-for-2 only
                if not _looks_fair(give, get):
                    continue
                my_gain = lineup_value(_swap(all_mine, give, get), slots) - base
                if my_gain < min_my_gain:
                    continue
                their_gain = lineup_value(_swap(all_theirs, get, give), slots) - their_base
                if their_gain < min_their_gain:
                    continue
                ideas.append(TradeIdea(
                    partner_id=opp["team_id"], partner_name=opp["name"],
                    send=list(give), receive=list(get),
                    my_gain=my_gain, their_gain=their_gain,
                ))

    ideas.sort(key=lambda t: (-t.my_gain, len(t.send), -t.their_gain))
    seen: set[tuple] = set()
    out: list[TradeIdea] = []
    for idea in ideas:
        key = (idea.partner_id, frozenset(p["player_id"] for p in idea.receive))
        if key in seen:
            continue
        seen.add(key)
        out.append(idea)
        if len(out) >= limit:
            break
    return out


def _played(p: dict) -> list[dict]:
    return [r for r in (p.get("history") or []) if r.get("played") and r.get("pts") is not None]


def form(p: dict) -> dict[str, Any]:
    """How a player is really doing: recent and season scoring vs ESPN's projection."""
    rows = _played(p)
    pts = [r["pts"] for r in rows]
    avg = sum(pts) / len(pts) if pts else None
    last3 = sum(pts[-3:]) / len(pts[-3:]) if pts else None
    return {"games": len(pts), "avg": avg, "last3": last3, "proj_avg": p.get("proj_avg")}


def buy_low_targets(opponents: Sequence[dict], *, min_value: float = 90.0, limit: int = 10) -> list[dict]:
    """Good players on other teams scoring well under their projection.

    A manager watching a first-round pick post single digits is the manager who
    takes a fair-looking offer. Three or more games played, actual average at
    least 20% under the projected average, and still projected to matter.
    """
    out = []
    for opp in opponents:
        if opp.get("is_me"):
            continue
        for p in opp.get("players", []):
            if (p.get("projected") or 0) < min_value or not _healthy(p):
                continue
            f = form(p)
            if f["games"] < 3 or not f["proj_avg"] or f["avg"] is None:
                continue
            gap = f["proj_avg"] - f["avg"]
            if gap >= 0.2 * f["proj_avg"] and gap >= 2.0:
                out.append({**p, "_gap": gap, "_form": f, "_owner": opp})
    out.sort(key=lambda p: -(p["_gap"] * (p["projected"] or 0)))
    return out[:limit]


def sell_high_ids(my_players: Sequence[dict]) -> dict[str, str]:
    """Our players scoring well above projection -- the ones to sell into.

    Returns player_id -> short reason.
    """
    out: dict[str, str] = {}
    for p in my_players:
        f = form(p)
        if f["games"] < 3 or not f["proj_avg"] or f["avg"] is None:
            continue
        if f["avg"] >= 1.25 * f["proj_avg"] and f["avg"] - f["proj_avg"] >= 2.5:
            out[p["player_id"]] = (
                f"{p['name']} is scoring {f['avg']:.1f} a game against a {f['proj_avg']:.1f} "
                "projection. Sell while he looks like a star."
            )
    return out


@dataclass
class TradePlan:
    """Two or three trades in order, each one standing alone for its partner."""

    steps: list[TradeIdea]
    total_gain: float  # measured from the original roster

    def as_dict(self) -> dict[str, Any]:
        d = {"steps": [s.as_dict() for s in self.steps], "total_gain": round(self.total_gain, 1)}
        d["step1"] = d["steps"][0]
        d["step2"] = d["steps"][1] if len(d["steps"]) > 1 else d["steps"][0]
        return d


def find_trade_plans(
    my_players: Sequence[dict],
    opponents: Sequence[dict],
    slots: dict[str, Any],
    *,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
    min_their_gain: float = 5.0,
    step_floor: float = -5.0,
    step_adds: float = 5.0,
    margin: float = 8.0,
    beam: int = 24,
    per_partner: int = 3,
    max_steps: int = 3,
    limit: int = 3,
) -> list[TradePlan]:
    """Up-to-three-move plans, found by beam search.

    Every step must stand alone for the manager who sees it (their gain >=
    min_their_gain, fair on paper), may cost us a little if it sets up the
    next move, and each later step must add `step_adds` on its own. A plan is
    reported only when its total beats the best single trade by `margin`:
    every extra negotiation is another chance to hear no.
    """
    from itertools import combinations

    protected = set(untouchable)
    rejected_set = set(rejected)
    mine0 = [p for p in my_players if p.get("projected") is not None]
    base0 = lineup_value(mine0, slots)

    def candidates(mine, teams, used):
        base = lineup_value(mine, slots)
        sendable = [p for p in mine if p["player_id"] not in protected and _healthy(p)]
        packs = [[p] for p in sendable] + [list(c) for c in combinations(sendable, 2)]
        found = []
        for opp in teams:
            if opp.get("is_me") or opp["team_id"] in used:
                continue
            theirs = [p for p in opp.get("players", []) if p.get("projected") is not None]
            if not theirs:
                continue
            tb = lineup_value(theirs, slots)
            for get in theirs:
                if not _healthy(get) or (opp["name"], get["name"]) in rejected_set:
                    continue
                for give in packs:
                    if not _looks_fair(give, [get]):
                        continue
                    mg = lineup_value(_swap(mine, give, [get]), slots) - base
                    if mg < step_floor:
                        continue
                    tg = lineup_value(_swap(theirs, [get], give), slots) - tb
                    if tg < min_their_gain:
                        continue
                    found.append(TradeIdea(opp["team_id"], opp["name"], list(give), [get], mg, tg))
        return found

    def prune(steps):
        steps.sort(key=lambda s: -(s.my_gain + 0.3 * s.their_gain))
        per: dict[int, int] = {}
        keep = []
        for s in steps:
            if per.get(s.partner_id, 0) >= per_partner:
                continue
            per[s.partner_id] = per.get(s.partner_id, 0) + 1
            keep.append(s)
            if len(keep) >= beam:
                break
        return keep

    def apply(mine, teams, step):
        new_mine = _swap(mine, step.send, step.receive)
        new_teams = []
        for t in teams:
            if t["team_id"] == step.partner_id:
                theirs = [p for p in t.get("players", []) if p.get("projected") is not None]
                new_teams.append({**t, "players": _swap(theirs, step.receive, step.send)})
            else:
                new_teams.append(t)
        return new_mine, new_teams

    first = candidates(mine0, list(opponents), set())
    best_single = max((s.my_gain for s in first), default=0.0)
    frontier = []
    for s in prune(first):
        m1, t1 = apply(mine0, list(opponents), s)
        frontier.append((m1, t1, [s], {s.partner_id}))

    plans: list[TradePlan] = []
    for _depth in range(2, max_steps + 1):
        nxt = []
        for m, t, path, used in frontier:
            prev_total = lineup_value(m, slots) - base0
            for s in prune(candidates(m, t, used)):
                m2, t2 = apply(m, t, s)
                total = lineup_value(m2, slots) - base0
                if total < prev_total + step_adds:
                    continue
                newpath = path + [s]
                plans.append(TradePlan(newpath, total))
                nxt.append((m2, t2, newpath, used | {s.partner_id}))
        nxt.sort(key=lambda x: -(lineup_value(x[0], slots) - base0))
        frontier = nxt[:beam]

    plans = [p for p in plans if p.total_gain >= best_single + margin]
    plans.sort(key=lambda p: (-p.total_gain, len(p.steps)))
    seen: set[tuple] = set()
    out: list[TradePlan] = []
    for plan in plans:
        sig = frozenset((s.partner_id, s.receive[0]["player_id"]) for s in plan.steps)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(plan)
        if len(out) >= limit:
            break
    return out
