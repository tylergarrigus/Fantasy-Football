"""The decision engine: given what changed, what (if anything) should Tyler do?

Runs per league, independently. The same NFL event arrives here twice -- once
per league context -- and the two evaluations never see each other. That is what
produces "start him" in one league and "claim him" in the other from a single
piece of news.

The default answer is DO NOTHING, and the code is arranged so that is what
happens unless something clears a real bar. Manufacturing an action to look busy
is the main failure mode of every tool in this space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ff.db.context import LeagueContext
from ff.engines.lineup import LineupEngine
from ff.engines.roster import RosterEngine
from ff.engines.simulate import Simulator
from ff.engines.trade import TradeEngine
from ff.engines.valuation import ValuationEngine
from ff.engines.waiver import Tier, WaiverEngine
from ff.intel.news import NewsEngine, severity_of
from ff.logging_setup import get_logger

log = get_logger(__name__)


class Priority:
    CRITICAL = "CRITICAL"   # act now or lose the opportunity
    HIGH = "HIGH"           # act today
    MEDIUM = "MEDIUM"       # act this week, only if genuinely actionable
    LOW = "LOW"             # log it, do not interrupt


class Urgency:
    NOW = "NOW"
    TODAY = "TODAY"
    THIS_WEEK = "THIS_WEEK"
    MONITOR = "MONITOR"


@dataclass
class Decision:
    league_key: str
    league_name: str
    action: str
    summary: str
    priority: str = Priority.LOW
    urgency: str = Urgency.MONITOR
    subject_player_id: str | None = None
    subject_name: str | None = None
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    championship_before: float | None = None
    championship_after: float | None = None
    confidence: float | None = None
    faab_low: int | None = None
    faab_high: int | None = None
    triggering_event_id: str | None = None
    related_player_ids: list[str] = field(default_factory=list)

    @property
    def championship_delta(self) -> float | None:
        if self.championship_before is None or self.championship_after is None:
            return None
        return round(self.championship_after - self.championship_before, 2)

    @property
    def is_action(self) -> bool:
        return self.action not in ("HOLD", "NO_ACTION")

    def rank_key(self) -> tuple:
        order = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}
        return (order.get(self.priority, 9), -(self.championship_delta or 0))


class DecisionEngine:
    """Turns changed facts into ranked, league-specific actions."""

    def __init__(
        self,
        ctx: LeagueContext,
        *,
        valuation: ValuationEngine,
        roster: RosterEngine,
        lineup: LineupEngine,
        waiver: WaiverEngine,
        trade: TradeEngine,
        simulator: Simulator,
        news: NewsEngine,
        min_delta: float = 1.0,
    ):
        self.ctx = ctx
        self.valuation = valuation
        self.roster = roster
        self.lineup = lineup
        self.waiver = waiver
        self.trade = trade
        self.sim = simulator
        self.news = news
        self.min_delta = min_delta

    # -- entry point -------------------------------------------------------

    def evaluate(
        self, events: list[dict[str, Any]], week: int | None = None, deep: bool = False
    ) -> list[Decision]:
        """Evaluate this league against a set of new NFL events.

        `deep` runs the expensive sweeps (trades, full waiver wire). Those run on
        the scheduled daily pass, not on every 30-minute cycle.
        """
        week = week or self.ctx.current_week()
        if self.ctx.my_team_id() is None:
            log.warning("%s: no team identified; cannot produce recommendations", self.ctx.name)
            return []

        decisions: list[Decision] = []
        self.sim.invalidate()

        for event in events:
            decisions.extend(self._from_event(event, week))

        decisions.extend(self._lineup_decisions(week))

        if deep:
            decisions.extend(self._waiver_decisions(week))
            decisions.extend(self._trade_decisions(week))

        decisions = self._deduplicate(decisions)
        decisions.sort(key=lambda d: d.rank_key())
        return decisions

    # -- event-driven ------------------------------------------------------

    def _from_event(self, event: dict[str, Any], week: int) -> list[Decision]:
        """The core routine. One event, evaluated against THIS league only."""
        player_id = event.get("player_id")
        if not player_id:
            return []

        availability = self.ctx.availability(player_id)
        name = self.valuation.registry.name_of(player_id)

        # Not in this league at all, and not on my roster -- nothing to do here.
        # This is the single biggest source of "do nothing", and correctly so.
        if availability == "unknown":
            return []

        conflicts = self.news.conflicts(player_id)
        if conflicts:
            return [
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="NO_ACTION",
                    subject_player_id=player_id,
                    subject_name=name,
                    summary=f"DATA CONFLICT on {name} -- sources disagree",
                    rationale=(
                        "Reports disagree about his status: "
                        + "; ".join(f"{c['claim']} (via {', '.join(c['sources'])})" for c in conflicts)
                        + ". NO ACTION RECOMMENDED UNTIL VERIFIED."
                    ),
                    priority=Priority.MEDIUM,
                    urgency=Urgency.MONITOR,
                    evidence={"conflicts": conflicts},
                    triggering_event_id=event.get("event_id"),
                )
            ]

        if availability == "mine":
            return self._decisions_for_my_player(event, player_id, name, week)
        if availability == "available":
            return self._decisions_for_available_player(event, player_id, name, week)
        return self._decisions_for_opponent_player(event, player_id, name, week)

    def _decisions_for_my_player(
        self, event: dict, player_id: str, name: str, week: int
    ) -> list[Decision]:
        """I own him. The question is lineup, IR, or drop -- never a claim."""
        value = self.valuation.value(player_id, week)
        status = event.get("new_value") or value.injury_status
        severity = severity_of(status)
        row = self.ctx.one(
            "SELECT slot FROM league_rosters WHERE league_id = :league_id AND player_id = :pid",
            pid=player_id,
        )
        slot = (row["slot"] if row else "BE") or "BE"
        is_starter = slot.upper() not in ("BE", "IR", "BENCH")

        if severity >= 5 and is_starter:
            # Two candidate fixes, and which one wins is exactly what differs
            # between leagues: the best replacement already on my bench, versus
            # the best one sitting on waivers *in this league*. Comparing only
            # the bench is how you end up starting a 2-point scrub while the
            # actual answer is a free agent.
            bench = self._best_bench_replacement(player_id, week)
            waiver = self._best_waiver_replacement(player_id, week)

            bench_pts = (bench or {}).get("projection") or 0.0
            waiver_pts = (waiver or {}).get("projection") or 0.0

            # Only prefer a claim if it beats the bench by enough to be worth a
            # roster move and whatever it costs in FAAB.
            if waiver and waiver_pts - bench_pts >= 2.0:
                return [self._claim_replacement(event, waiver, name, status, slot, bench, week)]
            if bench:
                return [self._start_replacement(event, bench, player_id, name, status, slot, week)]
            if waiver:
                return [self._claim_replacement(event, waiver, name, status, slot, None, week)]

            return [
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="ADD",
                    subject_player_id=player_id,
                    subject_name=name,
                    summary=f"{name} is {status} and you have no replacement",
                    rationale=(
                        f"{name} is {status} in your starting {slot}, and neither your bench "
                        "nor the waiver wire has a legal replacement worth starting. "
                        "DATA UNAVAILABLE on a better option -- check the wire manually."
                    ),
                    priority=Priority.CRITICAL,
                    urgency=Urgency.NOW,
                    evidence={"event": event, "status": status},
                    confidence=0.85,
                    triggering_event_id=event.get("event_id"),
                )
            ]

        if severity >= 3 and is_starter:
            return [
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="MONITOR",
                    subject_player_id=player_id,
                    subject_name=name,
                    summary=f"{name} is {status} -- watch for a downgrade before kickoff",
                    rationale=(
                        f"{name} is {status} and starting at {slot}. Not yet a decision: "
                        "questionable players play far more often than not. Worth re-checking "
                        "when inactives post about 90 minutes before kickoff."
                    ),
                    priority=Priority.MEDIUM,
                    urgency=Urgency.MONITOR,
                    evidence={"event": event, "status": status},
                    confidence=0.7,
                    triggering_event_id=event.get("event_id"),
                )
            ]

        if event.get("kind") == "depth_chart" and event.get("severity") == "major":
            return [
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="HOLD",
                    subject_player_id=player_id,
                    subject_name=name,
                    summary=f"{name}'s role just grew -- hold, do not sell",
                    rationale=(
                        f"{event.get('headline')}. You already own him, so there is nothing to "
                        "buy. The action here is to *not* trade him into the news cycle."
                    ),
                    priority=Priority.MEDIUM,
                    urgency=Urgency.THIS_WEEK,
                    evidence={"event": event},
                    confidence=0.7,
                    triggering_event_id=event.get("event_id"),
                )
            ]
        return []

    def _decisions_for_available_player(
        self, event: dict, player_id: str, name: str, week: int
    ) -> list[Decision]:
        """He is on waivers here. The question is whether to claim, and for how much."""
        targets = {t.player_id: t for t in self.waiver.evaluate(week, limit=120)}
        target = targets.get(player_id)
        if target is None or target.tier in (Tier.IGNORE,):
            return []

        value = self.valuation.value(player_id, week)
        delta = self.sim.championship_delta(week, mean_delta=target.upgrade_over_my_worst or 0)

        priority = {
            Tier.MUST_ADD: Priority.CRITICAL,
            Tier.HIGH_PRIORITY: Priority.HIGH,
            Tier.STASH: Priority.MEDIUM,
            Tier.STREAMER: Priority.MEDIUM,
            Tier.SPECULATIVE: Priority.LOW,
        }.get(target.tier, Priority.LOW)

        action = "CLAIM" if self.ctx.uses_faab else "ADD"
        summary = f"{action.title()} {name}"
        if target.faab_low and target.faab_high:
            summary += f" (${target.faab_low}-${target.faab_high})"

        rationale = target.reasoning
        if target.drop_candidate:
            rationale += f" Drop candidate: {target.drop_candidate}."
        rationale += (
            f" Triggered by: {event.get('headline')}."
            if event.get("headline") else ""
        )

        return [
            Decision(
                league_key=self.ctx.league_key,
                league_name=self.ctx.name,
                action=action,
                subject_player_id=player_id,
                subject_name=name,
                summary=summary,
                rationale=rationale,
                priority=priority,
                urgency=Urgency.NOW if priority == Priority.CRITICAL else Urgency.TODAY,
                evidence={
                    "event": event,
                    "tier": target.tier,
                    "upgrade": target.upgrade_over_my_worst,
                    "pct_owned": target.pct_owned,
                },
                championship_before=delta.get("before"),
                championship_after=delta.get("after"),
                faab_low=target.faab_low,
                faab_high=target.faab_high,
                confidence=0.85 if event.get("verified") else 0.7,
                related_player_ids=[target.drop_candidate_id] if target.drop_candidate_id else [],
                triggering_event_id=event.get("event_id"),
            )
        ]

    def _decisions_for_opponent_player(
        self, event: dict, player_id: str, name: str, week: int
    ) -> list[Decision]:
        """A rival owns him. Almost always nothing to do -- occasionally a buy-low."""
        severity = severity_of(event.get("new_value"))
        if severity < 3:
            return []

        owner_id = self.ctx.rostered_by(player_id)
        owner = self.ctx.one(
            "SELECT name FROM league_teams WHERE league_id = :league_id AND team_id = :tid",
            tid=owner_id,
        )
        value = self.valuation.value(player_id, week)

        # Only worth flagging if he'd actually help us and the injury is short-term.
        if (value.playoff_value or 0) < (value.projection or 0) * 0.8:
            return []

        return [
            Decision(
                league_key=self.ctx.league_key,
                league_name=self.ctx.name,
                action="BUY_LOW",
                subject_player_id=player_id,
                subject_name=name,
                summary=f"Possible buy-low on {name} ({owner['name'] if owner else 'a rival'})",
                rationale=(
                    f"{name} is {event.get('new_value')}, which usually depresses his trade "
                    f"price. His projected playoff value ({value.playoff_value:.1f}) is close to "
                    "his healthy rate, so the discount may be larger than the actual damage. "
                    "Speculative -- only worth pursuing if you have surplus to send."
                ),
                priority=Priority.LOW,
                urgency=Urgency.THIS_WEEK,
                evidence={"event": event, "owner": owner["name"] if owner else None},
                confidence=0.5,
                triggering_event_id=event.get("event_id"),
            )
        ]

    # -- periodic sweeps ---------------------------------------------------

    def _lineup_decisions(self, week: int) -> list[Decision]:
        analysis = self.lineup.analyze(week)
        if analysis.get("status") != "ok":
            return []

        out = []
        for swap in analysis.get("swaps", []):
            delta = self.sim.championship_delta(week, mean_delta=swap.projection_delta)
            out.append(
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="START",
                    subject_player_id=swap.start_player_id,
                    subject_name=swap.start_name,
                    summary=f"Start {swap.start_name} over {swap.bench_name}",
                    rationale=(
                        f"{swap.rationale}. Weekly win probability "
                        f"{swap.win_prob_before:.0f}% -> {swap.win_prob_after:.0f}% "
                        f"({swap.win_prob_delta:+.0f} points). {analysis['strategy']}"
                    ),
                    priority=Priority.HIGH if swap.win_prob_delta >= 5 else Priority.MEDIUM,
                    urgency=Urgency.TODAY,
                    evidence={
                        "win_prob_before": swap.win_prob_before,
                        "win_prob_after": swap.win_prob_after,
                        "opponent": analysis["opponent"],
                    },
                    championship_before=delta.get("before"),
                    championship_after=delta.get("after"),
                    confidence=0.75,
                    related_player_ids=[swap.bench_player_id],
                )
            )
        return out

    def _waiver_decisions(self, week: int) -> list[Decision]:
        out = []
        for target in self.waiver.evaluate(week):
            if not target.actionable:
                continue
            delta = self.sim.championship_delta(week, mean_delta=target.upgrade_over_my_worst or 0)
            if (delta.get("delta") or 0) < self.min_delta:
                continue
            action = "CLAIM" if self.ctx.uses_faab else "ADD"
            summary = f"{action.title()} {target.name}"
            if target.faab_low:
                summary += f" (${target.faab_low}-${target.faab_high})"
            out.append(
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action=action,
                    subject_player_id=target.player_id,
                    subject_name=target.name,
                    summary=summary,
                    rationale=target.reasoning,
                    priority=Priority.HIGH if target.tier == Tier.MUST_ADD else Priority.MEDIUM,
                    urgency=Urgency.TODAY,
                    evidence={"tier": target.tier, "upgrade": target.upgrade_over_my_worst},
                    championship_before=delta.get("before"),
                    championship_after=delta.get("after"),
                    faab_low=target.faab_low,
                    faab_high=target.faab_high,
                    confidence=0.7,
                )
            )
        return out

    def _trade_decisions(self, week: int) -> list[Decision]:
        result = self.trade.discover(week)
        if result.get("status") != "ok":
            return []
        out = []
        for proposal in result.get("proposals", [])[:2]:
            out.append(
                Decision(
                    league_key=self.ctx.league_key,
                    league_name=self.ctx.name,
                    action="TRADE",
                    summary=(
                        f"Offer {', '.join(p['name'] for p in proposal.i_send)} to "
                        f"{proposal.partner_name} for "
                        f"{', '.join(p['name'] for p in proposal.i_receive)}"
                    ),
                    rationale=(
                        f"{proposal.rationale} Acceptance likelihood: {proposal.acceptance} "
                        f"-- {proposal.acceptance_reason}"
                    ),
                    priority=Priority.MEDIUM,
                    urgency=Urgency.THIS_WEEK,
                    evidence={
                        "send": [p["name"] for p in proposal.i_send],
                        "receive": [p["name"] for p in proposal.i_receive],
                        "acceptance": proposal.acceptance,
                        "category": proposal.category,
                    },
                    championship_before=proposal.championship_before,
                    championship_after=proposal.championship_after,
                    confidence=0.6,
                    related_player_ids=[p["player_id"] for p in proposal.i_receive],
                )
            )
        return out

    # -- helpers -----------------------------------------------------------

    def _start_replacement(
        self, event: dict, bench: dict, injured_id: str, injured_name: str,
        status: str | None, slot: str, week: int,
    ) -> Decision:
        injured_value = self.valuation.value(injured_id, week)
        delta = self.sim.championship_delta(
            week, mean_delta=(bench["projection"] or 0) - (injured_value.projection or 0)
        )
        return Decision(
            league_key=self.ctx.league_key,
            league_name=self.ctx.name,
            action="START",
            subject_player_id=bench["player_id"],
            subject_name=bench["name"],
            summary=f"Start {bench['name']} in place of {injured_name}",
            rationale=(
                f"{injured_name} is {status} in your {slot} slot. {bench['name']} is the best "
                f"replacement you already roster ({bench['projection']:.1f} projected). "
                "No waiver move needed -- you already have the answer on your bench."
            ),
            priority=Priority.CRITICAL,
            urgency=Urgency.NOW,
            evidence={"event": event, "status": status, "slot": slot,
                      "bench_option": bench["projection"]},
            championship_before=delta.get("before"),
            championship_after=delta.get("after"),
            confidence=0.9 if event.get("verified") else 0.75,
            related_player_ids=[injured_id],
            triggering_event_id=event.get("event_id"),
        )

    def _claim_replacement(
        self, event: dict, waiver: dict, injured_name: str, status: str | None,
        slot: str, bench: dict[str, Any] | None, week: int,
    ) -> Decision:
        target = waiver["target"]
        delta = self.sim.championship_delta(
            week, mean_delta=(waiver["projection"] or 0) - ((bench or {}).get("projection") or 0)
        )
        action = "CLAIM" if self.ctx.uses_faab else "ADD"
        summary = f"{action.title()} {waiver['name']}"
        if target.faab_low and target.faab_high:
            summary += f" (${target.faab_low}-${target.faab_high})"

        rationale = (
            f"{injured_name} is {status} in your {slot} slot. {waiver['name']} is available "
            f"in this league and projects {waiver['projection']:.1f}"
        )
        if bench:
            rationale += (
                f", against {bench['projection']:.1f} from {bench['name']}, the best you "
                "already roster -- worth the roster move."
            )
        else:
            rationale += " and nothing on your bench can legally fill the slot."
        if target.drop_candidate:
            rationale += f" Drop candidate: {target.drop_candidate}."

        return Decision(
            league_key=self.ctx.league_key,
            league_name=self.ctx.name,
            action=action,
            subject_player_id=waiver["player_id"],
            subject_name=waiver["name"],
            summary=summary,
            rationale=rationale,
            priority=Priority.CRITICAL,
            urgency=Urgency.NOW,
            evidence={
                "event": event, "status": status, "slot": slot,
                "tier": target.tier,
                "bench_option": (bench or {}).get("projection"),
            },
            championship_before=delta.get("before"),
            championship_after=delta.get("after"),
            faab_low=target.faab_low,
            faab_high=target.faab_high,
            confidence=0.88 if event.get("verified") else 0.72,
            related_player_ids=[target.drop_candidate_id] if target.drop_candidate_id else [],
            triggering_event_id=event.get("event_id"),
        )

    def _best_waiver_replacement(self, injured_id: str, week: int) -> dict[str, Any] | None:
        """Best available player who could legally fill the injured player's slot.

        Scoped to this league's free agent pool, which is the whole reason two
        leagues diverge on identical news.
        """
        row = self.ctx.one(
            "SELECT slot FROM league_rosters WHERE league_id = :league_id AND player_id = :pid",
            pid=injured_id,
        )
        slot = (row["slot"] if row else "") or ""
        best = None
        for target in self.waiver.evaluate(week, limit=120):
            if not self.lineup._slot_legal(slot, target.position):
                continue
            if target.tier == Tier.IGNORE or target.projection is None:
                continue
            if best is None or target.projection > best["projection"]:
                best = {
                    "player_id": target.player_id,
                    "name": target.name,
                    "projection": target.projection,
                    "target": target,
                }
        return best

    def _best_bench_replacement(self, injured_id: str, week: int) -> dict[str, Any] | None:
        injured = self.valuation.value(injured_id, week)
        row = self.ctx.one(
            "SELECT slot FROM league_rosters WHERE league_id = :league_id AND player_id = :pid",
            pid=injured_id,
        )
        slot = (row["slot"] if row else "") or ""
        best = None
        for bench in self.ctx.roster(self.ctx.my_team_id()):
            if (bench["slot"] or "").upper() not in ("BE", "BENCH"):
                continue
            if not self.lineup._slot_legal(slot, bench["position"]):
                continue
            value = self.valuation.value(bench["player_id"], week)
            if not value.has_projection:
                continue
            if best is None or (value.projection or 0) > (best["projection"] or 0):
                best = {
                    "player_id": bench["player_id"],
                    "name": value.name,
                    "projection": value.projection,
                }
        return best

    def _deduplicate(self, decisions: list[Decision]) -> list[Decision]:
        """One decision per (action, player). Keep the highest-priority version."""
        best: dict[tuple, Decision] = {}
        for decision in decisions:
            key = (decision.action, decision.subject_player_id)
            existing = best.get(key)
            if existing is None or decision.rank_key() < existing.rank_key():
                best[key] = decision
        return list(best.values())

    def filter_for_notification(self, decisions: list[Decision]) -> list[Decision]:
        """The bar for interrupting someone. Deliberately high.

        Everything else is still recorded -- it just does not buzz a phone.
        """
        out = []
        for decision in decisions:
            if not decision.is_action:
                # DATA CONFLICT is worth surfacing even though it is a non-action.
                if decision.action == "NO_ACTION" and decision.priority == Priority.MEDIUM:
                    out.append(decision)
                continue
            if decision.priority == Priority.CRITICAL:
                out.append(decision)
                continue
            delta = decision.championship_delta
            if delta is not None and delta >= self.min_delta:
                out.append(decision)
            elif decision.priority == Priority.HIGH and delta is None:
                # No simulation available, but a high-priority action -- surface
                # it rather than silently dropping it.
                out.append(decision)
        return out
