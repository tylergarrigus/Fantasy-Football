"""The monitoring cycle.

Three stages, cheapest first, because the entire cost argument for this system
rests on the expensive stage almost never running:

  STAGE 1  -- pure Python, $0. Fetch, hash, diff. If nothing moved, exit. This
              is what happens on the overwhelming majority of cycles.
  STAGE 1.5 -- pure Python, $0. Of the things that did move, do any touch a
              player who is on a roster or available in one of Tyler's leagues?
              An injury to a player neither league can use is not news to us.
  STAGE 2  -- Claude Opus 5. Only reached when something material AND relevant
              changed. Runs per league, independently.

Every cycle is idempotent, because GitHub's scheduler is best-effort and will
happily fire late, twice, or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ff.config import Settings
from ff.db.context import LeagueContext, open_contexts
from ff.db.store import Store, utcnow
from ff.decide.engine import Decision, DecisionEngine
from ff.decide.reasoner import Reasoner
from ff.engines.lineup import LineupEngine
from ff.engines.opponent import OpponentEngine
from ff.engines.roster import RosterEngine
from ff.engines.simulate import Simulator
from ff.engines.trade import TradeEngine
from ff.engines.valuation import ValuationEngine
from ff.engines.waiver import WaiverEngine
from ff.identity import PlayerRegistry
from ff.intel.news import NewsEngine
from ff.logging_setup import get_logger
from ff.notify.format import format_no_action
from ff.notify.notifier import Notifier
from ff.sources.base import HttpClient, SourceAuthError, SourceError
from ff.sources.espn import ESPNFantasySource
from ff.sources.espn_news import ESPNNewsSource
from ff.sources.nflverse import NFLVerseSource
from ff.sources.sleeper import SleeperSource
from ff.sources.weather import WeatherSource

log = get_logger(__name__)


@dataclass
class CycleResult:
    run_id: str
    changes_detected: list[str] = field(default_factory=list)
    relevant_events: int = 0
    decisions: list[Decision] = field(default_factory=list)
    notifications_sent: int = 0
    stage2_invoked: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def did_nothing(self) -> bool:
        return not self.decisions and not self.errors

    def summary(self) -> str:
        if self.errors:
            return f"completed with {len(self.errors)} error(s): {'; '.join(self.errors[:3])}"
        if self.did_nothing:
            return "no material change; no action required"
        return (
            f"{len(self.changes_detected)} change(s), {self.relevant_events} relevant event(s), "
            f"{len(self.decisions)} decision(s), {self.notifications_sent} notification(s)"
        )


class MonitorCycle:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.http = HttpClient(settings.cache_dir)
        self.registry = PlayerRegistry(self.store)
        self.news = NewsEngine(self.store, self.registry)

        self.sleeper = SleeperSource(self.http)
        self.espn_news = ESPNNewsSource(self.http)
        self.espn = ESPNFantasySource(self.store, self.registry, settings.espn)
        self.weather = WeatherSource(self.http, self.store)
        self.usage = NFLVerseSource(self.store, self.registry)
        self.reasoner = Reasoner(settings.anthropic_api_key)
        self.notifier = Notifier(self.store, settings.notify_url, settings.dry_run)

    # -- entry point -------------------------------------------------------

    def run(self, *, deep: bool = False, trigger: str = "schedule") -> CycleResult:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        result = CycleResult(run_id=run_id)
        self.store.execute(
            "INSERT INTO run_log(run_id, started_at, trigger) VALUES(?,?,?)",
            (run_id, utcnow(), trigger),
        )
        self.store.commit()

        try:
            self._run_inner(result, deep=deep)
        except Exception as exc:  # noqa: BLE001 - a cycle must never crash the runner
            log.exception("cycle failed")
            result.errors.append(str(exc))
        finally:
            self.store.execute(
                "UPDATE run_log SET finished_at = ?, changes_found = ?, stage2_invoked = ?, "
                "notifications = ?, error = ? WHERE run_id = ?",
                (
                    utcnow(), len(result.changes_detected), int(result.stage2_invoked),
                    result.notifications_sent,
                    "; ".join(result.errors) or None, run_id,
                ),
            )
            self.store.commit()

        log.info("cycle %s: %s", run_id, result.summary())
        return result

    def _run_inner(self, result: CycleResult, *, deep: bool) -> None:
        # ---------- STAGE 1: cheap detection ----------
        events = self._collect_nfl_intel(result)
        contexts = self._sync_leagues(result)

        if not contexts:
            result.errors.append("no league could be synced")
            return

        league_changed = any(
            self.store.one(
                "SELECT 1 AS hit FROM league_state WHERE league_id = ? AND updated_at >= ?",
                (ctx.league_id, utcnow()[:13]),
            )
            for ctx in contexts.values()
        )

        if not events and not result.changes_detected and not deep:
            log.info(
                "stage 1: %s",
                format_no_action([c.name for c in contexts.values()]),
            )
            return

        # ---------- STAGE 1.5: relevance filter ----------
        relevant = self._filter_relevant(events, contexts)
        result.relevant_events = len(relevant)

        if not relevant and not deep:
            log.info(
                "stage 1.5: %d NFL event(s) but none touch a player in either league; stopping",
                len(events),
            )
            return

        # ---------- STAGE 2: analysis, per league, independently ----------
        all_decisions: list[Decision] = []
        for key, ctx in contexts.items():
            try:
                decisions = self._evaluate_league(ctx, relevant, deep=deep)
                all_decisions.extend(decisions)
            except Exception as exc:  # noqa: BLE001 - one league must not sink the other
                log.exception("evaluation failed for %s", key)
                result.errors.append(f"{key}: {exc}")

        if not all_decisions:
            log.info("analysis complete: no action required in either league")
            return

        result.stage2_invoked = self.reasoner.available
        result.decisions = all_decisions
        self._persist(all_decisions, contexts)

        alerts = self.notifier.build_alerts(all_decisions)
        result.notifications_sent = self.notifier.send_all(alerts)

    # -- stage 1 -----------------------------------------------------------

    def _collect_nfl_intel(self, result: CycleResult) -> list[dict[str, Any]]:
        """Global NFL collection. Runs once, shared by both leagues."""
        events = []

        try:
            players = self.sleeper.players()
            if self.store.global_changed("sleeper_universe_size", len(players)):
                self.registry.ingest_sleeper_universe(players)
        except SourceError as exc:
            log.warning("sleeper universe unavailable: %s", exc)
            result.errors.append(f"sleeper: {exc}")

        try:
            injuries = self.sleeper.injury_snapshot()
            previous = self._load_snapshot("injuries")
            if self.store.global_changed("injuries", injuries):
                result.changes_detected.append("injuries")
                events.extend(self.news.from_injury_diff(previous, injuries))
            self._save_snapshot("injuries", injuries)
        except SourceError as exc:
            log.warning("injury snapshot unavailable: %s", exc)

        try:
            depth = self.sleeper.depth_chart_snapshot()
            previous = self._load_snapshot("depth_charts")
            if self.store.global_changed("depth_charts", depth):
                result.changes_detected.append("depth_charts")
                events.extend(self.news.from_depth_chart_diff(previous, depth))
            self._save_snapshot("depth_charts", depth)
        except SourceError as exc:
            log.warning("depth chart snapshot unavailable: %s", exc)

        articles = self.espn_news.news(limit=40)
        if articles and self.store.global_changed("espn_news", [a["id"] for a in articles]):
            result.changes_detected.append("news")
            events.extend(self.news.from_espn_articles(articles))

        try:
            trending = self.sleeper.trending("add", limit=25)
            if trending and self.store.global_changed("trending", trending[:10]):
                result.changes_detected.append("trending")
                events.extend(self.news.from_trending(trending))
        except SourceError:
            pass

        fresh = self.news.record(events)
        self.news.mark_stale()
        return [dict(e.__dict__, event_id=e.event_id, fingerprint=e.fingerprint) for e in fresh]

    def _sync_leagues(self, result: CycleResult) -> dict[str, LeagueContext]:
        contexts = open_contexts(self.store, self.settings)
        for key, ctx in list(contexts.items()):
            cfg = self.settings.league(key)
            try:
                sync = self.espn.sync(ctx, cfg)
                if sync.changed:
                    result.changes_detected.extend(f"{key}:{c}" for c in sync.changed_keys)
                log.info(
                    "%s: %d teams, %d rostered, %d free agents%s",
                    cfg.name, sync.teams, sync.rostered, sync.free_agents,
                    f" (changed: {', '.join(sync.changed_keys)})" if sync.changed else "",
                )
            except SourceAuthError as exc:
                # A credential problem needs a human. Say so loudly and clearly.
                log.error("%s: %s", cfg.name, exc)
                result.errors.append(f"{key}: {exc}")
                contexts.pop(key, None)
            except SourceError as exc:
                log.error("%s sync failed: %s", cfg.name, exc)
                result.errors.append(f"{key}: {exc}")
                contexts.pop(key, None)
        return contexts

    # -- stage 1.5 ---------------------------------------------------------

    def _filter_relevant(
        self, events: list[dict[str, Any]], contexts: dict[str, LeagueContext]
    ) -> list[dict[str, Any]]:
        """Keep only events touching a player either league can actually use.

        Cheap, and it removes most of the volume. An injury to a third-string
        tight end on a team neither league rosters is real news and completely
        irrelevant to this system.
        """
        relevant = []
        for event in events:
            player_id = event.get("player_id")
            if not player_id:
                continue
            if any(ctx.availability(player_id) != "unknown" for ctx in contexts.values()):
                relevant.append(event)
        return relevant

    # -- stage 2 -----------------------------------------------------------

    def _build_engines(self, ctx: LeagueContext) -> DecisionEngine:
        valuation = ValuationEngine(ctx, self.registry, self.news, self.usage)
        simulator = Simulator(
            ctx, valuation, seed=self.settings.sim_seed, iterations=self.settings.sim_iterations
        )
        roster = RosterEngine(ctx, valuation)
        lineup = LineupEngine(ctx, valuation, simulator)
        waiver = WaiverEngine(ctx, valuation, roster, self.registry)
        opponent = OpponentEngine(ctx, valuation, roster)
        trade = TradeEngine(ctx, valuation, roster, opponent, simulator)
        return DecisionEngine(
            ctx,
            valuation=valuation, roster=roster, lineup=lineup, waiver=waiver,
            trade=trade, simulator=simulator, news=self.news,
            min_delta=self.settings.min_championship_delta,
        )

    def _evaluate_league(
        self, ctx: LeagueContext, events: list[dict[str, Any]], *, deep: bool
    ) -> list[Decision]:
        engine = self._build_engines(ctx)
        decisions = engine.evaluate(events, deep=deep)
        notifiable = engine.filter_for_notification(decisions)

        if notifiable and self.reasoner.available:
            notifiable = self.reasoner.adjudicate(notifiable, self._league_summary(ctx))
        return notifiable

    def _league_summary(self, ctx: LeagueContext) -> dict[str, Any]:
        """Compact league context for the model. Deliberately small -- it does
        not need the full roster to judge whether an alert is worth sending."""
        my_team = ctx.my_team()
        settings = ctx.settings()
        return {
            "league_name": ctx.name,
            "league_key": ctx.league_key,
            "week": ctx.current_week(),
            "my_record": f"{my_team['wins']}-{my_team['losses']}" if my_team else "unknown",
            "standing": my_team["standing"] if my_team else None,
            "waiver_type": settings["waiver_type"] if settings else None,
            "faab_remaining": ctx.faab_remaining(),
            "playoff_teams": settings["playoff_teams"] if settings else None,
            "posture": ctx.posture(),
            "preferences": ctx.preferences(),
        }

    # -- persistence -------------------------------------------------------

    def _persist(self, decisions: list[Decision], contexts: dict[str, LeagueContext]) -> None:
        for decision in decisions:
            ctx = contexts.get(decision.league_key)
            if ctx is None:
                continue
            rec_id = ctx.record_recommendation(
                {
                    "action": decision.action,
                    "urgency": decision.urgency,
                    "priority": decision.priority,
                    "week": ctx.current_week(),
                    "subject_player_id": decision.subject_player_id,
                    "related_player_ids": decision.related_player_ids,
                    "summary": decision.summary,
                    "rationale": decision.rationale,
                    "evidence": decision.evidence,
                    "assumptions": "\n".join(decision.assumptions),
                    "faab_low": decision.faab_low,
                    "faab_high": decision.faab_high,
                    "championship_before": decision.championship_before,
                    "championship_after": decision.championship_after,
                    "championship_delta": decision.championship_delta,
                    "confidence": decision.confidence,
                    "triggering_event_id": decision.triggering_event_id,
                    "model": decision.evidence.get("model", "deterministic"),
                }
            )
            decision.evidence["rec_id"] = rec_id

    # -- snapshot helpers --------------------------------------------------

    def _snapshot_key(self, name: str) -> str:
        return f"snapshot:{name}"

    def _load_snapshot(self, name: str) -> dict:
        import json

        row = self.store.one(
            "SELECT value FROM schema_meta WHERE key = ?", (self._snapshot_key(name),)
        )
        if not row:
            return {}
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return {}

    def _save_snapshot(self, name: str, payload: dict) -> None:
        import json

        self.store.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self._snapshot_key(name), json.dumps(payload, default=str)),
        )
        self.store.commit()

    def close(self) -> None:
        self.http.close()
        self.store.close()
