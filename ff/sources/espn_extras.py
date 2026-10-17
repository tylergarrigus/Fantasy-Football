"""Facts the page needs that the league sync does not store.

The sync keeps the durable, league-isolated state (rosters, free agents,
projections). This module reads the richer, more volatile things straight off
the espn_api objects after a sync: live matchup scores, each player's week-by-
week production and usage, opponent rank, bye weeks, and team records.

Every lookup is defensive. ESPN's response shape drifts, and a missing field
must degrade to "unavailable" on the page -- never to an invented number.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from ff.logging_setup import get_logger

log = get_logger(__name__)

# Usage columns worth keeping per week. Short keys keep the JSON small.
USAGE_KEYS = {
    "receivingTargets": "tgt",
    "receivingReceptions": "rec",
    "receivingYards": "rey",
    "receivingTouchdowns": "retd",
    "rushingAttempts": "car",
    "rushingYards": "ruy",
    "rushingTouchdowns": "rutd",
    "passingAttempts": "att",
    "passingYards": "pay",
    "passingTouchdowns": "patd",
}

_MIN_INTERVAL = 1.0
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _f(value: Any, digits: int = 2) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _team_id(obj: Any) -> int | None:
    """Schedules hold Team objects in some versions, bare ids in others."""
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return int(obj)
    return _i(getattr(obj, "team_id", None))


# ---------------------------------------------------------------- teams


def team_records(league: Any) -> dict[int, dict[str, Any]]:
    """Record, standing, points, streak, ESPN's playoff odds, full schedule."""
    out: dict[int, dict[str, Any]] = {}
    for t in getattr(league, "teams", []) or []:
        tid = _i(getattr(t, "team_id", None))
        if tid is None:
            continue
        streak_type = str(getattr(t, "streak_type", "") or "")
        streak_len = _i(getattr(t, "streak_length", 0)) or 0
        out[tid] = {
            "wins": _i(getattr(t, "wins", 0)) or 0,
            "losses": _i(getattr(t, "losses", 0)) or 0,
            "ties": _i(getattr(t, "ties", 0)) or 0,
            "points_for": _f(getattr(t, "points_for", 0), 1) or 0.0,
            "points_against": _f(getattr(t, "points_against", 0), 1) or 0.0,
            "standing": _i(getattr(t, "standing", None)),
            "streak": f"{streak_type[:1]}{streak_len}" if streak_type and streak_len else None,
            "playoff_pct": _f(getattr(t, "playoff_pct", None), 1),
            "outcomes": [str(o) for o in (getattr(t, "outcomes", []) or [])],
            "scores": [_f(s, 1) for s in (getattr(t, "scores", []) or [])],
            "schedule": [_team_id(o) for o in (getattr(t, "schedule", []) or [])],
            "faab_spent": _i(getattr(t, "acquisition_budget_spent", None)),
            "waiver_rank": _i(getattr(t, "waiver_rank", None)),
            "trades": _i(getattr(t, "trades", 0)) or 0,
            "acquisitions": _i(getattr(t, "acquisitions", 0)) or 0,
        }
    return out


# ---------------------------------------------------------------- matchups


