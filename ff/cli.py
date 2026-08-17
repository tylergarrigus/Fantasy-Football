"""CLI. Not a dashboard -- a way to ask the agent a direct question.

The agent's primary interface is the notification. This exists for the times you
want to interrogate it: why did you send that, what are you watching, what would
you do right now.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ff.config import ConfigError, load_settings
from ff.cycle import MonitorCycle
from ff.db.context import open_contexts
from ff.db.store import Store
from ff.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


def _load(env_file: str | None = None):
    if env_file:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)
    else:
        try:
            from dotenv import load_dotenv

            load_dotenv(override=False)
        except ImportError:
            pass
    return load_settings()


def _print_header(text: str) -> None:
    print(f"\n{text}\n{'=' * len(text)}")


def _fmt_pct(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else "n/a"


# -- commands --------------------------------------------------------------


def cmd_doctor(args) -> int:
    """Check configuration without touching the network."""
    try:
        settings = _load(args.env)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1

    _print_header("Configuration")
    print(f"Leagues configured : {len(settings.leagues)}")
    for lg in settings.leagues:
        team = lg.team_id if lg.team_id else "auto-detect from SWID"
        print(f"  {lg.key}: {lg.name} (id {lg.league_id}, season {lg.season}, team {team})")
    print(f"ESPN cookies       : {'set' if settings.espn.configured else 'not set (public leagues only)'}")
    print(f"Stage-2 reasoning  : {'enabled' if settings.can_reason else 'DISABLED (no ANTHROPIC_API_KEY)'}")
    print(f"Notifications      : {settings.notify_url.split('://')[0] + '://…' if settings.can_notify else 'NOT CONFIGURED'}")
    print(f"Database           : {settings.db_path}")
    print(f"Alert threshold    : {settings.min_championship_delta}% championship delta")
    print(f"Dry run            : {settings.dry_run}")

    if settings.warnings():
        _print_header("Warnings")
        for warning in settings.warnings():
            print(f"  ! {warning}")

    if not settings.leagues:
        print("\nNothing to do until FF_LEAGUE_1_ID / FF_LEAGUE_2_ID are set.")
        return 1
    return 0


def cmd_run(args) -> int:
    """Run one monitoring cycle."""
    settings = _load(args.env)
    cycle = MonitorCycle(settings)
    try:
        result = cycle.run(deep=args.deep, trigger=args.trigger)
    finally:
        cycle.close()

    _print_header("Cycle result")
    print(result.summary())
    if result.decisions:
        for decision in result.decisions:
            delta = decision.championship_delta
            impact = f" ({delta:+.1f}% title odds)" if delta is not None else ""
            print(f"  [{decision.priority}] {decision.league_name}: {decision.summary}{impact}")
    for error in result.errors:
        print(f"  ERROR: {error}")
    return 1 if result.errors else 0


def cmd_status(args) -> int:
    """What should I do right now?"""
    settings = _load(args.env)
    store = Store(settings.db_path)
    contexts = open_contexts(store, settings)

    if not contexts:
        print("No leagues configured.")
        return 1

    for key, ctx in contexts.items():
        if args.league and key != args.league:
            continue
        _print_header(f"{ctx.name} ({key})")
        team = ctx.my_team()
        if not team:
            print("  No team identified yet. Run `ff run` to sync, or set FF_LEAGUE_n_TEAM_ID.")
            continue
        print(f"  Record   : {team['wins']}-{team['losses']}  (standing {team['standing']})")
        faab = ctx.faab_remaining()
        if faab is not None:
            print(f"  FAAB     : ${faab} remaining")
        print(f"  Posture  : {ctx.posture()}")

        recs = ctx.recent_recommendations(limit=5)
        if not recs:
            print("\n  No outstanding recommendations. Nothing to do.")
            continue
        print("\n  Latest recommendations:")
        for rec in recs:
            delta = rec["championship_delta"]
            impact = f" ({delta:+.1f}%)" if delta is not None else ""
            print(f"    [{rec['priority']}] {rec['summary']}{impact}")
            if rec["rationale"]:
                print(f"      {rec['rationale']}")
    store.close()
    return 0


def cmd_odds(args) -> int:
    """Championship probability per league."""
    settings = _load(args.env)
    store = Store(settings.db_path)
    cycle = MonitorCycle(settings, store=store)
    contexts = open_contexts(store, settings)

    for key, ctx in contexts.items():
        if args.league and key != args.league:
            continue
        _print_header(f"{ctx.name} — season outlook")
        engine = cycle._build_engines(ctx)
        odds = engine.sim.season()
        if not odds:
            print("  DATA UNAVAILABLE — not enough league data to simulate.")
            continue
        mine = ctx.my_team_id()
        for team_id, entry in sorted(
            odds.items(), key=lambda kv: kv[1].championship_pct, reverse=True
        ):
            marker = " <- you" if team_id == mine else ""
            print(
                f"  {entry.name:<24} playoffs {_fmt_pct(entry.playoff_pct):>7}   "
                f"title {_fmt_pct(entry.championship_pct):>6}   "
                f"proj wins {entry.projected_wins:>4}{marker}"
            )
        print(f"\n  Model estimate over {settings.sim_iterations:,} simulated seasons.")
        for caveat in (odds[mine].caveats if mine in odds else []):
            print(f"  Note: {caveat}")
    cycle.close()
    return 0


def cmd_lineup(args) -> int:
    settings = _load(args.env)
    store = Store(settings.db_path)
    cycle = MonitorCycle(settings, store=store)
    for key, ctx in open_contexts(store, settings).items():
        if args.league and key != args.league:
            continue
        engine = cycle._build_engines(ctx)
        analysis = engine.lineup.analyze()
        _print_header(f"{ctx.name} — week {analysis.get('week', '?')} lineup")
        if analysis.get("status") != "ok":
            print(f"  {analysis.get('status')}: {analysis.get('reason')}")
            continue
        print(f"  vs {analysis['opponent']}")
        print(f"  You  {analysis['current']['projection']:.1f}  "
              f"(floor {analysis['current']['floor']:.1f}, ceiling {analysis['current']['ceiling']:.1f})")
        print(f"  Them {analysis['opponent_projection']:.1f}")
        print(f"  Win probability: {analysis['current']['win_probability']:.0f}%")
        print(f"\n  {analysis['strategy']}")
        if analysis["swaps"]:
            print("\n  Recommended changes:")
            for swap in analysis["swaps"]:
                print(f"    START {swap.start_name} over {swap.bench_name} "
                      f"({swap.win_prob_delta:+.0f}% win probability)")
                print(f"      {swap.rationale}")
        else:
            print("\n  Lineup is optimal. No changes needed.")
    cycle.close()
    return 0


def cmd_waivers(args) -> int:
    settings = _load(args.env)
    store = Store(settings.db_path)
    cycle = MonitorCycle(settings, store=store)
    for key, ctx in open_contexts(store, settings).items():
        if args.league and key != args.league:
            continue
        engine = cycle._build_engines(ctx)
        targets = engine.waiver.evaluate()
        _print_header(f"{ctx.name} — waiver targets")
        if not targets:
            print("  Nothing on waivers improves this roster. Doing nothing is correct.")
            continue
        for target in targets[: args.limit]:
            line = f"  [{target.tier}] {target.name} ({target.position})"
            if target.faab_low:
                line += f"  ${target.faab_low}-${target.faab_high}"
            print(line)
            print(f"      {target.reasoning}")
    cycle.close()
    return 0


def cmd_trades(args) -> int:
    settings = _load(args.env)
    store = Store(settings.db_path)
    cycle = MonitorCycle(settings, store=store)
    for key, ctx in open_contexts(store, settings).items():
        if args.league and key != args.league:
            continue
        engine = cycle._build_engines(ctx)
        result = engine.trade.discover()
        _print_header(f"{ctx.name} — trade opportunities")
        if result.get("status") != "ok":
            print(f"  {result.get('status')}: {result.get('reason')}")
            continue
        if result.get("note"):
            print(f"  {result['note']}")
        print(f"  Your needs: {', '.join(result.get('my_needs') or []) or 'none'}")
        print(f"  Your surplus: {', '.join(result.get('my_surplus') or []) or 'none'}")
        for proposal in result.get("proposals", []):
            print(f"\n  {proposal.describe()}")
            print(f"      {proposal.rationale}")
            print(f"      Acceptance: {proposal.acceptance} — {proposal.acceptance_reason}")
    cycle.close()
    return 0


def cmd_watching(args) -> int:
    """Everything the agent currently has eyes on."""
    settings = _load(args.env)
    store = Store(settings.db_path)
    contexts = open_contexts(store, settings)

    _print_header("Global NFL intelligence")
    counts = store.query(
        "SELECT kind, COUNT(*) AS n FROM nfl_events WHERE stale = 0 GROUP BY kind ORDER BY n DESC"
    )
    if counts:
        for row in counts:
            print(f"  {row['kind']:<16} {row['n']} active event(s)")
    else:
        print("  No events recorded yet. Run `ff run`.")

    players = store.one("SELECT COUNT(*) AS n FROM nfl_players")
    print(f"\n  Player universe: {players['n'] if players else 0} players")

    for key, ctx in contexts.items():
        _print_header(f"{ctx.name} ({key})")
        teams = ctx.teams()
        roster = ctx.my_roster()
        fa = ctx.query(
            "SELECT COUNT(*) AS n FROM league_free_agents WHERE league_id = :league_id"
        )
        print(f"  Teams tracked      : {len(teams)}")
        print(f"  My roster          : {len(roster)} players")
        print(f"  Free agents watched: {fa[0]['n'] if fa else 0}")
        print(f"  Waiver type        : {'FAAB' if ctx.uses_faab else 'priority'}")
        prefs = ctx.preferences()
        if prefs:
            print("  Your preferences   :")
            for pref_key, values in prefs.items():
                print(f"    {pref_key}: {', '.join(values)}")
    store.close()
    return 0


def cmd_why(args) -> int:
    """Why did you send me that alert?"""
    settings = _load(args.env)
    store = Store(settings.db_path)
    from ff.notify.notifier import Notifier

    notifier = Notifier(store)

    if not args.fingerprint:
        _print_header("Recent notifications")
        history = notifier.history(limit=15)
        if not history:
            print("  Nothing sent yet.")
        for row in history:
            status = "delivered" if row["delivered"] else f"FAILED ({row['error']})"
            print(f"  {row['fingerprint'][:8]}  {row['sent_at']}  [{row['priority']}]  {status}")
            print(f"      {row['title']}")
        print("\n  Run `ff why <first 8 chars>` for the full reasoning.")
        store.close()
        return 0

    detail = notifier.explain(args.fingerprint)
    if not detail:
        print(f"No notification found matching {args.fingerprint!r}")
        store.close()
        return 1

    _print_header(detail["title"])
    print(detail["body"])
    for rec in detail.get("recommendations", []):
        _print_header("Underlying recommendation")
        print(f"  Action     : {rec['action']}")
        print(f"  Confidence : {rec['confidence']}")
        print(f"  Evidence   : {rec['evidence_json']}")
        if rec["assumptions"]:
            print(f"  Assumptions:\n    " + rec["assumptions"].replace("\n", "\n    "))
    store.close()
    return 0


def cmd_prefer(args) -> int:
    """Record a standing preference: posture, never_trade, avoid_team, min_priority."""
    settings = _load(args.env)
    store = Store(settings.db_path)

    league_id = None
    if args.league:
        league_id = settings.league(args.league).league_id

    if args.clear:
        removed = store.clear_preferences(args.key, league_id)
        print(f"Cleared {removed} preference(s) for {args.key!r}.")
        store.close()
        return 0

    if not args.value:
        prefs = store.preferences(league_id)
        _print_header("Preferences")
        for row in prefs:
            scope = "both leagues" if row["league_id"] is None else f"league {row['league_id']}"
            print(f"  {row['key']} = {row['value']}  ({scope})")
        store.close()
        return 0

    store.set_preference(args.key, args.value, league_id)
    scope = "both leagues" if league_id is None else args.league
    print(f"Noted: {args.key} = {args.value} ({scope}).")
    store.close()
    return 0


def cmd_changed(args) -> int:
    """What changed recently?"""
    settings = _load(args.env)
    store = Store(settings.db_path)
    from ff.identity import PlayerRegistry
    from ff.intel.news import NewsEngine

    news = NewsEngine(store, PlayerRegistry(store))
    events = news.recent(hours=args.hours)

    _print_header(f"NFL changes in the last {args.hours}h")
    if not events:
        print("  Nothing. That is the normal state.")
        store.close()
        return 0

    for event in events[: args.limit]:
        flags = []
        if event["verified"]:
            flags.append("verified")
        if event["stale"]:
            flags.append("STALE")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  [{event['severity']:<7}] {event['headline']}{suffix}")
        print(f"      source: {event['source']} (tier {event['source_tier']}), "
              f"seen {event['first_seen_at']}")
    store.close()
    return 0


# -- wiring ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ff",
        description="Autonomous fantasy football GM for two ESPN leagues.",
    )
    parser.add_argument("--env", help="path to a .env file")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        return sp

    add("doctor", cmd_doctor, "check configuration without touching the network")

    p = add("run", cmd_run, "run one monitoring cycle")
    p.add_argument("--deep", action="store_true", help="also sweep trades and the full waiver wire")
    p.add_argument("--trigger", default="manual")

    for name, func, help_ in (
        ("status", cmd_status, "what should I do right now?"),
        ("odds", cmd_odds, "championship probability per league"),
        ("lineup", cmd_lineup, "start/sit analysis for this week"),
        ("watching", cmd_watching, "everything the agent is monitoring"),
    ):
        p = add(name, func, help_)
        p.add_argument("--league", choices=["L1", "L2"], help="limit to one league")

    p = add("waivers", cmd_waivers, "ranked waiver targets")
    p.add_argument("--league", choices=["L1", "L2"])
    p.add_argument("--limit", type=int, default=10)

    p = add("trades", cmd_trades, "find trades worth proposing")
    p.add_argument("--league", choices=["L1", "L2"])

    p = add("why", cmd_why, "why did you send me that alert?")
    p.add_argument("fingerprint", nargs="?", help="first 8 chars from `ff why` with no argument")

    p = add("prefer", cmd_prefer, "record a standing preference")
    p.add_argument("key", help="posture | never_trade | avoid_team | min_priority | faab_appetite")
    p.add_argument("value", nargs="?")
    p.add_argument("--league", choices=["L1", "L2"], help="scope to one league (default: both)")
    p.add_argument("--clear", action="store_true")

    p = add("changed", cmd_changed, "what changed in the NFL recently")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--limit", type=int, default=25)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
