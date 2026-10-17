#!/usr/bin/env python3
"""Build the Fantasy Command Center -- Tyler's personal fantasy football app.

Reads data/state_*.json, data/news.json and data/decisions.json (pulled from
ESPN on the Actions runner) and writes one self-contained HTML page. The page
is a small client app (ff/web/app.js + app.css) over an embedded JSON payload:

  Home      cross-league: the one thing that matters now, both matchups, alerts
  Leagues   matchup position by position, standings, roster, rules, activity
  Players   search / filter / sort / compare every player in the league
  Moves     lineup optimizer, waiver scout, trade lab
  Insights  strength, projection vs reality, trends, byes, schedule, depth

Everything the engine decides (the action queue, trade ideas, league strength
and market reads) is computed here in Python and embedded; everything
interactive (what-if lineups, trade evaluation, scouting) is recomputed in the
browser with the same lineup math. Nothing talks to ESPN from the page.
"""

from __future__ import annotations

import base64
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ff.engines.actions import Action, build_queue  # noqa: E402
from ff.engines.trades import best_lineup, with_ros_values  # noqa: E402

DATA = ROOT / "data"
WEB = ROOT / "ff" / "web"
OUT = DATA / "command_center.html"
SKILL = ("QB", "RB", "WR", "TE")
# The leagues' real names, for data pulled before the pull learned them.
LEAGUE_NAMES = {"L1": "OJ Simpson Fantasy", "L2": "UAM Golf"}
HIST_KEYS = ("w", "pts", "proj", "played", "tgt", "car", "rec", "att")


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def load(name: str) -> dict | None:
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------- analysis


def rejected_filter(queue: list[Action], league_key: str, decisions: dict) -> list[Action]:
    """Drop trade ideas a manager has already said no to.

    A rejection is information, not a retry queue. Re-pitching the same deal
    (or the same player from the same manager) is how you stop getting your
    calls answered.
    """
    rejected = [r for r in decisions.get("rejected_trades", []) if r.get("league") == league_key]
    if not rejected:
        return queue
    out = []
    for a in queue:
        if a.kind == "trade" and a.trade:
            hit = any(
                r["partner"] == a.trade["partner_name"]
                and {p["name"] for p in a.trade["receive"]} & set(r["receive"])
                for r in rejected
            )
            if hit:
                continue
        out.append(a)
    return out


