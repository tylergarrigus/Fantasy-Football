#!/usr/bin/env python3
"""Read-only reconnaissance against one ESPN league.

Answers the questions that have to be answered before the agent can be
configured, and that cannot be answered from a network-restricted dev session:

  * Is this league public, or does it need cookies?
  * What are the scoring settings, roster slots, and playoff structure?
  * FAAB or waiver priority, and what's the budget?
  * Who are the teams, and which one is the user's?
  * Has the draft happened yet?

Prints nothing that isn't already visible to anyone who can open the league,
and never prints a credential.

    python scripts/probe_league.py --league-id 123456 --season 2026
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.logging_setup import setup_logging  # noqa: E402


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def probe(league_id: int, season: int, team_name: str | None) -> int:
    from espn_api.football import League
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
    )

    espn_s2 = os.environ.get("ESPN_S2", "").strip()
    swid = os.environ.get("ESPN_SWID", "").strip()
    have_cookies = bool(espn_s2 and swid)

    section("Access")
    print(f"League ID : {league_id}")
    print(f"Season    : {season}")
    print(f"Cookies   : {'provided' if have_cookies else 'none (trying public access)'}")

    league = None
    used_auth = False

    # Public first, always. No reason to put account-wide cookies on the wire
    # for a league anyone can read.
    try:
        league = League(league_id=league_id, year=season)
        print("\nRESULT: league is PUBLIC -- no ESPN cookies needed. ")
    except ESPNInvalidLeague:
        print(
            f"\nRESULT: ESPN does not recognise league {league_id} for {season}.\n"
            "  Check the leagueId in your URL, and that the season year is right.\n"
            "  (A league created for 2026 will 404 if you probe it as 2025.)"
        )
        return 1
    except ESPNAccessDenied:
        print("\nRESULT: league is PRIVATE.")
        if not have_cookies:
            print(
                "  Cookies are required. Add ESPN_S2 and ESPN_SWID as repository\n"
                "  secrets and re-run this probe.\n\n"
                "  To get them: log into ESPN, open DevTools > Application (Chrome)\n"
                "  or Storage (Firefox) > Cookies > espn.com, and copy the values of\n"
                "  'espn_s2' and 'SWID'. SWID includes the curly braces."
            )
            return 2
        try:
            league = League(league_id=league_id, year=season, espn_s2=espn_s2, swid=swid)
            used_auth = True
            print("  Cookies accepted. Access granted.")
        except Exception as exc:  # noqa: BLE001
            print(
                f"  Cookies were REJECTED ({type(exc).__name__}).\n"
                "  They have most likely expired -- re-extract them from your browser.\n"
                "  Note that logging out of ESPN invalidates espn_s2."
            )
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"\nRESULT: unexpected failure -- {type(exc).__name__}: {exc}")
        return 1

    settings = getattr(league, "settings", None)

    section("League")
    print(f"Name          : {getattr(settings, 'name', 'unknown')}")
    print(f"Teams         : {len(getattr(league, 'teams', []) or [])}")
    print(f"Current week  : {getattr(league, 'current_week', 'unknown')}")
    print(f"Reg season    : {getattr(settings, 'reg_season_count', 'unknown')} weeks")
    print(f"Playoff teams : {getattr(settings, 'playoff_team_count', 'unknown')}")
    print(f"Keeper league : {getattr(settings, 'keeper_count', 0) or 'no'}")

    section("Scoring")
    scoring_type = getattr(settings, "scoring_type", None)
    print(f"Type : {scoring_type or 'unknown'}")
    fmt = getattr(settings, "scoring_format", None)
    if isinstance(fmt, list):
        interesting = {
            "receptions": "PPR", "receivingYards": "rec yds",
            "rushingYards": "rush yds", "passingTD": "pass TD",
            "receivingTD": "rec TD", "rushingTD": "rush TD",
        }
        for item in fmt:
            if not isinstance(item, dict):
                continue
            abbr = item.get("abbr") or item.get("label")
            if abbr in interesting or (item.get("points") not in (0, None)):
                print(f"  {abbr:<22} {item.get('points')}")
    else:
        print("  DATA UNAVAILABLE -- scoring detail not exposed by this endpoint")

    section("Roster slots")
    slots = getattr(settings, "position_slot_counts", None)
    if isinstance(slots, dict):
        for position, count in sorted(slots.items()):
            if count:
                print(f"  {position:<10} {count}")
    else:
        print("  DATA UNAVAILABLE")

    section("Waivers")
    faab = getattr(settings, "faab_budget", None)
    if faab:
        print(f"Type   : FAAB")
        print(f"Budget : ${faab}")
    else:
        print(f"Type   : {getattr(settings, 'waiver_type', 'waiver priority')} (no FAAB budget found)")

    section("Teams")
    my_team = None
    for team in getattr(league, "teams", []) or []:
        owners = getattr(team, "owners", None) or []
        owner_names = []
        for owner in owners:
            if isinstance(owner, dict):
                name = " ".join(
                    filter(None, [owner.get("firstName"), owner.get("lastName")])
                ).strip()
                owner_names.append(name or owner.get("displayName") or "?")
            else:
                owner_names.append(str(owner))

        marker = ""
        if team_name and team_name.strip().lower() in (team.team_name or "").lower():
            marker = "   <-- YOU (matched by name)"
            my_team = team
        print(
            f"  [{team.team_id:>2}] {team.team_name:<28} "
            f"{getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)}  "
            f"{', '.join(owner_names) or 'unknown owner'}{marker}"
        )

    if used_auth and my_team is None:
        swid_clean = swid.strip("{}").upper()
        for team in getattr(league, "teams", []) or []:
            for owner in getattr(team, "owners", None) or []:
                oid = owner.get("id") if isinstance(owner, dict) else str(owner)
                if oid and str(oid).strip("{}").upper() == swid_clean:
                    my_team = team
                    print(f"\n  Auto-detected your team from SWID: [{team.team_id}] {team.team_name}")

    section("Draft")
    try:
        draft = getattr(league, "draft", None) or []
        if draft:
            print(f"Draft has happened -- {len(draft)} picks recorded.")
            print("First 5 picks:")
            for pick in draft[:5]:
                print(
                    f"  R{getattr(pick, 'round_num', '?')}.{getattr(pick, 'round_pick', '?'):<3} "
                    f"{getattr(getattr(pick, 'playerName', None), '__str__', lambda: '')() or getattr(pick, 'playerName', '?')}"
                )
        else:
            print("No draft data -- the draft has NOT happened yet.")
            print("This is a pre-draft league.")
    except Exception as exc:  # noqa: BLE001
        print(f"DATA UNAVAILABLE -- could not read draft ({exc})")

    if my_team is not None:
        section(f"Your roster ({my_team.team_name})")
        roster = getattr(my_team, "roster", []) or []
        if not roster:
            print("  Empty -- consistent with a pre-draft league.")
        for player in roster:
            status = getattr(player, "injuryStatus", None) or ""
            proj = getattr(player, "projected_points", None)
            print(
                f"  {getattr(player, 'position', '??'):<4} "
                f"{getattr(player, 'name', '?'):<26} "
                f"{getattr(player, 'proTeam', '?'):<4} "
                f"proj {proj if proj is not None else 'n/a'}"
                f"{'  [' + status + ']' if status and status != 'ACTIVE' else ''}"
            )

    section("Configuration to use")
    print(json.dumps(
        {
            "league_id": league_id,
            "season": season,
            "requires_cookies": used_auth,
            "team_id": getattr(my_team, "team_id", None),
            "waiver": "faab" if faab else "priority",
            "faab_budget": faab,
        },
        indent=2,
    ))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--team-name", default="")
    args = parser.parse_args()

    setup_logging("WARNING")
    return probe(args.league_id, args.season, args.team_name or None)


if __name__ == "__main__":
    raise SystemExit(main())
