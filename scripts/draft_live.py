#!/usr/bin/env python3
"""Watch a live ESPN draft and print a recommendation after every pick.

Runs as a long-lived GitHub Actions job (the only place with network access to
ESPN). Everything it prints goes to the job log, which can be read from
anywhere while the draft is happening -- so the question "who should I take"
can be answered without anyone typing out who has already gone.

Polls `mDraftDetail`, which updates during a live draft. Prints only when the
pick count actually changes, so the log stays readable.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.config import ESPNCredentials, LeagueConfig  # noqa: E402
from ff.db.context import LeagueContext  # noqa: E402
from ff.db.store import Store  # noqa: E402
from ff.engines.draft import STREAMABLE, DraftEngine  # noqa: E402
from ff.identity import PlayerRegistry  # noqa: E402
from ff.logging_setup import get_logger, setup_logging  # noqa: E402
from ff.sources.base import HttpClient  # noqa: E402
from ff.sources.espn import ESPNFantasySource  # noqa: E402
from ff.sources.espn_draft import ESPNDraftSource  # noqa: E402
from ff.sources.sleeper import SleeperSource  # noqa: E402

log = get_logger(__name__)


def stamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%H:%M:%SZ")


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}", flush=True)


def report(engine: DraftEngine, ctx: LeagueContext, registry: PlayerRegistry) -> None:
    """The block that gets read mid-draft. Dense on purpose."""
    made = engine.picks_made()
    teams = len(ctx.teams()) or 12
    rnd = (made // teams) + 1

    banner(f"[{stamp()}]  {made} PICKS MADE  ·  round {rnd}  ·  next is overall #{made + 1}")

    # Who just went, most recent first.
    recent = ctx.query(
        "SELECT d.overall_pick, d.team_id, d.player_id, p.full_name, p.position, t.name AS team "
        "FROM draft_picks d "
        "LEFT JOIN nfl_players p ON p.player_id = d.player_id "
        "LEFT JOIN league_teams t ON t.league_id = d.league_id AND t.team_id = d.team_id "
        "WHERE d.league_id = :league_id "
        "ORDER BY d.overall_pick DESC LIMIT 8"
    )
    if recent:
        print("\nLAST 8 PICKS")
        for r in reversed(recent):
            print(
                f"  #{r['overall_pick']:<3} {(r['full_name'] or '?'):<24} "
                f"{(r['position'] or '?'):<4} -> {r['team'] or '?'}"
            )

    my_id = ctx.my_team_id()
    mine = ctx.query(
        "SELECT p.full_name, p.position FROM draft_picks d "
        "JOIN nfl_players p ON p.player_id = d.player_id "
        "WHERE d.league_id = :league_id AND d.team_id = :tid ORDER BY d.overall_pick",
        tid=my_id,
    )
    print(f"\nMY ROSTER ({len(mine)})")
    if mine:
        for r in mine:
            print(f"  {r['position']:<4} {r['full_name']}")
    else:
        print("  (empty)")

    advice = engine.advise()
    if advice.recommendation is None:
        print("\n  " + "; ".join(advice.caveats or ["no recommendation available"]))
        return

    print(f"\nSTILL NEEDED: {', '.join(advice.roster_needs) or 'starters filled'}")
    if advice.picks_until_next:
        print(f"NEXT PICK: overall #{advice.next_pick} ({advice.picks_until_next} away)")

    print(f"\n>>> TAKE: {advice.recommendation.name} "
          f"({advice.recommendation.position}, proj {advice.recommendation.projected:.0f}, "
          f"VOR {advice.recommendation.vor:+.0f})")
    print(f"    {advice.reasoning}")

    if advice.alternatives:
        print("\nNEXT BEST")
        for alt in advice.alternatives:
            adp = f"ADP {alt.adp:.0f}" if alt.adp else "ADP n/a"
            print(f"  {alt.name:<24} {alt.position:<4} proj {alt.projected:>5.0f} "
                  f"VOR {alt.vor:>+6.0f}  {adp}")

    if advice.tier_warnings:
        print("\nCLIFFS BEFORE YOUR NEXT PICK")
        for w in advice.tier_warnings:
            print(f"  ! {w}")

    # Best available by position -- the question that always comes up next.
    available = engine.available()
    print("\nBEST AVAILABLE BY POSITION")
    for pos in ("QB", "RB", "WR", "TE"):
        pool = [p for p in available if p.position == pos][:4]
        if pool:
            names = ", ".join(f"{p.name} ({p.projected:.0f})" for p in pool)
            print(f"  {pos:<3} {names}")
    print(flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--minutes", type=int, default=240)
    parser.add_argument("--interval", type=int, default=20)
    args = parser.parse_args()

    setup_logging("WARNING")

    work = Path("live_state")
    work.mkdir(exist_ok=True)
    store = Store(work / "live.db")
    http = HttpClient(work / ".cache")
    registry = PlayerRegistry(store)
    creds = ESPNCredentials(
        espn_s2=os.environ.get("ESPN_S2", ""), swid=os.environ.get("ESPN_SWID", "")
    )

    banner(f"LIVE DRAFT WATCH  ·  league {args.league_id}  ·  started {stamp()}")

    try:
        registry.ingest_sleeper_universe(SleeperSource(http).players())
    except Exception as exc:  # noqa: BLE001
        print(f"  (sleeper universe unavailable: {exc})", flush=True)

    cfg = LeagueConfig(key="L1", league_id=args.league_id, season=args.season, name="League")
    ctx = LeagueContext(store, args.league_id, "L1", "League")

    ESPNFantasySource(store, registry, creds).sync(ctx, cfg)
    source = ESPNDraftSource(http, store, registry, creds)

    scoring = "ppr"
    stored = source.sync_rankings(ctx, args.season, scoring)
    print(f"  Board loaded: {stored} ranked players. Watching for picks...", flush=True)

    engine = DraftEngine(ctx)
    deadline = time.time() + args.minutes * 60
    last_count = -1
    idle_notices = 0

    while time.time() < deadline:
        try:
            source.sync_picks(ctx, args.season)
            engine._board = None  # picks changed; recompute the board
            made = engine.picks_made()

            if made != last_count:
                last_count = made
                idle_notices = 0
                report(engine, ctx, registry)
            else:
                idle_notices += 1
                # Heartbeat every ~5 minutes so the log shows it is alive.
                if idle_notices % max(1, (300 // args.interval)) == 0:
                    print(f"[{stamp()}] waiting... {made} picks so far", flush=True)
        except Exception as exc:  # noqa: BLE001 - a blip must not end the watch
            print(f"[{stamp()}] poll error (continuing): {exc}", flush=True)

        time.sleep(args.interval)

    banner(f"WATCH ENDED {stamp()}  ·  {last_count} picks recorded")
    http.close()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
