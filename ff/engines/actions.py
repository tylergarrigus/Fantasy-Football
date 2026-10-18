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

from ff.engines.trades import (
    TradeChain,
    TradeIdea,
    TradePlan,
    _looks_fair,
    best_lineup,
    buy_low_targets,
    find_packages,
    find_trade_chains,
    find_trade_plans,
    find_trades,
    form,
    lineup_value,
    sell_high_ids,
)

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
    tag: str = ""                      # trade flavour: package | buy_low | sell_high | plan
    note: str = ""                     # one line of "why now", for trades

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
            f"worth ~{gain:.0f} points the rest of the way; real, but within a range "
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
    """One casual line for the league group chat -- not a formal pitch.

    The league barely talks; a trade note has to sound like a person, be
    over in a second, and make one true claim. Both sides gaining is the
    claim, because it is the only one the math actually backs.
    """
    give = " + ".join(p["name"] for p in idea.send).lower()
    get = " + ".join(p["name"] for p in idea.receive).lower()
    who = idea.partner_name.strip().split()[0].lower()
    return (f"yo {who} -- {give} for {get}, straight swap? "
            "ran the numbers and your lineup actually gets better too, "
            "not tryna fleece anyone lol")


def trade_actions(
    me: dict, opponents: Sequence[dict], slots: dict,
    untouchable: Sequence[str] = (), min_their_gain: float = 5.0,
) -> list[Action]:
    ideas = find_trades(
        me["players"], opponents, slots, untouchable=untouchable,
        min_their_gain=min_their_gain, limit=4,
    )
    # Offers that spend the same player are alternatives, not a to-do list.
    seen_senders: set[str] = set()
    hot = sell_high_ids(me["players"])
    out: list[Action] = []
    for idea in ideas:
        alt = any(p["player_id"] in seen_senders for p in idea.send)
        seen_senders.update(p["player_id"] for p in idea.send)
        note = next((hot[p["player_id"]] for p in idea.send if p["player_id"] in hot), "")

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
                    f"the rest of the season; theirs improves by ~{idea.their_gain:.0f}, "
                    f"which is {_acceptance_read(idea.their_gain)}."
                ),
                confidence=conf,
                confidence_why=conf_why,
                deadline="No deadline",
                steps=f"ESPN app: League → {idea.partner_name} → Propose Trade "
                      f"→ offer {give} for {get}.",
                gain=idea.my_gain,
                urgency=1,
                players=[p["name"] for p in idea.send + idea.receive],
                draft_message=_trade_message(idea),
                trade=idea.as_dict(),
                tag="sell_high" if note else "",
                note=note,
            )
        )
    return out


def _healthy_player(p: dict) -> bool:
    return (p.get("injury_status") or "").upper().replace(" ", "_") not in UNAVAILABLE


def package_actions(
    me: dict, opponents: Sequence[dict], slots: dict,
    untouchable: Sequence[str] = (), rejected: Sequence[tuple[str, str]] = (),
    min_their_gain: float = 5.0,
) -> list[Action]:
    """Two-or-three-for-one deals that land a star without giving one up."""
    out: list[Action] = []
    for idea in find_packages(
        me["players"], opponents, slots, untouchable=untouchable, rejected=rejected,
        min_their_gain=min_their_gain, limit=4,
    ):
        conf, conf_why = _confidence_for_gain(idea.my_gain)
        give = ", ".join(f'{p["name"]} ({p["position"]})' for p in idea.send)
        get = ", ".join(f'{p["name"]} ({p["position"]})' for p in idea.receive)
        out.append(Action(
            kind="trade",
            title=f"Package {give} to {idea.partner_name} for {get}",
            why=(f"{len(idea.send)} of your players for {len(idea.receive)} of theirs: your "
                 f"starting lineup improves by ~{idea.my_gain:.0f} the rest of the season, "
                 f"theirs by ~{idea.their_gain:.0f}. They get depth, you get the star."),
            confidence=conf, confidence_why=conf_why, deadline="No deadline",
            steps=f"ESPN app: League → {idea.partner_name} → Propose Trade → offer {give} for {get}.",
            gain=idea.my_gain, urgency=1,
            players=[p["name"] for p in idea.send + idea.receive],
            draft_message=_trade_message(idea), trade=idea.as_dict(),
            tag="package", note="Two for one: they fill two spots, you upgrade one.",
        ))
    return out


