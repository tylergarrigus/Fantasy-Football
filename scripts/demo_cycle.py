#!/usr/bin/env python3
"""Simulated monitoring cycle -- no network, no credentials, no ESPN.

Builds two leagues in an in-memory-ish database, injects one NFL event, and runs
the real decision engine and notification formatter over it. What it prints is
exactly what would arrive on the phone.

    python scripts/demo_cycle.py

Its purpose is to make the core behaviour inspectable before any credentials
exist: the same event, two leagues, two different correct answers.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.db.store import Store  # noqa: E402
from ff.identity import PlayerRegistry  # noqa: E402
from ff.intel.news import NFLEvent, NewsEngine  # noqa: E402
from ff.logging_setup import setup_logging  # noqa: E402
from ff.notify.notifier import Notifier  # noqa: E402
from tests.conftest import LeagueBuilder, add_player, build_engine  # noqa: E402


def build_world(store: Store):
    registry = PlayerRegistry(store)
    news = NewsEngine(store, registry)

    roster_plan = [
        ("p_cmc", "Christian McCaffrey", "RB", "RB", 18.0),
        ("p_rb2", "Isaac Guerendo", "RB", "RB", 7.0),
        ("p_qb", "Brock Purdy", "QB", "QB", 19.0),
        ("p_wr1", "Deebo Samuel", "WR", "WR", 14.0),
        ("p_wr2", "Jauan Jennings", "WR", "WR", 11.0),
        ("p_te", "George Kittle", "TE", "TE", 12.0),
        ("p_k", "Jake Moody", "K", "K", 8.0),
        ("p_dst", "49ers DST", "DST", "DST", 7.0),
        ("p_scrub", "Deep Bench Guy", "RB", "BE", 2.0),
    ]
    for pid, name, pos, _slot, _proj in roster_plan:
        add_player(registry, pid, name, pos, "SF")
    add_player(registry, "p_mason", "Jordan Mason", "RB", "SF")
    add_player(registry, "p_fa_rb", "Some Waiver RB", "RB", "NYJ")

    contexts = {}
    for league_id, key, name, mason_free in (
        (1001, "L1", "Sunday Money", False),
        (2002, "L2", "Office League", True),
    ):
        builder = LeagueBuilder(store, league_id, key, name)
        builder.create()
        mine = builder.team("Tyler's Team", mine=True, faab_remaining=76)
        rival = builder.team("The Rival")
        builder.matchup(5, mine, rival)

        for pid, _name, _pos, slot, proj in roster_plan:
            builder.roster(mine, pid, slot, projection=proj)

        if mason_free:
            builder.free_agent("p_mason", projection=13.5, pct_owned=8.0)
        else:
            builder.roster(mine, "p_mason", "BE", projection=13.5)
        builder.free_agent("p_fa_rb", projection=5.0, pct_owned=3.0)

        for idx, (pid, pos, proj) in enumerate(
            [("r_qb", "QB", 17.0), ("r_rb1", "RB", 12.0), ("r_rb2", "RB", 10.0),
             ("r_wr1", "WR", 13.0), ("r_wr2", "WR", 9.0), ("r_te", "TE", 8.0),
             ("r_k", "K", 8.0), ("r_dst", "DST", 6.0)]
        ):
            unique = f"{pid}_{league_id}"
            add_player(registry, unique, f"Rival {pos}{idx}", pos, "DAL")
            builder.roster(rival, unique, pos, projection=proj)

        contexts[key] = builder.ctx

    return registry, news, contexts


def main() -> int:
    setup_logging("WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "demo.db")
        registry, news, contexts = build_world(store)

        print("=" * 72)
        print("SIMULATED MONITORING CYCLE")
        print("=" * 72)
        print("\nSTAGE 1 -- cheap detection (pure Python, no model, $0)")
        print("  Polling Sleeper, ESPN news, depth charts, weather...")
        print("  Detected change: Christian McCaffrey  Questionable -> Out")

        event = NFLEvent(
            kind="injury_status",
            player_id="p_cmc",
            nfl_team="SF",
            headline="Christian McCaffrey ruled OUT for Sunday (knee)",
            old_value="Questionable",
            new_value="Out",
            severity="major",
            source="espn_injury_report",
            verified=True,
        )
        news.record([event])
        payload = dict(event.__dict__, event_id=event.event_id)

        print("\nSTAGE 1.5 -- relevance filter (pure Python, $0)")
        for key, ctx in contexts.items():
            availability = ctx.availability("p_cmc")
            print(f"  {ctx.name:<16} McCaffrey is '{availability}' -> relevant")

        print("\nSTAGE 2 -- analysis, run independently per league")
        all_decisions = []
        for key, ctx in contexts.items():
            engine = build_engine(ctx, registry, news, iterations=2000)
            decisions = engine.evaluate([payload], week=5)
            notifiable = engine.filter_for_notification(decisions)
            all_decisions.extend(notifiable)

            print(f"\n  {ctx.name} ({key})")
            print(f"    Mason availability here : {ctx.availability('p_mason')}")
            print(f"    FAAB remaining          : ${ctx.faab_remaining()}")
            for d in notifiable:
                print(f"    -> [{d.priority}] {d.action}: {d.summary}")

        print("\n" + "=" * 72)
        print("WHAT ARRIVES ON THE PHONE")
        print("=" * 72)

        notifier = Notifier(store, dry_run=True)
        alerts = notifier.build_alerts(all_decisions)
        for alert in alerts:
            print(f"\n[{alert.priority}] {alert.title}")
            print("-" * 72)
            print(alert.body)
            print("-" * 72)

        print(f"\n{len(alerts)} notification(s) from 1 NFL event.")
        print("Note the same news produced START in one league and CLAIM in the other.")
        print(
            "\n(The championship swings look large because these demo leagues have only\n"
            " two teams each -- one lineup change moves a two-team league enormously.\n"
            " In a real 10- or 12-team league these deltas are typically 1-5 points.)"
        )

        print("\n" + "=" * 72)
        print("A QUIET CYCLE (the common case)")
        print("=" * 72)
        ctx = contexts["L1"]
        engine = build_engine(ctx, registry, news, iterations=500)
        quiet = engine.filter_for_notification(engine.evaluate([], week=5))
        critical = [d for d in quiet if d.priority == "CRITICAL"]
        print(f"\n  No new events. Critical alerts generated: {len(critical)}")
        print("  Stage 2 never runs. Cost: $0. Nothing reaches the phone.")

        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
