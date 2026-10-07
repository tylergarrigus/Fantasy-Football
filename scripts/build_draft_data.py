#!/usr/bin/env python3
"""Pull real draft rankings for each league and write a board to disk.

Runs on the GitHub Actions runner (the only place that can reach ESPN) and
commits its output as JSON, so the analysis can happen anywhere afterwards.

Everything it writes is derived from ESPN's own projections under each league's
own scoring settings -- nothing here is invented. Players ESPN gave us no
projection for are omitted rather than guessed at.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.config import ESPNCredentials, LeagueConfig  # noqa: E402
from ff.db.context import LeagueContext  # noqa: E402
from ff.db.store import Store  # noqa: E402
from ff.engines.draft import DraftEngine  # noqa: E402
from ff.identity import PlayerRegistry  # noqa: E402
from ff.logging_setup import get_logger, setup_logging  # noqa: E402
from ff.sources.base import HttpClient  # noqa: E402
from ff.sources.espn import ESPNFantasySource  # noqa: E402
from ff.sources.espn_draft import ESPNDraftSource  # noqa: E402
from ff.sources.sleeper import SleeperSource  # noqa: E402

log = get_logger(__name__)


def build(league_id: int, key: str, name: str, season: int, out_dir: Path) -> dict:
    store = Store(out_dir / "draft_state.db")
    http = HttpClient(out_dir / ".cache")
    registry = PlayerRegistry(store)
    creds = ESPNCredentials(
        espn_s2=os.environ.get("ESPN_S2", ""), swid=os.environ.get("ESPN_SWID", "")
    )

    # Sleeper first -- it supplies the identity crosswalk that lets ESPN ids
    # resolve to real players.
    try:
        sleeper = SleeperSource(http)
        registry.ingest_sleeper_universe(sleeper.players())
    except Exception as exc:  # noqa: BLE001
        log.warning("sleeper universe unavailable: %s", exc)

    cfg = LeagueConfig(key=key, league_id=league_id, season=season, name=name)
    ctx = LeagueContext(store, league_id, key, name)

    espn = ESPNFantasySource(store, registry, creds)
    espn.sync(ctx, cfg)

    scoring = _infer_scoring(ctx)
    draft_source = ESPNDraftSource(http, store, registry, creds)
    ranked = draft_source.sync_rankings(ctx, season, scoring)
    draft_source.sync_picks(ctx, season)

    engine = DraftEngine(ctx)
    board = engine.build_board(refresh=True)
    strategy = engine.strategy()

    settings = ctx.settings()
    my_team = ctx.my_team()

    payload = {
        "league_id": league_id,
        "league_key": key,
        "name": settings["name"] if settings else name,
        "season": season,
        "scoring_bucket": scoring,
        "teams": len(ctx.teams()),
        "roster_slots": ctx.roster_slots(),
        "playoff_teams": settings["playoff_teams"] if settings else None,
        "reg_season_weeks": settings["reg_season_weeks"] if settings else None,
        "waiver_type": settings["waiver_type"] if settings else None,
        "my_team": {
            "team_id": my_team["team_id"] if my_team else None,
            "name": my_team["name"] if my_team else None,
        },
        "managers": [
            {"team_id": t["team_id"], "name": t["name"], "owner": t["owner"]}
            for t in ctx.teams()
        ],
        "ranked_players": ranked,
        "strategy": strategy,
        "board": [
            {
                "player_id": p.player_id,
                "name": p.name,
                "position": p.position,
                "projected": round(p.projected, 1),
                "vor": p.vor,
                "adp": p.adp,
                "draft_rank": p.draft_rank,
                "tier": p.tier,
                "injury_status": p.injury_status,
            }
            for p in board[:250]
        ],
    }

    path = out_dir / f"draft_{key}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("wrote %s (%d ranked, %d on board)", path, ranked, len(board))

    http.close()
    store.close()
    return payload


def _infer_scoring(ctx: LeagueContext) -> str:
    """Match ESPN's own ranking bucket to this league's reception scoring."""
    scoring = ctx.scoring()
    blob = json.dumps(scoring).lower()
    # "REC" is ESPN's abbreviation for points per reception.
    rec = scoring.get("REC") if isinstance(scoring, dict) else None
    try:
        rec = float(rec)
    except (TypeError, ValueError):
        rec = None
    if rec is not None:
        if rec >= 0.9:
            return "ppr"
        if rec >= 0.4:
            return "half"
        return "standard"
    if "ppr" in blob:
        return "ppr"
    return "standard"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()

    setup_logging("INFO")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    leagues = [
        (1631980693, "L1", "League 1"),
        (1259820957, "L2", "League 2"),
    ]

    summary = []
    for league_id, key, name in leagues:
        try:
            payload = build(league_id, key, name, args.season, out_dir)
            summary.append(
                {
                    "key": key,
                    "name": payload["name"],
                    "ranked": len(payload["ranked_players"]),
                    "board": len(payload["board"]),
                    "scoring": payload["scoring_bucket"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - one league must not sink the other
            log.exception("failed to build %s", key)
            summary.append({"key": key, "error": str(exc)})

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