def buy_low_actions(
    me: dict, opponents: Sequence[dict], slots: dict,
    untouchable: Sequence[str] = (), rejected: Sequence[tuple[str, str]] = (),
) -> list[Action]:
    """Offers for good players whose managers are watching them underperform."""
    protected = set(untouchable)
    rejected_set = set(rejected)
    mine = [p for p in me["players"] if p.get("projected") is not None]
    base = lineup_value(mine, slots)
    out: list[Action] = []
    for target in buy_low_targets(opponents):
        opp = target["_owner"]
        if (opp["name"], target["name"]) in rejected_set:
            continue
        theirs = [p for p in opp["players"] if p.get("projected") is not None]
        their_base = lineup_value(theirs, slots)
        sendable = [p for p in mine if p["player_id"] not in protected and _healthy_player(p)]
        packs = [[p] for p in sendable] + [[a, b] for i, a in enumerate(sendable) for b in sendable[i + 1:]]
        offers: list[TradeIdea] = []
        for give in packs:
            if not _looks_fair(give, [target]):
                continue  # a frustrated manager still wants something real back
            my_gain = lineup_value([p for p in mine if p not in give] + [target], slots) - base
            if my_gain < 5.0:
                continue
            their_gain = lineup_value([p for p in theirs if p is not target] + give, slots) - their_base
            if their_gain < 5.0:
                continue
            offers.append(TradeIdea(opp["team_id"], opp["name"], list(give), [target], my_gain, their_gain))
        if not offers:
            continue
        # The cheapest price that still gets most of the gain -- buying low
        # means not overpaying, not maximising what the other side gets.
        top = max(o.my_gain for o in offers)
        best = min((o for o in offers if o.my_gain >= top - 5),
                   key=lambda o: (sum(p["projected"] for p in o.send), -o.their_gain))
        f = target["_form"]
        conf, conf_why = _confidence_for_gain(best.my_gain)
        give = ", ".join(f'{p["name"]} ({p["position"]})' for p in best.send)
        out.append(Action(
            kind="trade",
            title=f"Buy low: offer {give} to {opp['name']} for {target['name']} ({target['position']})",
            why=(f"{target['name']} is scoring {f['avg']:.1f} a game against a {f['proj_avg']:.1f} "
                 f"projection, so his manager is watching a disappointment. ESPN still projects him "
                 f"for ~{target['projected']:.0f} the rest of the way; you gain ~{best.my_gain:.0f} "
                 f"lineup points, they gain ~{best.their_gain:.0f}."),
            confidence="Medium" if conf == "High" else conf,
            confidence_why=f"{conf_why}; the discount only exists if his manager is losing patience",
            deadline="Before he has a big game",
            steps=f"ESPN app: League → {opp['name']} → Propose Trade → offer {give} for {target['name']}.",
            gain=best.my_gain, urgency=1,
            players=[p["name"] for p in best.send] + [target["name"]],
            draft_message=(f"yo {opp['name'].strip().lower()} -- {' + '.join(p['name'].lower() for p in best.send)} "
                           f"for {target['name'].lower()}? he's been slow but i still like him, "
                           "you get help now, lmk"),
            trade=best.as_dict(),
            tag="buy_low",
            note=f"Scoring {f['avg']:.1f} a game vs {f['proj_avg']:.1f} projected. Buy while he's cold.",
        ))
        if len(out) >= 3:
            break
    return out


