"""Turn league state into a short, prioritized action queue.

Every action answers the same five questions: what to do, why it is right for
this league and this roster, how confident we are, when it must be done by, and
exactly where to click. When nothing clears the bar, the queue says "no action
needed" -- an empty queue is a finding, not a failure, and manufacturing advice
to fill space is the one thing this module must never do.

Confidence is a word, not a percentage. The projections underneath are ESPN
season estimates; pretending they support two decimal places would be lying
with extra steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ff.engines.trades import TradeIdea, best_lineup, find_trades

# Below this many starting-lineup points, a move is noise, not an action.
ACTION_FLOOR = 3.0

OUT_STATUSES = {"OUT", "INJURY_RESERVE", "IR", "SUSPENSION", "DOUBTFUL"}


@dataclass
class Action:
    kind: str            # trade | waiver | injury | lineup | info
    title: str
    why: str             # plain-English, league-specific reasoning
    confidence: str      # High | Medium | Low
    confidence_why: str
    deadline: str        # honest phrasing; "no hard deadline" is allowed
    steps: str           # the exact clicks in ESPN
    gain: float = 0.0    # starting-lineup points, used for ordering
    urgency: int = 1     # 2 = do today, 1 = this week, 0 = when convenient
    players: list[str] = field(default_factory=list)
    draft_message: str | None = None   # for trades: a message Tyler can send
    trade: dict | None = None          # structured send/receive for the UI

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _confidence_for_gain(gain: float) -> tuple[str, str]:
    if gain >= 30:
        return "High", (
            f"the gap is ~{gain:.0f} starting-lineup points -- far larger than "
            "projection noise"
        )
    if gain >= 10:
        return "Medium", (
            f"worth ~{gain:.0f} points over the season; real, but within a range "
            "where projections can be wrong"
        )
    return "Low", f"only ~{gain:.0f} points -- close to projection noise"


def _acceptance_read(their_gain: float) -> str:
    if their_gain >= 25:
        return "clearly better for them too, so an accept is realistic"
    if their_gain >= 8:
        return "a modest but real gain for them -- worth an ask"
    return "a marginal gain for them, so expect a negotiation"


def _trade_message(idea: TradeIdea) -> str:
    """A short, natural note Tyler can paste. No analytics jargon."""
    give = " and ".join(p["name"] for p in idea.send)
    get = " and ".join(p["name"] for p in idea.receive)
    return (
        f"Hey -- would you do {get} for {give}? "
        f"Looking at your roster I think {give.split(' and ')[0]} actually helps "
        f"your starting lineup, and I have a hole {get.split(' and ')[0]} would fill. "
        "Open to tweaking it if you're close."
    )


def trade_actions(
    me: dict, opponents: Sequence[dict], slots: dict, untouchable: Sequence[str] = ()
) -> list[Action]:
    ideas = find_trades(
        me["players"], opponents, slots, untouchable=untouchable, limit=4
    )
    # Offers that spend the same player are alternatives, not a to-do list.
    seen_senders: set[str] = set()
    out: list[Action] = []
    for idea in ideas:
        alt = any(p["player_id"] in seen_senders for p in idea.send)
        seen_senders.update(p["player_id"] for p in idea.send)

        conf, conf_why = _confidence_for_gain(idea.my_gain)
        give = ", ".join(f'{p["name"]} ({p["position"]})' for p in idea.send)
        get = ", ".join(f'{p["name"]} ({p["position"]})' for p in idea.receive)
        title = f"Offer {give} to {idea.partner_name} for {get}"
        if alt:
            title = f"Alternative: {title.lower()[0] + title[1:]}"
        out.append(
            Action(
                kind="trade",
                title=title,
                why=(
                    f"Your starting lineup improves by ~{idea.my_gain:.0f} points "
                    f"over the season; theirs improves by ~{idea.their_gain:.0f}, "
                    f"which is {_acceptance_read(idea.their_gain)}."
                ),
                confidence=conf,
                confidence_why=conf_why,
                deadline="No hard deadline, but value fades once the season starts "
                         "and everyone sees the same holes.",
                steps=f"ESPN app: League → {idea.partner_name} → Propose Trade "
                      f"→ offer {give} for {get}.",
                gain=idea.my_gain,
                urgency=1,
                players=[p["name"] for p in idea.send + idea.receive],
                draft_message=_trade_message(idea),
                trade=idea.as_dict(),
            )
        )
    return out


def waiver_actions(me: dict, free_agents: Sequence[dict], slots: dict,
                   faab: bool = False) -> list[Action]:
    base = best_lineup(me["players"], slots)
    out: list[Action] = []
    for fa in free_agents:
        if not fa.get("projected"):
            continue
        trial = best_lineup(list(me["players"]) + [fa], slots)
        gain = trial.projected - base.projected
        if gain < ACTION_FLOOR:
            continue

        # The drop is whoever the add pushes furthest from ever starting.
        bench = sorted(trial.bench, key=lambda p: (p.get("projected") or 0))
        drop = next(
            (p for p in bench if p["player_id"] != fa["player_id"]), None
        )
        conf, conf_why = _confidence_for_gain(gain)
        bid = " Bid modestly -- single digits of FAAB." if faab else ""
        out.append(
            Action(
                kind="waiver",
                title=f"Add {fa['name']} ({fa['position']})"
                      + (f", drop {drop['name']}" if drop else ""),
                why=(
                    f"{fa['name']} projects {fa['projected']:.0f} and would start "
                    f"for you, worth ~{gain:.0f} points over your current lineup."
                    f"{bid}"
                ),
                confidence=conf,
                confidence_why=conf_why,
                deadline="Before your league's next waiver run.",
                steps=f"ESPN app: Players → search {fa['name']} → Add"
                      + (f" → drop {drop['name']}." if drop else "."),
                gain=gain,
                urgency=2 if gain >= 15 else 1,
                players=[fa["name"]] + ([drop["name"]] if drop else []),
            )
        )
    out.sort(key=lambda a: -a.gain)
    return out[:3]


def injury_actions(me: dict, slots: dict, news_by_espn_id: dict) -> list[Action]:
    """Starters in real injury trouble -- not the ambient 'questionable' fog."""
    lineup = best_lineup(me["players"], slots)
    out: list[Action] = []
    for s in lineup.starters:
        status = (s.get("injury_status") or "").upper().replace(" ", "_")
        if status not in OUT_STATUSES:
            continue
        story = news_by_espn_id.get(str(s.get("espn_id") or ""))
        cite = f' ESPN: "{story["headline"]}".' if story else ""
        out.append(
            Action(
                kind="injury",
                title=f"Replace {s['name']} in your lineup ({status.title()})",
                why=f"{s['name']} is listed {status.title()} and currently occupies "
                    f"your {s['slot']} slot.{cite}",
                confidence="High",
                confidence_why="injury designations are facts, not projections",
                deadline="Before kickoff of his game.",
                steps="ESPN app: My Team → move him to bench → start the "
                      "best healthy option.",
                gain=s.get("projected") or 0,
                urgency=2,
                players=[s["name"]],
            )
        )
    return out


def build_queue(state: dict, news_items: Sequence[dict],
                untouchable: Sequence[str] = ()) -> list[Action]:
    """The whole point: a short list, ordered by what matters most."""
    me = next((t for t in state["teams"] if t.get("is_me")), None)
    if me is None or not me.get("players"):
        return [Action(
            kind="info",
            title="Nothing to manage yet",
            why="This league hasn't drafted. The queue fills in the moment "
                "rosters exist.",
            confidence="High", confidence_why="there are no rosters to act on",
            deadline="—", steps="—", urgency=0,
        )]

    slots = state["roster_slots"]
    opponents = [t for t in state["teams"] if not t.get("is_me")]
    news_by_id: dict[str, dict] = {}
    for item in news_items:
        for aid in item.get("espn_athlete_ids") or []:
            news_by_id.setdefault(str(aid), item)

    queue: list[Action] = []
    queue += injury_actions(me, slots, news_by_id)
    queue += trade_actions(me, opponents, slots, untouchable)
    queue += waiver_actions(
        me, state.get("free_agents", []), slots,
        faab=(state.get("waiver_type") == "faab"),
    )

    queue.sort(key=lambda a: (-a.urgency, -a.gain))
    if not queue:
        queue.append(Action(
            kind="info",
            title="No action needed",
            why="No trade makes both sides better, nobody on waivers would crack "
                "your lineup, and no starter is in injury trouble. Doing nothing "
                "is the right move today.",
            confidence="High",
            confidence_why="every roster and the full free-agent pool was checked",
            deadline="—", steps="—", urgency=0,
        ))
    return queue
