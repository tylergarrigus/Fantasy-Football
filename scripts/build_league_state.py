#!/usr/bin/env python3
"""Pull the full post-draft picture for each league and write it to disk.

Runs on the GitHub Actions runner (the only place that can reach ESPN) and
commits JSON, so every downstream view -- the war room page, trade finder,
lineup calls -- reads the same facts rather than each re-deriving them.

Every roster in the league is included, not just ours. A trade needs to be good
for the other side too, and you cannot see that from your own roster alone.
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
from ff.identity import PlayerRegistry  # noqa: E402
from ff.logging_setup import get_logger, setup_logging  # noqa: E402
from ff.sources.base import HttpClient  # noqa: E402
from ff.sources.espn import ESPNFantasySource  # noqa: E402
from ff.sources.espn_draft import ESPNDraftSource  # noqa: E402
from ff.sources.sleeper import SleeperSource  # noqa: E402

log = get_logger(__name__)

STARTABLE = ("QB", "RB", "WR", "TE", "K", "DST")


def _infer_scoring(ctx: LeagueContext) -> str:
    scoring = ctx.scoring()
    rec = scoring.get("REC") if isinstance(scoring, dict) else None
    try:
        rec = float(rec)
    except (TypeError, ValueError):
        rec = None
    if rec is None:
        return "ppr" if "ppr" in json.dumps(scoring).lower() else "standard"
    if rec >= 0.9:
        return "ppr"
    return "half" if rec >= 0.4 else "standard"


def best_lineup(players: list[dict], slots: dict[str, int]) -> dict:
    """Fill the starting slots greedily by projection; return starters and total.

    Greedy is right here because the flex takes whoever is left over -- there is
    no case where benching a better player at a fixed slot frees up more points
    elsewhere.
    """
    pool = sorted(
        [p for p in players if p.get("projected") is not None],
        key=lambda p: -p["projected"],
    )
    used: set[str] = set()
    starters: list[dict] = []

    fixed = [(pos, int(slots.get(pos, 0) or 0)) for pos in STARTABLE]
    fixed = [(p, n) for p, n in fixed if n]
    for pos, count in fixed:
        taken = 0
        for p in pool:
            if taken >= count:
                break
            if p["player_id"] in used or p["position"] != pos:
                continue
            used.add(p["player_id"])
            starters.append({**p, "slot": pos})
            taken += 1

    flex_count = int(slots.get("RB/WR/TE", 0) or 0)
    if flex_count:
        taken = 0
        for p in pool:
            if taken >= flex_count:
                break
            if p["player_id"] in used or p["position"] not in ("RB", "WR", "TE"):
                continue
            used.add(p["player_id"])
            starters.append({**p, "slot": "FLEX"})
            taken += 1

    bench = [p for p in pool if p["player_id"] not in used]
    return {
        "starters": starters,
        "bench": bench,
        "projected": round(sum(p["projected"] for p in starters), 1),
        "unfilled": [
            pos
            for pos, count in fixed
            if sum(1 for s in starters if s["slot"] == pos) < count
        ],
    }


def build(league_id: int, key: str, name: str, season: int, out_dir: Path) -> dict:
    store = Store(out_dir / f"state_{key}.db")
    http = HttpClient(out_dir / ".cache")
    registry = PlayerRegistry(store)
    creds = ESPNCredentials(
        espn_s2=os.environ.get("ESPN_S2", ""), swid=os.environ.get("ESPN_SWID", "")
    )

    try:
        registry.ingest_sleeper_universe(SleeperSource(http).players())
    except Exception as exc:  # noqa: BLE001
        log.warning("sleeper universe unavailable: %s", exc)

    cfg = LeagueConfig(key=key, league_id=league_id, season=season, name=name)
    ctx = LeagueContext(store, league_id, key, name)
    ESPNFantasySource(store, registry, creds).sync(ctx, cfg)

    scoring = _infer_scoring(ctx)
    draft = ESPNDraftSource(http, store, registry, creds)
    draft.sync_rankings(ctx, season, scoring)
    draft.sync_picks(ctx, season)

    # Season projections come from the draft-rankings pull, which is the only
    # place ESPN exposes a full-season number.
    proj = {
        r["player_id"]: r["projected"]
        for r in ctx.query(
            "SELECT player_id, projected FROM draft_rankings WHERE league_id = :league_id"
        )
    }
    adp = {
        r["player_id"]: r["adp"]
        for r in ctx.query(
            "SELECT player_id, adp FROM draft_rankings WHERE league_id = :league_id"
        )
    }

    def decorate(rows) -> list[dict]:
        out = []
        for r in rows:
            pid = r["player_id"]
            out.append(
                {
                    "player_id": pid,
                    "name": r["full_name"],
                    "position": r["position"],
                    "nfl_team": r["nfl_team"] if "nfl_team" in r.keys() else None,
                    "injury_status": r["injury_status"] if "injury_status" in r.keys() else None,
                    "projected": proj.get(pid),
                    "adp": adp.get(pid),
                }
            )
        return out

    slots = ctx.roster_slots()
    teams = []
    for team in ctx.teams():
        rows = ctx.query(
            "SELECT r.player_id, p.full_name, p.position, p.nfl_team, p.injury_status "
            "FROM league_rosters r JOIN nfl_players p ON p.player_id = r.player_id "
            "WHERE r.league_id = :league_id AND r.team_id = :tid",
            tid=team["team_id"],
        )
        players = decorate(rows)
        lineup = best_lineup(players, slots)
        teams.append(
            {
                "team_id": team["team_id"],
                "name": team["name"],
                "owner": team["owner"],
                "is_me": team["team_id"] == ctx.my_team_id(),
                "players": players,
                "starters": lineup["starters"],
                "bench": lineup["bench"],
                "projected": lineup["projected"],
                "unfilled": lineup["unfilled"],
            }
        )
    teams.sort(key=lambda t: -t["projected"])
    for rank, t in enumerate(teams, 1):
        t["rank"] = rank

    fa_rows = ctx.query(
        "SELECT f.player_id, p.full_name, p.position, p.nfl_team, p.injury_status "
        "FROM league_free_agents f JOIN nfl_players p ON p.player_id = f.player_id "
        "WHERE f.league_id = :league_id LIMIT 400"
    )
    free_agents = [p for p in decorate(fa_rows) if p["projected"]]
    free_agents.sort(key=lambda p: -p["projected"])

    settings = ctx.settings()
    payload = {
        "league_id": league_id,
        "league_key": key,
        "name": settings["name"] if settings else name,
        "season": season,
        "scoring": scoring,
        "team_count": len(ctx.teams()),
        "roster_slots": slots,
        "playoff_teams": settings["playoff_teams"] if settings else None,
        "my_team_id": ctx.my_team_id(),
        "teams": teams,
        "free_agents": free_agents[:120],
    }

    path = out_dir / f"state_{key}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("wrote %s (%d teams, %d free agents)", path, len(teams), len(free_agents))

    http.close()
    store.close()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()

    setup_logging("INFO")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for league_id, key, name in (
        (1631980693, "L1", "League 1"),
        (1259820957, "L2", "League 2"),
    ):
        try:
            p = build(league_id, key, name, args.season, out_dir)
            drafted = sum(len(t["players"]) for t in p["teams"])
            summary.append(
                {
                    "key": key,
                    "name": p["name"],
                    "teams": p["team_count"],
                    "rostered": drafted,
                    "my_rank": next(
                        (t["rank"] for t in p["teams"] if t["is_me"]), None
                    ),
                    "my_projected": next(
                        (t["projected"] for t in p["teams"] if t["is_me"]), None
                    ),
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