def plan_action(
    me: dict,
    opponents: Sequence[dict],
    slots: dict,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
    min_their_gain: float = 5.0,
) -> list[Action]:
    """Two- and three-move plans that clearly beat any single trade."""
    out: list[Action] = []
    for plan in find_trade_plans(
        me["players"], opponents, slots, untouchable=untouchable, rejected=rejected,
        min_their_gain=min_their_gain, limit=2,
    ):
        n = len(plan.steps)
        last = plan.steps[-1]
        get = ", ".join(p["name"] for p in last.receive)
        lines = []
        for i, st in enumerate(plan.steps, 1):
            give = " + ".join(p["name"] for p in st.send)
            lines.append(f"{i}. {give} to {st.partner_name.strip()} for "
                         f"{', '.join(p['name'] for p in st.receive)} (they gain ~{st.their_gain:.0f})")
        conf, conf_why = _confidence_for_gain(plan.total_gain)
        out.append(Action(
            kind="plan",
            title=f"{n}-step plan: land {get}",
            why=(" ".join(lines) + f". All together your lineup gains ~{plan.total_gain:.0f} the rest "
                 "of the season. Each later step only happens if the one before lands."),
            confidence="Medium" if conf == "High" else "Low",
            confidence_why=f"{conf_why}; {n} managers each have to say yes",
            deadline="Step 1 now",
            steps=f"ESPN app: League → {plan.steps[0].partner_name} → Propose Trade → offer "
                  f"{' + '.join(p['name'] for p in plan.steps[0].send)} for "
                  f"{', '.join(p['name'] for p in plan.steps[0].receive)}. When accepted, come back for step 2.",
            gain=plan.total_gain, urgency=1,
            players=[p["name"] for st in plan.steps for p in st.send + st.receive],
            draft_message=_trade_message(plan.steps[0]),
            trade=plan.as_dict(),
            tag="plan",
            note=f"{n} trades, in order. Step 1 first.",
        ))
    return out


def chain_action(
    me: dict,
    opponents: Sequence[dict],
    slots: dict,
    untouchable: Sequence[str] = (),
    rejected: Sequence[tuple[str, str]] = (),
    min_their_gain: float = 3.0,
) -> Action | None:
    """The best two-move plan, when one clearly beats any single trade."""
    chains = find_trade_chains(
        me["players"], opponents, slots,
        untouchable=untouchable, rejected=rejected,
        min_their_gain=min_their_gain, limit=1,
    )
    if not chains:
        return None
    c: TradeChain = chains[0]
    s1, s2 = c.step1, c.step2

    # The raw projections on screen look lopsided; the honest resolution is
    # naming who actually starts. Both notes are computed, not written.
    def _swap_roster(roster, out, into):
        gone = {p["player_id"] for p in out}
        return [p for p in roster if p["player_id"] not in gone] + list(into)

    def bench_note(step):
        o = next((x for x in opponents if x["team_id"] == step.partner_id), None)
        if not o:
            return ""
        theirs = [p for p in o.get("players", []) if p.get("projected") is not None]
        starters = {x["player_id"] for x in best_lineup(theirs, slots).starters}
        riding = [p["name"] for p in step.receive if p["player_id"] not in starters]
        if riding:
            return (f"{' and '.join(riding)} never cracks "
                    f"{o['name']}'s lineup -- scores them nothing.")
        return ""

    mine_now = [p for p in me["players"] if p.get("projected") is not None]
    orig = {x["player_id"]: x["name"]
            for x in best_lineup(mine_now, slots).starters}
    final_roster = _swap_roster(
        _swap_roster(mine_now, s1.send, s1.receive), s2.send, s2.receive
    )
    fin = {x["player_id"]: x["name"]
           for x in best_lineup(final_roster, slots).starters}
    into = [n for pid, n in fin.items() if pid not in orig]
    outof = [n for pid, n in orig.items() if pid not in fin]
    end_note = ""
    if into and outof:
        end_note = (f"You end up starting {', '.join(sorted(into))} "
                    f"instead of {', '.join(sorted(outof))}.")
    step_notes = [bench_note(s1), end_note]
    give1 = ", ".join(f'{p["name"]} ({p["position"]})' for p in s1.send)
    get1 = ", ".join(f'{p["name"]} ({p["position"]})' for p in s1.receive)
    give2 = ", ".join(f'{p["name"]} ({p["position"]})' for p in s2.send)
    get2 = ", ".join(f'{p["name"]} ({p["position"]})' for p in s2.receive)
    conf, conf_why = _confidence_for_gain(c.total_gain)
    return Action(
        kind="plan",
        title=f"Two-step plan: land {get2} by first getting {get1}",
        why=(
            f"Step 1 (do now): offer {give1} to {s1.partner_name} for {get1} -- "
            f"they gain ~{s1.their_gain:.0f}, {_acceptance_read(s1.their_gain)}. "
            f"That makes step 2 possible: offer {give2} to {s2.partner_name} "
            f"for {get2} (they gain ~{s2.their_gain:.0f}). "
            f"Both deals together are worth ~{c.total_gain:.0f} points to you -- "
            "more than any single trade on the board. Step 2 only happens if "
            "step 1 lands, so nothing is risked up front."
        ),
        confidence="Medium" if conf == "High" else conf,
        confidence_why=f"{conf_why}; two separate managers each have to say yes",
        deadline="Step 1 now",
        steps=f"ESPN app: League → {s1.partner_name} → Propose Trade → offer "
              f"{give1} for {get1}. When accepted, come back for step 2.",
        gain=c.total_gain,
        urgency=1,
        players=[p["name"] for p in s1.send + s1.receive + s2.send + s2.receive],
        draft_message=_trade_message(s1),
        trade={**c.as_dict(), "step_notes": step_notes},
    )