def analyse(state: dict, news_items: list[dict], decisions: dict | None = None) -> dict:
    """Engine output for one league: queue, positional strength, market read.

    Every value the engine trades on is rest-of-season: ESPN's per-game
    projection times the games a player has left, byes included.
    """
    state = with_ros_values(state)
    slots = state["roster_slots"]
    for t in state["teams"]:
        lu = best_lineup(t["players"], slots)
        t["_lineup"] = lu
        t["_total"] = lu.projected
    for rank, t in enumerate(sorted(state["teams"], key=lambda t: -t["_total"]), 1):
        t["_rank"] = rank

    me = next((t for t in state["teams"] if t.get("is_me")), None)
    drafted = bool(me and me["players"])
    rejected_pairs = [
        (r["partner"], name)
        for r in (decisions or {}).get("rejected_trades", [])
        if r.get("league") == state.get("league_key")
        for name in r.get("receive", [])
    ]
    queue = rejected_filter(
        build_queue(state, news_items, rejected=rejected_pairs,
                    trade_posture=(decisions or {}).get("trade_posture", "normal")),
        state.get("league_key", ""),
        decisions or {},
    )

    strength: dict[str, dict] = {}
    market: list[dict] = []
    if drafted:
        for pos in SKILL:
            per_team = [
                (t["team_id"], sum(s["projected"] for s in t["_lineup"].starters if s["position"] == pos))
                for t in state["teams"]
            ]
            vals = sorted(p for _, p in per_team)
            median = vals[len(vals) // 2]
            my_pts = next(p for tid, p in per_team if tid == me["team_id"])
            strength[pos] = {"mine": round(my_pts, 1), "median": round(median, 1), "edge": round(my_pts - median, 1)}

        league_start = {
            pos: sorted((s["projected"] for t in state["teams"] for s in t["_lineup"].starters
                         if s["position"] == pos), reverse=True)
            for pos in SKILL
        }
        for t in state["teams"]:
            needs, surplus = [], []
            for pos in SKILL:
                starters = [s for s in t["_lineup"].starters if s["position"] == pos]
                if not starters:
                    continue
                worst = min(s["projected"] for s in starters)
                pool = league_start[pos]
                if pool and worst <= pool[max(0, int(len(pool) * 0.75) - 1)]:
                    needs.append(pos)
                strong = [b for b in t["_lineup"].bench
                          if b["position"] == pos and (b.get("projected") or 0) >= max(worst * 0.85, 150)]
                for b in strong[:1]:
                    surplus.append({"pos": pos, "name": b["name"], "pid": b["player_id"]})
            market.append({"teamId": t["team_id"], "needs": needs, "surplus": surplus})

    return {"state": state, "me": me, "drafted": drafted, "queue": queue,
            "strength": strength, "market": market}


# ------------------------------------------------------------- payload


HEADSHOTS = DATA / "headshots"


def photo(espn_id) -> str | None:
    """The player's photo as a data URI, when the runner has fetched one."""
    if espn_id is None:
        return None
    f = HEADSHOTS / f"{espn_id}.webp"
    if not f.exists():
        return None
    return "data:image/webp;base64," + base64.b64encode(f.read_bytes()).decode("ascii")


def slim_player(p: dict, owner: int | None) -> dict:
    hist = [{k: r.get(k) for k in HIST_KEYS if r.get(k) is not None} for r in (p.get("history") or [])]
    return {
        "id": p["player_id"], "eid": p.get("espn_id"), "n": p["name"], "pos": p["position"],
        "team": p.get("nfl_team"), "inj": p.get("injury_status"), "slot": p.get("current_slot"),
        "owner": owner, "proj": p.get("season_proj", p.get("projected")), "ros": p.get("ros"), "wk": p.get("week_projected"),
        "opp": p.get("opponent"), "bye": bool(p.get("on_bye")), "wpts": p.get("week_pts"),
        "played": p.get("played"), "oppRank": p.get("opp_rank"), "kick": p.get("kickoff"),
        "spts": p.get("season_pts"), "avg": p.get("avg_pts"), "pavg": p.get("proj_avg"),
        "posRank": p.get("pos_rank"), "own": p.get("pct_owned"), "byes": p.get("bye_weeks") or [],
        "hist": hist,
    }


def action_payload(a: Action, key: str, msgs: dict) -> dict:
    k = f"{key}|{a.kind}|{a.title}"
    if a.draft_message:
        msgs[k] = a.draft_message
    return {
        "key": k, "lg": key, "kind": a.kind, "title": a.title, "why": a.why,
        "conf": a.confidence, "confWhy": a.confidence_why, "deadline": a.deadline,
        "steps": a.steps, "gain": round(a.gain, 1), "urgency": a.urgency,
        "players": a.players, "msg": bool(a.draft_message), "trade": a.trade,
        "tag": a.tag, "note": a.note,
    }


def league_payload(a: dict, news: dict, msgs: dict) -> dict:
    st = a["state"]
    key = st["league_key"]
    players: dict[str, dict] = {}
    teams = []
    for t in st["teams"]:
        for p in t["players"]:
            players[p["player_id"]] = slim_player(p, t["team_id"])
        teams.append({
            "id": t["team_id"], "name": (t["name"] or "").strip(), "owner": t.get("owner"),
            "me": bool(t.get("is_me")), "w": t.get("wins"), "l": t.get("losses"), "t": t.get("ties"),
            "pf": t.get("points_for"), "pa": t.get("points_against"), "standing": t.get("standing"),
            "streak": t.get("streak"), "po": t.get("playoff_pct"), "outcomes": t.get("outcomes") or [],
            "scores": t.get("scores") or [], "sched": t.get("schedule") or [],
            "waiverRank": t.get("waiver_rank"), "faabSpent": t.get("faab_spent"),
            "rank": t["_rank"], "lineupProj": round(t["_total"], 1),
            "players": [p["player_id"] for p in t["players"]],
        })
    fa = []
    for p in st.get("free_agents", []):
        if p.get("projected") and p["player_id"] not in players:
            players[p["player_id"]] = slim_player(p, None)
            fa.append(p["player_id"])

    by_eid = {str(p["eid"]): p for p in players.values() if p.get("eid")}
    my_id = st.get("my_team_id")
    items = []
    for item in news.get("items", []):
        hits = [by_eid[str(i)] for i in item.get("espn_athlete_ids", []) if str(i) in by_eid]
        if not hits:
            continue
        items.append({
            "h": item["headline"], "url": item.get("url"), "src": item.get("source"),
            "when": (item.get("published") or "")[:10],
            "who": ", ".join(dict.fromkeys(p["n"] for p in hits)),
            "pids": [p["id"] for p in hits],
            "mine": any(p["owner"] == my_id for p in hits),
        })

    return {
        "key": key, "name": LEAGUE_NAMES.get(key, st["name"]) if str(st["name"]).startswith("League ") else st["name"],
        "id": st["league_id"], "teamCount": st["team_count"],
        "slots": st["roster_slots"], "scoring": st.get("scoring") or "", "rules": st.get("scoring_rules") or [],
        "playoffTeams": st.get("playoff_teams"), "regWeeks": st.get("reg_season_weeks"),
        "waiverType": st.get("waiver_type"), "faabBudget": st.get("faab_budget"),
        "myId": my_id, "week": st.get("current_week"), "pulledAt": st.get("pulled_at"),
        "extrasOk": bool(st.get("extras_ok")),
        "teams": teams, "players": players, "fa": fa,
        "matchups": [{"home": m["home"], "away": m["away"], "hs": m.get("home_score"), "as": m.get("away_score"),
                      "hp": m.get("home_proj"), "ap": m.get("away_proj")} for m in st.get("matchups", [])],
        "activity": st.get("activity") or [],
        "queue": [action_payload(x, key, msgs) for x in a["queue"]],
        "strength": a["strength"], "market": a["market"], "news": items[:30],
    }


def build() -> str:
    news = load("news.json") or {"items": []}
    decisions = load("decisions.json") or {}
    msgs: dict[str, str] = {}
    leagues: dict[str, dict] = {}
    week = season = None
    for key in ("L1", "L2"):
        state = load(f"state_{key}.json")
        if not state:
            continue
        a = analyse(state, news["items"], decisions)
        if not a["drafted"]:
            continue
        leagues[key] = league_payload(a, news, msgs)
        week = week or state.get("current_week")
        season = season or state.get("season")

    # One photo per ESPN id, shared across leagues.
    photos: dict[str, str] = {}
    for lg in leagues.values():
        for p in lg["players"].values():
            eid = p.get("eid")
            if eid is not None and str(eid) not in photos:
                img = photo(eid)
                if img:
                    photos[str(eid)] = img

    now = datetime.now(timezone.utc)
    payload = {
        "season": season, "week": week, "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": list(leagues.keys()), "leagues": leagues, "photos": photos,
    }
    data_js = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    msg_js = json.dumps(msgs, separators=(",", ":")).replace("</", "<\\/")
    return (PAGE
            .replace("{{CSS}}", (WEB / "app.css").read_text())
            .replace("{{JS}}", (WEB / "app.js").read_text())
            .replace("{{DATA}}", data_js)
            .replace("{{MSGS}}", msg_js))


PAGE = """<title>Fantasy Command Center</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:ital,wdth,wght@0,62..125,400..900;1,62..125,400..900&display=swap">
<style>
{{CSS}}
</style>
<div class="glow" aria-hidden="true"></div>
<div class="app">
  <header class="top" id="top"></header>
  <main id="main"></main>
</div>
<div class="dock"><nav class="nav" id="nav" aria-label="Sections"></nav></div>
<div id="sheet"></div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>window.FCC={{DATA}};window.FCC_MSG={{MSGS}};</script>
<script>
{{JS}}
</script>
"""


def main() -> int:
    OUT.write_text(build())
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