def live_matchups(league: Any, week: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """This week's box scores: matchup totals plus every lineup player's live line.

    Scores are whatever ESPN had at pull time -- the page labels them with the
    pull timestamp, and a projection is always shown as a projection.
    """
    try:
        _throttle()
        boxes = league.box_scores(week) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("box scores unavailable for week %s: %s", week, exc)
        return [], {}

    matchups: list[dict[str, Any]] = []
    live: dict[int, dict[str, Any]] = {}
    for b in boxes:
        home, away = _team_id(getattr(b, "home_team", None)), _team_id(getattr(b, "away_team", None))
        matchups.append({
            "home": home, "away": away,
            "home_score": _f(getattr(b, "home_score", None), 1),
            "away_score": _f(getattr(b, "away_score", None), 1),
            "home_proj": _f(getattr(b, "home_projected", None), 1),
            "away_proj": _f(getattr(b, "away_projected", None), 1),
            "playoff": bool(getattr(b, "is_playoff", False)),
        })
        for team_id, lineup in ((home, getattr(b, "home_lineup", [])), (away, getattr(b, "away_lineup", []))):
            for bp in lineup or []:
                pid = _i(getattr(bp, "playerId", None))
                if pid is None:
                    continue
                opp = getattr(bp, "pro_opponent", None)
                game = getattr(bp, "game_date", None)
                live[pid] = {
                    "team": team_id,
                    "slot": getattr(bp, "slot_position", None),
                    "pts": _f(getattr(bp, "points", None), 1),
                    "proj": _f(getattr(bp, "projected_points", None), 1),
                    "played": _i(getattr(bp, "game_played", 0)) or 0,
                    "opp": None if opp in (None, "None", "") else str(opp),
                    "opp_rank": _i(getattr(bp, "pro_pos_rank", None)) or None,
                    "bye": bool(getattr(bp, "on_bye_week", False)),
                    "kickoff": game.isoformat(timespec="minutes") if hasattr(game, "isoformat") else None,
                }
    return matchups, live


# ---------------------------------------------------------------- players


def week_row(stats: dict[Any, Any], week: int) -> dict[str, Any] | None:
    """One week of a player's stats: actual, projected, and the usage behind it."""
    entry = stats.get(week)
    if entry is None:
        entry = stats.get(str(week))
    if not isinstance(entry, dict):
        return None
    row: dict[str, Any] = {"w": week}
    if entry.get("points") is not None:
        row["pts"] = _f(entry["points"], 1)
    if entry.get("projected_points") is not None:
        row["proj"] = _f(entry["projected_points"], 1)
    breakdown = entry.get("breakdown") or {}
    if isinstance(breakdown, dict):
        row["played"] = bool(breakdown)
        for key, short in USAGE_KEYS.items():
            v = _f(breakdown.get(key), 1)
            if v:
                row[short] = v
    return row if ("pts" in row or "proj" in row) else None


def player_facts(player: Any, max_week: int) -> dict[str, Any]:
    """Everything durable about one player: season line, weekly history, byes."""
    stats = getattr(player, "stats", None) or {}
    history = [r for r in (week_row(stats, w) for w in range(1, max_week + 1)) if r]
    facts: dict[str, Any] = {
        "history": history,
        "season_pts": _f(getattr(player, "total_points", None), 1),
        "avg_pts": _f(getattr(player, "avg_points", None), 1),
        "proj_total": _f(getattr(player, "projected_total_points", None), 1),
        "proj_avg": _f(getattr(player, "projected_avg_points", None), 1),
        "pos_rank": _i(getattr(player, "posRank", None)) or None,
        "pct_owned": _f(getattr(player, "percent_owned", None), 1),
        "pct_started": _f(getattr(player, "percent_started", None), 1),
        "bye_weeks": bye_weeks(player),
    }
    if facts["pct_owned"] is not None and facts["pct_owned"] < 0:
        facts["pct_owned"] = None
    if facts["pct_started"] is not None and facts["pct_started"] < 0:
        facts["pct_started"] = None
    return facts


def bye_weeks(player: Any) -> list[int]:
    schedule = getattr(player, "schedule", None) or {}
    weeks = {int(k) for k in schedule.keys() if str(k).isdigit()}
    if not weeks:
        return []
    return [w for w in range(1, max(max(weeks), 18) + 1) if w not in weeks][:2]


def player_cards(league: Any, espn_ids: Iterable[int], batch: int = 40) -> list[Any]:
    """Full player cards (all weeks of stats) in batches; failures skip the batch."""
    ids = list(dict.fromkeys(i for i in (_i(x) for x in espn_ids) if i is not None))
    out: list[Any] = []
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        try:
            _throttle()
            res = league.player_info(playerId=chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("player cards failed for %d ids: %s", len(chunk), exc)
            continue
        if res is None:
            continue
        out.extend(res if isinstance(res, list) else [res])
    return out


# ---------------------------------------------------------------- settings / activity


def scoring_rules(league: Any) -> list[dict[str, Any]]:
    settings = getattr(league, "settings", None)
    fmt = getattr(settings, "scoring_format", None) or []
    rules = []
    for item in fmt:
        if not isinstance(item, dict):
            continue
        pts = _f(item.get("points"), 2)
        if not pts:
            continue
        rules.append({"abbr": str(item.get("abbr") or ""), "label": str(item.get("label") or ""), "points": pts})
    return rules


def recent_activity(league: Any, size: int = 30) -> list[dict[str, Any]]:
    try:
        _throttle()
        activity = league.recent_activity(size=size) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("recent activity unavailable: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for item in activity:
        date = _i(getattr(item, "date", None))
        for action in getattr(item, "actions", []) or []:
            parts = list(action) + [None, None, None, None]
            team, kind, player, bid = parts[:4]
            rows.append({
                "at": date,
                "team": getattr(team, "team_name", None) or (str(team) if team else None),
                "team_id": _i(getattr(team, "team_id", None)),
                "kind": str(kind),
                "player": getattr(player, "name", None) or (str(player) if player else None),
                "espn_id": _i(getattr(player, "playerId", None)),
                "bid": _i(bid) or None,
            })
    return rows


# ---------------------------------------------------------------- assembly


def collect_extras(
    league: Any,
    week: int,
    espn_ids: Iterable[int],
    free_agent_objects: Iterable[Any] = (),
) -> dict[str, Any]:
    """Everything above, assembled once per league. Each part fails alone."""
    out: dict[str, Any] = {"records": {}, "matchups": [], "live": {}, "players": {},
                           "scoring_rules": [], "activity": []}
    try:
        out["records"] = team_records(league)
    except Exception as exc:  # noqa: BLE001
        log.warning("team records failed: %s", exc)
    try:
        out["matchups"], out["live"] = live_matchups(league, week)
    except Exception as exc:  # noqa: BLE001
        log.warning("live matchups failed: %s", exc)
    try:
        out["scoring_rules"] = scoring_rules(league)
    except Exception as exc:  # noqa: BLE001
        log.warning("scoring rules failed: %s", exc)
    try:
        out["activity"] = recent_activity(league)
    except Exception as exc:  # noqa: BLE001
        log.warning("activity failed: %s", exc)

    # Roster players and free agents first (always present), then the richer
    # player cards overwrite what they cover.
    by_id: dict[int, Any] = {}
    for t in getattr(league, "teams", []) or []:
        for p in getattr(t, "roster", []) or []:
            pid = _i(getattr(p, "playerId", None))
            if pid is not None:
                by_id[pid] = p
    for p in free_agent_objects or []:
        pid = _i(getattr(p, "playerId", None))
        if pid is not None:
            by_id[pid] = p
    wanted = [i for i in (_i(x) for x in espn_ids) if i is not None]
    try:
        for p in player_cards(league, wanted):
            pid = _i(getattr(p, "playerId", None))
            if pid is not None:
                by_id[pid] = p
    except Exception as exc:  # noqa: BLE001
        log.warning("player cards failed: %s", exc)

    for pid in wanted:
        p = by_id.get(pid)
        if p is None:
            continue
        try:
            out["players"][pid] = player_facts(p, week)
        except Exception as exc:  # noqa: BLE001
            log.debug("player facts failed for %s: %s", pid, exc)
    # Free-agent BoxPlayers carry this week's opponent and defensive rank too.
    for p in free_agent_objects or []:
        pid = _i(getattr(p, "playerId", None))
        if pid is None or pid in out["live"]:
            continue
        opp = getattr(p, "pro_opponent", None)
        out["live"][pid] = {
            "team": None, "slot": "FA",
            "pts": _f(getattr(p, "points", None), 1),
            "proj": _f(getattr(p, "projected_points", None), 1),
            "played": _i(getattr(p, "game_played", 0)) or 0,
            "opp": None if opp in (None, "None", "") else str(opp),
            "opp_rank": _i(getattr(p, "pro_pos_rank", None)) or None,
            "bye": bool(getattr(p, "on_bye_week", False)),
            "kickoff": None,
        }
    log.info(
        "extras: %d records, %d matchups, %d live lines, %d player facts, %d rules, %d activity rows",
        len(out["records"]), len(out["matchups"]), len(out["live"]), len(out["players"]),
        len(out["scoring_rules"]), len(out["activity"]),
    )
    return out