def waiver_actions(me: dict, free_agents: Sequence[dict], slots: dict,
                   faab: bool = False) -> list[Action]:
    base = best_lineup(me["players"], slots)
    needed_now = _weekly_starter_ids(me, slots)
    by_id = {p["player_id"]: p for p in me["players"]}
    out: list[Action] = []
    for fa in free_agents:
        if not fa.get("projected"):
            continue
        trial = best_lineup(list(me["players"]) + [fa], slots)
        gain = trial.projected - base.projected
        if gain < ACTION_FLOOR:
            continue

        # The drop is whoever the add pushes furthest from ever starting --
        # but never someone this week's lineup needs, and never the IR slot,
        # which frees no roster spot.
        # A weekly starter the add directly replaces (same spot, at least
        # as good this week) is fair game -- that's a stream, not a hole.
        fa_week = _week_value(fa) or 0
        protected = {
            pid for pid in needed_now
            if not (by_id[pid]["position"] == fa["position"]
                    and fa_week >= (_week_value(by_id[pid]) or 0))
        }
        bench = sorted(trial.bench, key=lambda p: (p.get("projected") or 0))
        drop = next(
            (p for p in bench if p["player_id"] != fa["player_id"]
             and p["player_id"] not in protected
             and (p.get("current_slot") or "BE") != "IR"),
            None,
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
                    f"for you, worth ~{gain:.0f} points the rest of the season."
                    f"{bid}"
                ),
                confidence=conf,
                confidence_why=conf_why,
                deadline="Before the next waiver run",
                steps=f"ESPN app: Players → search {fa['name']} → Add"
                      + (f" → drop {drop['name']}." if drop else "."),
                gain=gain,
                urgency=2 if gain >= 15 else 1,
                players=[fa["name"]] + ([drop["name"]] if drop else []),
            )
        )
    # Three defenses to stream are one decision, not three: keep the best
    # suggestion at each position.
    out.sort(key=lambda a: -a.gain)
    seen_pos: set[str] = set()
    kept: list[Action] = []
    for a in out:
        pos = a.title.split("(")[1].split(")")[0] if "(" in a.title else ""
        if pos in seen_pos:
            continue
        seen_pos.add(pos)
        kept.append(a)
    return kept[:3]


UNAVAILABLE = OUT_STATUSES | {"PUP", "NA"}
FLEX_OK = {"RB", "WR", "TE"}


def _week_value(p: dict) -> float | None:
    """What a player is worth in THIS week's lineup.

    Bye and unavailable players are worth exactly zero -- the thing season
    averages hide. When ESPN gave no weekly number, fall back to a per-game
    share of the season projection rather than inventing one.
    """
    if p.get("on_bye"):
        return 0.0
    status = (p.get("injury_status") or "").upper().replace(" ", "_")
    if status in UNAVAILABLE:
        return 0.0
    if p.get("week_projected") is not None:
        return float(p["week_projected"])
    if p.get("projected") is not None:
        return float(p["projected"]) / 17.0
    return None


def _weekly_starter_ids(me: dict, slots: dict) -> set[str]:
    """Who this week's best lineup can't do without (empty without weekly data)."""
    players = me.get("players", [])
    if not any(p.get("week_projected") is not None or p.get("on_bye")
               for p in players):
        return set()
    weekly = [{**p, "projected": _week_value(p)} for p in players
              if _week_value(p) is not None]
    return {s["player_id"] for s in best_lineup(weekly, slots).starters
            if s["projected"] > 0}


def _why_not(p: dict) -> str:
    if p.get("on_bye"):
        return "on bye"
    status = (p.get("injury_status") or "").upper().replace(" ", "_")
    if status in UNAVAILABLE:
        return status.replace("_", " ").title()
    return f"{(_week_value(p) or 0):.1f} proj"


def lineup_actions(me: dict, slots: dict,
                   free_agents: Sequence[dict] = ()) -> list[Action]:
    """Start/sit for this week only, from weekly projections and byes.

    When the best possible lineup still has a dead slot -- every eligible
    player on bye or out -- no swap fixes it, and the move is a one-week
    pickup. That is the case season averages can never see.
    """
    players = [p for p in me.get("players", []) if _week_value(p) is not None]
    if not players or not any("current_slot" in p for p in players):
        return []
    weekly = [{**p, "season": p.get("projected"), "projected": _week_value(p)}
              for p in players]
    best = best_lineup(weekly, slots)
    best_ids = {s["player_id"] for s in best.starters}
    started = [p for p in weekly
               if (p.get("current_slot") or "BE") not in ("BE", "IR")]
    started_ids = {p["player_id"] for p in started}

    ins = sorted((p for p in weekly if p["player_id"] in best_ids - started_ids),
                 key=lambda p: -p["projected"])
    outs = sorted((p for p in started if p["player_id"] not in best_ids),
                  key=lambda p: p["projected"])
    actions: list[Action] = []
    for put_in, take_out in zip(ins, outs):
        gain = put_in["projected"] - take_out["projected"]
        dead = take_out["projected"] == 0
        if gain < 1.0 and not dead:
            continue
        reason = _why_not(take_out)
        opp = put_in.get("opponent")
        vs = f" vs {opp}" if opp and opp != "BYE" else ""
        actions.append(Action(
            kind="lineup",
            title=f"Start {put_in['name']}, bench {take_out['name']}",
            why=(f"{take_out['name']} is {reason}; {put_in['name']} projects "
                 f"{put_in['projected']:.1f}{vs} this week."),
            confidence="High" if dead or gain >= 4 else "Medium",
            confidence_why=("a bye or injury is a fact, not a projection" if dead
                            else f"about {gain:.1f} points this week"),
            deadline="Before kickoff",
            steps=f"ESPN app: My Team → move {take_out['name']} to bench → "
                  f"start {put_in['name']}.",
            gain=gain,
            urgency=2 if dead else 1,
            players=[put_in["name"], take_out["name"]],
        ))

    named = {n for a in actions for n in a.players}
    for hole in [s for s in best.starters if s["projected"] == 0]:
        slot = hole["slot"]
        eligible = FLEX_OK if slot == "FLEX" else {slot}
        pool = [fa for fa in free_agents
                if fa.get("position") in eligible and not fa.get("on_bye")
                and (fa.get("injury_status") or "").upper().replace(" ", "_")
                not in UNAVAILABLE and fa.get("week_projected")]
        if not pool:
            continue
        pick = max(pool, key=lambda fa: fa["week_projected"])
        # An IR-slot player doesn't hold a bench spot, so dropping him frees
        # nothing; the drop has to come off the active bench.
        drop = min((p for p in weekly if p["player_id"] not in best_ids
                    and p["name"] not in named
                    and (p.get("current_slot") or "BE") != "IR"),
                   # Who to cut is a rest-of-season question: a good player
                   # on bye this week is still a good player next week.
                   key=lambda p: (p.get("season") or 0), default=None)
        drop_txt = f", drop {drop['name']}" if drop else ""
        opp = pick.get("opponent")
        actions.append(Action(
            kind="lineup",
            title=f"Pick up {pick['name']} ({pick['position']}) for this week{drop_txt}",
            why=(f"Your {slot} spot scores zero: {hole['name']} is "
                 f"{_why_not(hole)} and nobody on your bench can fill it. "
                 f"{pick['name']} projects {pick['week_projected']:.1f}"
                 f"{' vs ' + opp if opp else ''}."),
            confidence="High",
            confidence_why="an empty slot is a guaranteed zero",
            deadline="Before kickoff",
            steps=f"ESPN app: Players → {pick['name']} → Add"
                  + (f" → drop {drop['name']}" if drop else "")
                  + f" → start him at {slot}.",
            gain=float(pick["week_projected"]),
            urgency=2,
            players=[pick["name"]] + ([drop["name"]] if drop else []),
        ))
        named.update(a for a in [pick["name"]])
    return actions


def injury_actions(me: dict, slots: dict, news_by_espn_id: dict) -> list[Action]:
    """Starters in real injury trouble -- not the ambient 'questionable' fog.

    "Starter" means who is in the ESPN lineup right now when we know it; a
    hurt player already sitting on the bench needs no action.
    """
    players = me.get("players", [])
    if any("current_slot" in p for p in players):
        starters = [{**p, "slot": p["current_slot"]} for p in players
                    if (p.get("current_slot") or "BE") not in ("BE", "IR")]
    else:
        starters = best_lineup(players, slots).starters
    out: list[Action] = []
    for s in starters:
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
                deadline="Before kickoff",
                steps="ESPN app: My Team → move him to bench → start the "
                      "best healthy option.",
                gain=s.get("projected") or 0,
                urgency=2,
                players=[s["name"]],
            )
        )
    return out


def build_queue(state: dict, news_items: Sequence[dict],
                untouchable: Sequence[str] = (),
                rejected: Sequence[tuple[str, str]] = (),
                trade_posture: str = "normal") -> list[Action]:
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

    # A league that rarely accepts trades gets shown only offers the other
    # manager would find hard to refuse. Their history sets the bar, not ours.
    their_bar = 15.0 if trade_posture == "conservative" else 5.0
    chain_bar = 12.0 if trade_posture == "conservative" else 3.0

    queue: list[Action] = []
    lineup = lineup_actions(me, slots, state.get("free_agents", []))
    queue += lineup
    covered = {n for a in lineup for n in a.players}
    queue += [a for a in injury_actions(me, slots, news_by_id)
              if not set(a.players) & covered]
    trades: list[Action] = []
    trades += buy_low_actions(me, opponents, slots, untouchable, rejected)
    trades += trade_actions(me, opponents, slots, untouchable, their_bar)
    trades += package_actions(me, opponents, slots, untouchable, rejected, their_bar)
    # A bigger package has to buy something: adding a third player to a deal
    # that already worked for both sides is just a giveaway.
    seen_shapes: set[tuple] = set()
    accepted: list[tuple[str, frozenset, frozenset, float, float]] = []
    for a in trades:
        t = a.trade
        send = frozenset(p["name"] for p in t["send"])
        recv = frozenset(p["name"] for p in t["receive"])
        shape = (t["partner_name"], send, recv)
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        if a.tag == "package" and any(
            partner == t["partner_name"] and (s0 <= send or r0 <= recv)
            and t["my_gain"] < mg + 3 and t["their_gain"] < tg + 3
            for partner, s0, r0, mg, tg in accepted
        ):
            continue
        accepted.append((t["partner_name"], send, recv, t["my_gain"], t["their_gain"]))
        queue.append(a)
    queue += plan_action(me, opponents, slots, untouchable, rejected, chain_bar)
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
