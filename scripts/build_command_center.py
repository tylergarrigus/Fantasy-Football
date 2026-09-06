#!/usr/bin/env python3
"""Build the Fantasy Command Center -- one calm page that runs both leagues.

Reads data/state_*.json and data/news.json (pulled from ESPN on the Actions
runner) and writes a single self-contained HTML page organized around one
question: what should Tyler do right now?

Views: Today (the action queue), My Team, Market, Trades (including a live
in-page evaluator for incoming offers), Waivers, News, Settings. League data is
embedded so the trade evaluator works entirely client-side -- no server, no
account, nothing to install.

Nothing here submits anything to ESPN. Every action ends in the exact clicks
to do it yourself; approval-first is the design, not a disclaimer.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ff.engines.actions import Action, build_queue  # noqa: E402
from ff.engines.trades import best_lineup  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "command_center.html"

SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "D/ST", "K"]
SKILL = ("QB", "RB", "WR", "TE")


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def load(name: str) -> dict | None:
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


def pos_chip(pos: str) -> str:
    key = "DST" if pos == "D/ST" else pos
    return f'<span class="p p-{esc(key)}">{esc(pos)}</span>'


# ------------------------------------------------------------- analysis


def rejected_filter(queue: list[Action], league_key: str, decisions: dict) -> list[Action]:
    """Drop trade ideas a manager has already said no to.

    A rejection is information, not a retry queue. Re-pitching the same deal
    (or the same player from the same manager) is how you stop getting your
    calls answered.
    """
    rejected = [
        r for r in decisions.get("rejected_trades", [])
        if r.get("league") == league_key
    ]
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
    if not out:
        out.append(Action(
            kind="info",
            title="No action needed",
            why="The remaining trade ideas were already offered and declined, "
                "nobody on waivers would crack your lineup, and no starter is in "
                "injury trouble. Sitting tight is the right move -- rejected "
                "offers get re-examined when rosters or projections change.",
            confidence="High",
            confidence_why="every roster and the full free-agent pool was checked",
            deadline="—", steps="—", urgency=0,
        ))
    return out


def analyse(state: dict, news_items: list[dict], decisions: dict | None = None) -> dict:
    slots = state["roster_slots"]
    for t in state["teams"]:
        lu = best_lineup(t["players"], slots)
        t["_lineup"] = lu
        t["_total"] = lu.projected
    state["teams"].sort(key=lambda t: -t["_total"])
    for rank, t in enumerate(state["teams"], 1):
        t["_rank"] = rank

    me = next((t for t in state["teams"] if t.get("is_me")), None)
    drafted = bool(me and me["players"])
    queue = rejected_filter(
        build_queue(state, news_items),
        state.get("league_key", ""),
        decisions or {},
    )

    # Positional strength: my starters at each position vs the league median.
    strength: dict[str, dict] = {}
    if drafted:
        for pos in SKILL:
            per_team = []
            for t in state["teams"]:
                pts = sum(
                    s["projected"] for s in t["_lineup"].starters
                    if s["position"] == pos
                )
                per_team.append((t["team_id"], pts))
            vals = sorted(p for _, p in per_team)
            median = vals[len(vals) // 2]
            my_pts = next(p for tid, p in per_team if tid == me["team_id"])
            strength[pos] = {
                "mine": my_pts,
                "median": median,
                "edge": my_pts - median,
            }

    # Each rival's needs and surpluses, from lineup construction -- the input
    # to "who would actually take this call".
    market = []
    if drafted:
        league_start = {
            pos: sorted(
                (s["projected"] for t in state["teams"]
                 for s in t["_lineup"].starters if s["position"] == pos),
                reverse=True,
            )
            for pos in SKILL
        }
        for t in state["teams"]:
            needs, surplus = [], []
            for pos in SKILL:
                starters = [s for s in t["_lineup"].starters if s["position"] == pos]
                bench = [b for b in t["_lineup"].bench if b["position"] == pos
                         and (b.get("projected") or 0) > 0]
                if starters:
                    worst = min(s["projected"] for s in starters)
                    pool = league_start[pos]
                    # Bottom quartile of the league's starters at that spot.
                    if pool and worst <= pool[max(0, int(len(pool) * 0.75) - 1)]:
                        needs.append(pos)
                strong_bench = [
                    b for b in bench
                    if b["projected"] >= (min(s["projected"] for s in starters)
                                          if starters else 0) * 0.85
                    and b["projected"] >= 150
                ]
                if strong_bench:
                    surplus.append((pos, strong_bench[0]))
            market.append({"team": t, "needs": needs, "surplus": surplus})

    return {
        "state": state, "me": me, "drafted": drafted,
        "queue": queue, "strength": strength, "market": market,
    }


# ------------------------------------------------------------- views


CONF_CLASS = {"High": "hi", "Medium": "md", "Low": "lo"}


def render_action(a: Action, i: int, league: str) -> str:
    conf = f'<span class="conf {CONF_CLASS.get(a.confidence, "lo")}">' \
           f'{esc(a.confidence)}</span>'
    urgent = ' data-urgent="1"' if a.urgency >= 2 else ""
    msg = ""
    if a.draft_message:
        msg = (
            '<div class="msg"><span class="msg-l">Message to send '
            '<button class="copy" data-copy>Copy</button></span>'
            f'<p>{esc(a.draft_message)}</p></div>'
        )
    controls = ""
    if a.kind != "info":
        controls = (
            '<div class="act-do">'
            '<button class="btn yes" data-yes>Approve</button>'
            '<button class="btn no" data-no>Pass</button></div>'
            f'<div class="steps" hidden><p><b>Do it:</b> {esc(a.steps)}</p></div>'
        )
    return f"""
<article class="act k-{esc(a.kind)}" data-act="{esc(league)}-{i}"{urgent}>
  <header class="act-h">
    <span class="kind">{esc(a.kind)}</span>
    {conf}
    <span class="dl">{esc(a.deadline)}</span>
  </header>
  <h3>{esc(a.title)}</h3>
  <p class="why">{esc(a.why)}</p>
  <p class="conf-why">Confidence: {esc(a.confidence)} &mdash; {esc(a.confidence_why)}.</p>
  {msg}{controls}
</article>"""


def render_today(a: dict, key: str) -> str:
    cards = "".join(
        render_action(act, i, key) for i, act in enumerate(a["queue"])
    )
    quiet = ""
    if all(act.kind == "info" for act in a["queue"]):
        quiet = '<p class="allclear">Quiet day. That is the system working.</p>'
    return f'<div class="view" data-view="today">{quiet}<div class="acts">{cards}</div></div>'


def render_team(a: dict) -> str:
    if not a["drafted"]:
        return ('<div class="view" data-view="team" hidden><p class="empty">'
                "No roster yet &mdash; this league hasn't drafted. Everything here "
                "fills in automatically afterward.</p></div>")
    me = a["me"]
    lu = me["_lineup"]
    order = {s: i for i, s in enumerate(SLOT_ORDER)}
    starters = sorted(lu.starters, key=lambda s: (order.get(s["slot"], 9), -s["projected"]))

    def prow(s, slot=True):
        status = (s.get("injury_status") or "").upper()
        flag = ""
        if status and status not in ("ACTIVE", "NORMAL"):
            cls = "bad" if status in ("OUT", "DOUBTFUL", "INJURY_RESERVE") else "warn"
            flag = f' <span class="flag {cls}">{esc(status[:3])}</span>'
        left = f'<td class="slot">{esc(s["slot"])}</td>' if slot else ""
        return (f'<tr>{left}<td class="who">{esc(s["name"])}{flag}</td>'
                f'<td>{pos_chip(s["position"])}</td>'
                f'<td class="n">{(s.get("projected") or 0):.0f}</td></tr>')

    srows = "".join(prow(s) for s in starters)
    brows = "".join(
        prow(p, slot=False)
        for p in sorted(lu.bench, key=lambda p: -(p.get("projected") or 0))
    )

    bars = ""
    for pos, s in a["strength"].items():
        edge = s["edge"]
        cls = "good" if edge > 12 else ("bad" if edge < -12 else "even")
        word = "strong" if edge > 12 else ("thin" if edge < -12 else "league-average")
        bars += (f'<li class="{cls}">{pos_chip(pos)}<span class="st-w">{word}</span>'
                 f'<span class="st-n">{edge:+.0f} vs median</span></li>')

    state = a["state"]
    po = state.get("playoff_teams")
    outlook = ""
    if po:
        making = me["_rank"] <= po
        outlook = (
            f"Projections put you <b>{me['_rank']} of {state['team_count']}</b>; "
            f"top {po} make the playoffs, so you are currently "
            f"{'inside' if making else 'outside'} the line. "
            "That is a preseason estimate, not a fate."
        )

    plan = "".join(
        f"<li>{esc(act.title)}</li>"
        for act in a["queue"] if act.kind != "info"
    )[:2000] or "<li>Nothing this week. Check back after the first games.</li>"

    return f"""
<div class="view" data-view="team" hidden>
  <div class="two">
    <section class="panel">
      <h2 class="sh">Starters</h2>
      <table class="grid"><tbody>{srows}</tbody></table>
      <p class="tot"><span>Season projection</span><b>{lu.projected:,.0f}</b></p>
    </section>
    <section class="panel">
      <h2 class="sh">Bench</h2>
      <table class="grid bench"><tbody>{brows}</tbody></table>
    </section>
  </div>
  <div class="two">
    <section class="panel">
      <h2 class="sh">Positional strength</h2>
      <ul class="strength">{bars}</ul>
    </section>
    <section class="panel">
      <h2 class="sh">Playoff outlook</h2>
      <p class="prose">{outlook}</p>
      <h2 class="sh" style="margin-top:18px">This week's plan</h2>
      <ol class="plan">{plan}</ol>
    </section>
  </div>
</div>"""


def render_market(a: dict) -> str:
    if not a["drafted"]:
        return ('<div class="view" data-view="market" hidden><p class="empty">'
                "The market opens when the league drafts.</p></div>")
    rows = ""
    for m in sorted(a["market"], key=lambda m: m["team"]["_rank"]):
        t = m["team"]
        needs = " ".join(pos_chip(p) for p in m["needs"]) or '<span class="none">none</span>'
        surplus = ", ".join(
            f'{esc(pl["name"])} ({esc(pos)})' for pos, pl in m["surplus"]
        ) or "&mdash;"
        fit = ""
        if not t.get("is_me") and m["needs"]:
            my_surplus = next(
                (pos for pos, _ in next(
                    (x["surplus"] for x in a["market"] if x["team"].get("is_me")), []
                ) if pos in m["needs"]), None,
            )
            if my_surplus:
                fit = f'<span class="fit">fit: they need {esc(my_surplus)}, you have spare</span>'
        me_cls = ' class="mine"' if t.get("is_me") else ""
        rows += (f'<tr{me_cls}><td class="rk">{t["_rank"]}</td>'
                 f'<td class="who">{esc(t["name"])}{fit}</td>'
                 f'<td class="n">{t["_total"]:,.0f}</td>'
                 f'<td>{needs}</td><td class="sur">{surplus}</td></tr>')
    return f"""
<div class="view" data-view="market" hidden>
  <section class="panel">
    <h2 class="sh">Every team, honestly read</h2>
    <p class="sub">Needs are starting spots in the league's bottom quartile.
       Surplus is a benched player good enough to start elsewhere &mdash; that
       pair is what makes a trade real.</p>
    <div class="scroll"><table class="grid market">
      <thead><tr><th></th><th>Team</th><th class="n">Proj</th><th>Needs</th><th>Surplus</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
  </section>
</div>"""


def render_trades(a: dict, key: str) -> str:
    if not a["drafted"]:
        return ('<div class="view" data-view="trades" hidden><p class="empty">'
                "Trades open when rosters exist.</p></div>")
    outgoing = "".join(
        render_action(act, 100 + i, key)
        for i, act in enumerate(act for act in a["queue"] if act.kind == "trade")
    ) or '<p class="empty">No outgoing offer clears the bar right now.</p>'
    return f"""
<div class="view" data-view="trades" hidden>
  <section class="panel">
    <h2 class="sh">Offers worth making</h2>
    <div class="acts">{outgoing}</div>
  </section>
  <section class="panel">
    <h2 class="sh">Got an offer? Evaluate it</h2>
    <p class="sub">Pick who they want and who they're giving. Verdict comes from
       both starting lineups, this league's slots and scoring &mdash; not a generic
       trade calculator.</p>
    <div class="eval">
      <div class="ev-col">
        <label class="lbl" for="ev-partner">From</label>
        <select id="ev-partner" class="sel"></select>
        <label class="lbl">They give you</label>
        <div id="ev-get" class="picklist"></div>
      </div>
      <div class="ev-col">
        <label class="lbl">You give them</label>
        <div id="ev-send" class="picklist"></div>
      </div>
    </div>
    <button class="btn yes" id="ev-run">Evaluate</button>
    <div id="ev-out" class="verdict" hidden></div>
  </section>
  <section class="panel">
    <h2 class="sh">Decision log</h2>
    <p class="sub">Everything you approve, pass on, or evaluate is recorded here,
       on this device.</p>
    <ul id="audit" class="audit"><li class="empty">Nothing yet.</li></ul>
  </section>
</div>"""


def render_waivers(a: dict) -> str:
    if not a["drafted"]:
        return ('<div class="view" data-view="waivers" hidden><p class="empty">'
                "Waivers open after the draft.</p></div>")
    state = a["state"]
    me = a["me"]
    base = me["_lineup"].projected
    rows = ""
    for fa in state.get("free_agents", [])[:40]:
        if not fa.get("projected"):
            continue
        trial = best_lineup(me["players"] + [fa], state["roster_slots"])
        gain = trial.projected - base
        now = "starts for you" if gain >= 3 else "depth only"
        cls = "good" if gain >= 3 else ""
        rows += (f'<tr class="{cls}"><td class="who">{esc(fa["name"])}</td>'
                 f'<td>{pos_chip(fa["position"])}</td>'
                 f'<td class="n">{fa["projected"]:.0f}</td>'
                 f'<td class="n">{gain:+.0f}</td><td>{now}</td></tr>')
    wt = state.get("waiver_type") or "unknown"
    note = ("Bids come out of your FAAB budget &mdash; the queue on Today says how much."
            if wt == "faab" else
            "This league uses waiver priority, so a claim also spends your spot in line "
            "&mdash; the +points column says when that's worth it.")
    return f"""
<div class="view" data-view="waivers" hidden>
  <section class="panel">
    <h2 class="sh">Free agents, measured against your roster</h2>
    <p class="sub">"+Lineup" is what adding him does to <em>your</em> starting
       lineup &mdash; the only number that matters. {note}</p>
    <div class="scroll"><table class="grid">
      <thead><tr><th>Player</th><th></th><th class="n">Proj</th>
      <th class="n">+Lineup</th><th>Verdict</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
  </section>
</div>"""


def render_news(a: dict, news: dict) -> str:
    state = a["state"]
    rostered: dict[str, tuple[str, str]] = {}
    for t in state["teams"]:
        for p in t["players"]:
            if p.get("espn_id"):
                rostered[str(p["espn_id"])] = (p["name"], t["name"])
    items = ""
    linked = 0
    for item in news.get("items", []):
        hits = [rostered[str(i)] for i in item.get("espn_athlete_ids", [])
                if str(i) in rostered]
        if not hits:
            continue
        linked += 1
        who = "; ".join(f"<b>{esc(n)}</b> ({esc(tm)})" for n, tm in hits)
        src = (f' &middot; <a href="{esc(item["url"])}" target="_blank" rel="noopener">'
               f'{esc(item.get("source","source"))}</a>' if item.get("url") else "")
        when = esc((item.get("published") or "")[:10])
        items += (f'<li><p class="nh">{esc(item["headline"])}</p>'
                  f'<p class="nm">Affects {who}{src}'
                  f'{" &middot; " + when if when else ""}</p></li>')
    fetched = esc((news.get("fetched_at") or "")[:16].replace("T", " "))
    if not linked:
        items = ('<li class="empty">No current headline touches a rostered player '
                 "in this league. Quiet is good.</li>")
    return f"""
<div class="view" data-view="news" hidden>
  <section class="panel">
    <h2 class="sh">News that touches this league</h2>
    <p class="sub">Only stories naming a rostered player appear here. Anything
       that changes a decision also shows up on Today as an action.
       {f"Last checked {fetched} UTC." if fetched else ""}</p>
    <ul class="news">{items}</ul>
  </section>
</div>"""


def render_settings(key: str) -> str:
    return f"""
<div class="view" data-view="settings" hidden>
  <section class="panel">
    <h2 class="sh">Notifications</h2>
    <p class="sub">These control what Claude includes when it messages you about
       this league. Nothing here creates extra pings &mdash; it only removes them.</p>
    <ul class="prefs" data-league="{esc(key)}">
      <li><label><input type="checkbox" data-pref="digest" checked>
        Daily digest &mdash; one message, only when something changed</label></li>
      <li><label><input type="checkbox" data-pref="deadlines" checked>
        Deadline reminders &mdash; waivers and lineup lock</label></li>
      <li><label><input type="checkbox" data-pref="urgent" checked>
        Urgent only: starter injured, incoming trade, time-sensitive pickup</label></li>
      <li class="quiet-row"><label><input type="checkbox" data-pref="quiet">
        <b>Quiet mode</b> &mdash; nothing but urgent, until you turn it off</label></li>
    </ul>
  </section>
  <section class="panel">
    <h2 class="sh">Untouchables</h2>
    <p class="sub">Players the system must never offer in a trade. The evaluator
       and counteroffers respect this list immediately.</p>
    <div class="untouch"><input type="text" id="untouch-in"
      placeholder="Type a player's name and press Enter"><ul id="untouch-list"></ul></div>
  </section>
  <section class="panel">
    <h2 class="sh">Automation</h2>
    <p class="prose">Nothing is ever submitted to ESPN automatically &mdash; ESPN
       doesn't allow it, and this product wouldn't do it anyway. The flow is
       always: it recommends, you approve, you make the one click in ESPN.
       The rules below shape <em>recommendations only</em>.</p>
    <ul class="prefs">
      <li><label>Flag trades as "accept-worthy" only above
        <input type="number" id="rule-threshold" value="10" min="0" max="100"
         class="numin"> lineup points</label></li>
      <li><label><input type="checkbox" id="rule-injured" checked>
        Never recommend accepting a trade for an OUT/IR player without a warning</label></li>
    </ul>
  </section>
</div>"""


# ------------------------------------------------------------- shell


def client_data(analyses: dict[str, dict]) -> str:
    """The slim JSON the in-page evaluator needs: rosters + slots per league."""
    payload = {}
    for key, a in analyses.items():
        if not a["drafted"]:
            continue
        st = a["state"]
        payload[key] = {
            "slots": st["roster_slots"],
            "myTeamId": st["my_team_id"],
            "teams": [
                {
                    "id": t["team_id"], "name": t["name"], "me": bool(t.get("is_me")),
                    "players": [
                        {"id": p["player_id"], "n": p["name"], "pos": p["position"],
                         "pr": p.get("projected"), "inj": p.get("injury_status")}
                        for p in t["players"]
                    ],
                }
                for t in st["teams"]
            ],
        }
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def build() -> str:
    news = load("news.json") or {"items": []}
    decisions = load("decisions.json") or {}
    analyses: dict[str, dict] = {}
    tabs, blocks = [], []
    for key in ("L1", "L2"):
        state = load(f"state_{key}.json")
        if not state:
            continue
        a = analyse(state, news["items"], decisions)
        analyses[key] = a
        first = not tabs
        n_act = sum(1 for x in a["queue"] if x.kind != "info")
        badge = f'<i class="badge">{n_act}</i>' if n_act else ""
        tabs.append(
            f'<button data-league="{key}" aria-pressed="{"true" if first else "false"}">'
            f'{esc(state["name"])}{badge}</button>'
        )
        blocks.append(f"""
<section class="league" id="lg-{key}"{"" if first else " hidden"}>
  {render_today(a, key)}
  {render_team(a)}
  {render_market(a)}
  {render_trades(a, key)}
  {render_waivers(a)}
  {render_news(a, news)}
  {render_settings(key)}
</section>""")

    stamp = datetime.now(timezone.utc).strftime("%b %-d, %H:%M UTC")
    return (PAGE
            .replace("{{TABS}}", "".join(tabs))
            .replace("{{BLOCKS}}", "".join(blocks))
            .replace("{{DATA}}", client_data(analyses))
            .replace("{{STAMP}}", stamp))


PAGE = r"""<title>Fantasy Command Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500;600&family=Roboto+Mono:wght@500&display=swap">
<style>
:root{
  --ground:#0a0d12; --panel:#12171f; --panel2:#191f29; --line:#262e3a;
  --ink:#eef1f6; --muted:#8b95a7; --faint:#5c6678;
  --hot:#ff3b1f; --good:#33d17a; --warn:#f5b942;
  --qb:#a97bff; --rb:#2fd4c4; --wr:#ffb020; --te:#ff6e8a; --dst:#7d8a9e; --k:#7d8a9e;
  --disp:"Barlow Condensed",Impact,sans-serif;
  --body:Barlow,system-ui,-apple-system,sans-serif;
  --mono:"Roboto Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ground);color:var(--ink);font:400 16.5px/1.5 var(--body)}
a{color:var(--ink)}
.wrap{max-width:1060px;margin:0 auto;padding:0 18px 90px}

/* top */
.top{display:flex;align-items:center;justify-content:space-between;gap:14px;
     padding:20px 0 14px;flex-wrap:wrap}
.mark{font:700 19px/1 var(--disp);letter-spacing:.10em;text-transform:uppercase}
.mark i{color:var(--hot);font-style:normal}
.stamp{font:500 11.5px/1 var(--mono);color:var(--faint)}

/* league switch */
.lgs{display:flex;gap:8px;margin-bottom:4px}
.lgs button{appearance:none;cursor:pointer;border:1px solid var(--line);
  background:var(--panel);color:var(--muted);padding:9px 18px;
  font:700 14px/1 var(--disp);letter-spacing:.12em;text-transform:uppercase;
  display:flex;align-items:center;gap:8px}
.lgs button[aria-pressed="true"]{background:var(--panel2);color:var(--ink);
  border-color:var(--hot);border-bottom-width:2px}
.badge{font:500 11px/1 var(--mono);font-style:normal;background:var(--hot);
  color:#fff;padding:2px 6px;border-radius:8px}
.lgs button:focus-visible,.nav button:focus-visible{outline:2px solid var(--hot);outline-offset:-2px}

/* view nav */
.nav{display:flex;gap:0;margin:10px 0 26px;border-bottom:2px solid var(--line);
     overflow-x:auto;scrollbar-width:none}
.nav button{appearance:none;border:0;background:none;cursor:pointer;color:var(--muted);
  padding:10px 15px 12px;margin-bottom:-2px;white-space:nowrap;
  font:700 13.5px/1 var(--disp);letter-spacing:.12em;text-transform:uppercase;
  border-bottom:2px solid transparent}
.nav button[aria-pressed="true"]{color:var(--ink);border-bottom-color:var(--hot)}

/* actions */
.allclear{margin:0 0 14px;color:var(--good);font:700 20px/1.2 var(--disp);
          letter-spacing:.04em;text-transform:uppercase}
.acts{display:flex;flex-direction:column;gap:13px}
.act{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
     padding:18px 20px 16px}
.act.k-trade{border-left-color:var(--hot)}
.act.k-waiver{border-left-color:var(--warn)}
.act.k-injury,[data-urgent]{border-left-color:var(--hot)}
.act.k-info{border-left-color:var(--good)}
.act[data-state="done"]{border-left-color:var(--good);opacity:.95}
.act[data-state="passed"]{opacity:.45}
.act-h{display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}
.kind{font:500 10.5px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--hot)}
.k-waiver .kind{color:var(--warn)}.k-info .kind{color:var(--good)}
.conf{font:500 10.5px/1 var(--mono);letter-spacing:.1em;padding:3px 7px;border:1px solid}
.conf.hi{color:var(--good);border-color:var(--good)}
.conf.md{color:var(--warn);border-color:var(--warn)}
.conf.lo{color:var(--faint);border-color:var(--faint)}
.dl{margin-left:auto;font-size:12.5px;color:var(--faint)}
.act h3{margin:0 0 10px;font:700 clamp(21px,3.6vw,28px)/1.08 var(--disp);text-wrap:balance}
.why{margin:0;color:var(--muted);font-size:15px;max-width:68ch}
.conf-why{margin:7px 0 0;color:var(--faint);font-size:13px;max-width:68ch}
.msg{margin-top:13px;background:var(--panel2);border:1px solid var(--line);padding:11px 13px}
.msg-l{display:flex;justify-content:space-between;align-items:center;
  font:500 10.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.msg p{margin:8px 0 0;font-size:14.5px;color:var(--ink)}
.copy{appearance:none;cursor:pointer;border:1px solid var(--line);background:none;
  color:var(--muted);padding:4px 10px;font:500 10.5px/1 var(--mono);letter-spacing:.1em}
.copy:hover{color:var(--ink);border-color:var(--muted)}
.act-do{display:flex;gap:10px;margin-top:14px}
.btn{appearance:none;cursor:pointer;border:1px solid var(--line);padding:11px 24px;
  background:var(--panel2);color:var(--ink);
  font:700 13.5px/1 var(--disp);letter-spacing:.13em;text-transform:uppercase}
.btn.yes{background:var(--hot);border-color:var(--hot);color:#fff}
.btn.yes:hover{background:#ff5537}
.btn.no:hover{border-color:var(--muted);color:var(--muted)}
.btn:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.steps{margin-top:12px;padding:11px 14px;background:var(--panel2);border-left:3px solid var(--good)}
.steps p{margin:0;font-size:14.5px;color:var(--muted)}
.steps b{color:var(--ink)}

/* panels/tables */
.two{display:grid;grid-template-columns:1fr 1fr;gap:13px}
@media(max-width:740px){.two{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);padding:18px 20px;margin-bottom:13px}
.sh{margin:0 0 6px;font:700 13.5px/1 var(--disp);letter-spacing:.16em;
    text-transform:uppercase;color:var(--muted)}
.sub{margin:0 0 14px;color:var(--faint);font-size:13.5px;max-width:74ch}
.prose{margin:0;color:var(--muted);font-size:15px}
.prose b{color:var(--ink)}
.empty{color:var(--faint);font-size:15px}
.grid{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.grid th{text-align:left;font:500 10.5px/1 var(--mono);letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);padding:0 8px 8px 0;border-bottom:1px solid var(--line)}
.grid th.n{text-align:right}
.grid td{padding:8px 8px 8px 0;border-bottom:1px solid var(--line);vertical-align:middle}
.grid tr:last-child td{border-bottom:0}
.grid .slot{width:46px;font:700 11.5px/1 var(--disp);letter-spacing:.1em;color:var(--faint)}
.grid .who{font-weight:500;font-size:15.5px}
.grid .n{text-align:right;font:500 13px/1 var(--mono)}
.grid .rk{width:26px;font:500 12px/1 var(--mono);color:var(--faint)}
.grid tr.mine td{background:rgba(255,59,31,.05)}
.grid tr.mine .who{color:var(--ink);font-weight:600}
.grid tr.good .who{color:var(--good)}
.bench .who{color:var(--muted)}
.market .sur{font-size:13.5px;color:var(--muted)}
.fit{display:block;font-size:11.5px;color:var(--good)}
.none{color:var(--faint);font-size:12.5px}
.scroll{overflow-x:auto}
.tot{display:flex;justify-content:space-between;align-items:baseline;margin:14px 0 0;
     padding-top:12px;border-top:2px solid var(--line)}
.tot span{font:500 10.5px/1 var(--mono);letter-spacing:.15em;text-transform:uppercase;color:var(--faint)}
.tot b{font:700 27px/1 var(--disp)}
.flag{display:inline-block;margin-left:6px;padding:2px 5px;
  font:700 9.5px/1 var(--mono);letter-spacing:.05em;vertical-align:1px}
.flag.warn{background:var(--warn);color:#231a00}
.flag.bad{background:var(--hot);color:#fff}

.p{display:inline-block;min-width:36px;text-align:center;padding:3px 6px;
   font:700 10.5px/1 var(--mono);letter-spacing:.04em;color:#0a0d12}
.p-QB{background:var(--qb)}.p-RB{background:var(--rb)}.p-WR{background:var(--wr)}
.p-TE{background:var(--te)}.p-DST{background:var(--dst)}.p-K{background:var(--k)}

.strength{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
.strength li{display:flex;align-items:center;gap:11px}
.st-w{font-weight:600;font-size:15px}
.strength .good .st-w{color:var(--good)}
.strength .bad .st-w{color:var(--hot)}
.st-n{margin-left:auto;font:500 12.5px/1 var(--mono);color:var(--faint)}
.plan{margin:0;padding-left:20px;color:var(--muted);font-size:14.5px}
.plan li{margin-bottom:7px}

/* evaluator */
.eval{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:700px){.eval{grid-template-columns:1fr}}
.lbl{display:block;margin:0 0 6px;font:500 10.5px/1 var(--mono);
  letter-spacing:.15em;text-transform:uppercase;color:var(--faint)}
.sel,.numin,#untouch-in{background:var(--panel2);border:1px solid var(--line);
  color:var(--ink);padding:9px 11px;font:500 14.5px/1.3 var(--body);width:100%}
.sel{margin-bottom:12px}
.numin{width:74px;padding:5px 8px;margin:0 6px}
.picklist{display:flex;flex-direction:column;gap:2px;max-height:250px;overflow-y:auto;
  border:1px solid var(--line);background:var(--panel2);padding:6px}
.picklist label{display:flex;align-items:center;gap:9px;padding:6px 8px;cursor:pointer;font-size:14.5px}
.picklist label:hover{background:var(--panel)}
.picklist .pr{margin-left:auto;font:500 12px/1 var(--mono);color:var(--faint)}
.verdict{margin-top:14px;padding:16px 18px;background:var(--panel2);border-left:3px solid var(--hot)}
.verdict.ok{border-left-color:var(--good)}
.verdict h4{margin:0 0 8px;font:700 24px/1 var(--disp);letter-spacing:.03em;text-transform:uppercase}
.verdict p{margin:0 0 6px;color:var(--muted);font-size:14.5px}
.verdict .counter{margin-top:10px;padding:10px 12px;background:var(--panel);border:1px solid var(--line)}
.verdict .counter b{font-size:15px}
.verdict .counter span{display:block;color:var(--faint);font-size:13px;margin-top:3px}

/* audit + news + prefs */
.audit{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.audit li{font-size:14px;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:8px}
.audit li:last-child{border-bottom:0}
.audit time{font:500 11.5px/1 var(--mono);color:var(--faint);margin-right:9px}
.news{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
.nh{margin:0;font-weight:600;font-size:15.5px}
.nm{margin:3px 0 0;font-size:13.5px;color:var(--faint)}
.nm b{color:var(--muted)}
.prefs{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:11px}
.prefs label{display:flex;align-items:center;gap:10px;font-size:15px;color:var(--muted);cursor:pointer}
.prefs input[type=checkbox]{width:17px;height:17px;accent-color:var(--hot)}
.quiet-row label{color:var(--ink)}
.untouch ul{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.untouch li{background:var(--panel2);border:1px solid var(--line);padding:6px 11px;
  font-size:14px;display:flex;gap:8px;align-items:center}
.untouch li button{appearance:none;border:0;background:none;color:var(--faint);
  cursor:pointer;font-size:15px;padding:0}
.foot{margin-top:32px;color:var(--faint);font-size:13px;max-width:74ch}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
  <div class="top">
    <span class="mark">Fantasy <i>Command Center</i></span>
    <span class="stamp">Data updated {{STAMP}}</span>
  </div>
  <nav class="lgs">{{TABS}}</nav>
  <nav class="nav">
    <button data-nav="today" aria-pressed="true">Today</button>
    <button data-nav="team" aria-pressed="false">My Team</button>
    <button data-nav="market" aria-pressed="false">Market</button>
    <button data-nav="trades" aria-pressed="false">Trades</button>
    <button data-nav="waivers" aria-pressed="false">Waivers</button>
    <button data-nav="news" aria-pressed="false">News</button>
    <button data-nav="settings" aria-pressed="false">Settings</button>
  </nav>

  {{BLOCKS}}

  <p class="foot">Every number is ESPN's own projection under this league's scoring,
     refreshed twice daily. Nothing is ever submitted automatically &mdash; you approve,
     then make the one click in ESPN. Decisions and settings live on this device.</p>
</div>

<script>
var LEAGUES = {{DATA}};
(function(){
  "use strict";
  function store(key, val){ try{ localStorage.setItem(key, JSON.stringify(val)); }catch(e){} }
  function read(key, fb){ try{ return JSON.parse(localStorage.getItem(key)) || fb; }catch(e){ return fb; } }

  /* ---------- league + view switching ---------- */
  var lgBtns = document.querySelectorAll(".lgs button");
  var navBtns = document.querySelectorAll(".nav button");
  function currentLeague(){
    var b = document.querySelector('.lgs button[aria-pressed="true"]');
    return b ? b.dataset.league : null;
  }
  function show(){
    var lg = currentLeague();
    var nb = document.querySelector('.nav button[aria-pressed="true"]');
    var view = nb ? nb.dataset.nav : "today";
    document.querySelectorAll(".league").forEach(function(sec){
      sec.hidden = sec.id !== "lg-" + lg;
      sec.querySelectorAll(".view").forEach(function(v){
        v.hidden = v.dataset.view !== view;
      });
    });
  }
  lgBtns.forEach(function(b){ b.addEventListener("click", function(){
    lgBtns.forEach(function(x){ x.setAttribute("aria-pressed", String(x===b)); });
    initEval(); show();
  });});
  navBtns.forEach(function(b){ b.addEventListener("click", function(){
    navBtns.forEach(function(x){ x.setAttribute("aria-pressed", String(x===b)); });
    show();
  });});

  /* ---------- decisions: approve / pass + audit ---------- */
  var decisions = read("fcc-decisions", {});
  var audit = read("fcc-audit", []);
  function logAudit(text){
    audit.unshift({t: new Date().toISOString().slice(0,16).replace("T"," "), x: text});
    audit = audit.slice(0, 40); store("fcc-audit", audit); paintAudit();
  }
  function paintAudit(){
    document.querySelectorAll("#audit").forEach(function(ul){
      if(!audit.length){ ul.innerHTML = '<li class="empty">Nothing yet.</li>'; return; }
      ul.innerHTML = audit.map(function(a){
        return "<li><time>"+a.t+"</time>"+a.x.replace(/</g,"&lt;")+"</li>";
      }).join("");
    });
  }
  function paintCard(card, state){
    card.dataset.state = state || "";
    var st = card.querySelector(".steps"); if(st) st.hidden = state !== "done";
    var y = card.querySelector("[data-yes]");
    if(y) y.textContent = state === "done" ? "Approved" : "Approve";
  }
  document.querySelectorAll(".act[data-act]").forEach(function(card){
    var id = card.dataset.act;
    if(decisions[id]) paintCard(card, decisions[id]);
    var title = (card.querySelector("h3")||{}).textContent || id;
    var y = card.querySelector("[data-yes]"), n = card.querySelector("[data-no]");
    if(y) y.addEventListener("click", function(){
      decisions[id] = decisions[id]==="done" ? "" : "done";
      store("fcc-decisions", decisions); paintCard(card, decisions[id]);
      if(decisions[id]) logAudit("Approved: " + title);
    });
    if(n) n.addEventListener("click", function(){
      decisions[id] = decisions[id]==="passed" ? "" : "passed";
      store("fcc-decisions", decisions); paintCard(card, decisions[id]);
      if(decisions[id]) logAudit("Passed: " + title);
    });
  });

  /* copy buttons */
  document.querySelectorAll("[data-copy]").forEach(function(btn){
    btn.addEventListener("click", function(){
      var p = btn.closest(".msg").querySelector("p");
      var done = function(){ btn.textContent = "Copied"; setTimeout(function(){ btn.textContent="Copy"; }, 1500); };
      if(navigator.clipboard){ navigator.clipboard.writeText(p.textContent).then(done, done); }
    });
  });

  /* ---------- prefs, untouchables, rules ---------- */
  var prefs = read("fcc-prefs", {});
  document.querySelectorAll(".prefs [data-pref]").forEach(function(cb){
    var lg = cb.closest(".prefs").dataset.league || "all";
    var k = lg + ":" + cb.dataset.pref;
    if(k in prefs) cb.checked = prefs[k];
    cb.addEventListener("change", function(){ prefs[k] = cb.checked; store("fcc-prefs", prefs); });
  });
  var untouch = read("fcc-untouchables", []);
  function paintUntouch(){
    document.querySelectorAll("#untouch-list").forEach(function(ul){
      ul.innerHTML = untouch.map(function(n,i){
        return "<li>"+n.replace(/</g,"&lt;")+'<button data-ui="'+i+'" aria-label="Remove">&times;</button></li>';
      }).join("");
      ul.querySelectorAll("[data-ui]").forEach(function(b){
        b.addEventListener("click", function(){
          untouch.splice(+b.dataset.ui,1); store("fcc-untouchables", untouch); paintUntouch();
        });
      });
    });
  }
  document.querySelectorAll("#untouch-in").forEach(function(inp){
    inp.addEventListener("keydown", function(e){
      if(e.key === "Enter" && inp.value.trim()){
        untouch.push(inp.value.trim()); inp.value="";
        store("fcc-untouchables", untouch); paintUntouch();
      }
    });
  });
  var rules = read("fcc-rules", {threshold: 10, injured: true});
  document.querySelectorAll("#rule-threshold").forEach(function(el){
    el.value = rules.threshold;
    el.addEventListener("change", function(){ rules.threshold = +el.value || 0; store("fcc-rules", rules); });
  });
  document.querySelectorAll("#rule-injured").forEach(function(el){
    el.checked = rules.injured !== false;
    el.addEventListener("change", function(){ rules.injured = el.checked; store("fcc-rules", rules); });
  });

  /* ---------- lineup math (mirror of the Python engine) ---------- */
  var FIXED = ["QB","RB","WR","TE","K","D/ST"], FLEXP = {RB:1,WR:1,TE:1};
  function lineupValue(players, slots){
    var pool = players.filter(function(p){ return p.pr != null; })
                      .slice().sort(function(a,b){ return b.pr - a.pr; });
    var used = {}, total = 0;
    FIXED.forEach(function(pos){
      var need = +(slots[pos]||0);
      for(var i=0;i<pool.length && need>0;i++){
        var p = pool[i];
        if(used[p.id] || p.pos !== pos) continue;
        used[p.id]=1; total+=p.pr; need--;
      }
    });
    var flex = +(slots["RB/WR/TE"]||0);
    for(var j=0;j<pool.length && flex>0;j++){
      var q = pool[j];
      if(used[q.id] || !FLEXP[q.pos]) continue;
      used[q.id]=1; total+=q.pr; flex--;
    }
    return total;
  }
  function swap(roster, out, inn){
    var gone = {}; out.forEach(function(p){ gone[p.id]=1; });
    return roster.filter(function(p){ return !gone[p.id]; }).concat(inn);
  }

  /* ---------- incoming-trade evaluator ---------- */
  function initEval(){
    var lg = LEAGUES[currentLeague()]; if(!lg) return;
    var partnerSel = document.querySelector("#lg-"+currentLeague()+" #ev-partner");
    if(!partnerSel || partnerSel.dataset.ready) { fillPick(); return; }
    partnerSel.dataset.ready = "1";
    lg.teams.filter(function(t){ return !t.me; }).forEach(function(t){
      var o = document.createElement("option");
      o.value = t.id; o.textContent = t.name; partnerSel.appendChild(o);
    });
    partnerSel.addEventListener("change", fillPick);
    fillPick();

    function fillPick(){
      var sec = document.querySelector("#lg-"+currentLeague());
      if(!sec) return;
      var me = lg.teams.filter(function(t){ return t.me; })[0];
      var them = lg.teams.filter(function(t){ return String(t.id) === partnerSel.value; })[0]
                 || lg.teams.filter(function(t){ return !t.me; })[0];
      function list(el, team){
        el.innerHTML = team.players.slice().sort(function(a,b){ return (b.pr||0)-(a.pr||0); })
          .map(function(p){
            return '<label><input type="checkbox" value="'+p.id+'">'+
              p.n.replace(/</g,"&lt;")+' ('+p.pos+')'+
              (p.inj && p.inj !== "ACTIVE" ? ' <span class="flag warn">'+p.inj.slice(0,3)+"</span>":"")+
              '<span class="pr">'+(p.pr!=null ? Math.round(p.pr) : "&mdash;")+"</span></label>";
          }).join("");
      }
      list(sec.querySelector("#ev-get"), them);
      list(sec.querySelector("#ev-send"), me);
    }
  }
  initEval();

  document.querySelectorAll("#ev-run").forEach(function(btn){
    btn.addEventListener("click", function(){
      var key = currentLeague(), lg = LEAGUES[key]; if(!lg) return;
      var sec = document.querySelector("#lg-"+key);
      var me = lg.teams.filter(function(t){ return t.me; })[0];
      var pid = sec.querySelector("#ev-partner").value;
      var them = lg.teams.filter(function(t){ return String(t.id)===pid; })[0];
      function picked(id, team){
        var ids = Array.prototype.map.call(
          sec.querySelectorAll(id+" input:checked"), function(i){ return i.value; });
        return team.players.filter(function(p){ return ids.indexOf(p.id)>=0; });
      }
      var get = picked("#ev-get", them), send = picked("#ev-send", me);
      var out = sec.querySelector("#ev-out"); out.hidden = false;
      if(!get.length && !send.length){
        out.className = "verdict"; out.innerHTML = "<p>Pick at least one player on either side.</p>"; return;
      }
      var myBase = lineupValue(me.players, lg.slots);
      var myAfter = lineupValue(swap(me.players, send, get), lg.slots);
      var thBase = lineupValue(them.players, lg.slots);
      var thAfter = lineupValue(swap(them.players, get, send), lg.slots);
      var myGain = myAfter - myBase, thGain = thAfter - thBase;
      var injured = get.filter(function(p){
        return p.inj && ["OUT","DOUBTFUL","INJURY_RESERVE","IR"].indexOf(p.inj.toUpperCase())>=0; });

      var verdict, cls = "verdict", body = "";
      var thr = rules.threshold || 10;
      if(injured.length && rules.injured !== false){
        verdict = "Hold"; body = "<p><b>"+injured[0].n+"</b> is listed "+injured[0].inj+
          ". Your rules say never accept a deal for an injured player without a second look.</p>";
      } else if(myGain >= thr){
        verdict = "Accept"; cls += " ok";
        body = "<p>Your starting lineup improves by ~"+Math.round(myGain)+
          " points over the season. That clears your "+thr+"-point bar.</p>";
      } else if(myGain >= 0){
        verdict = "Hold";
        body = "<p>You gain ~"+Math.round(myGain)+" points -- inside projection noise. "+
          "No harm accepting, no rush either.</p>";
      } else {
        verdict = "Decline"; body = "<p>This costs your starting lineup ~"+
          Math.round(-myGain)+" points. Their side improves by ~"+Math.round(thGain)+".</p>";
      }

      /* counters: what WOULD work with this partner */
      var counters = [];
      if(verdict !== "Accept"){
        var mine = me.players.filter(function(p){
          return p.pr != null && untouch.indexOf(p.n) < 0; });
        var packs = [];
        mine.forEach(function(p){ packs.push([p]); });
        for(var i=0;i<mine.length;i++) for(var j=i+1;j<mine.length;j++) packs.push([mine[i],mine[j]]);
        them.players.forEach(function(target){
          if(target.pr == null) return;
          packs.forEach(function(pk){
            var mg = lineupValue(swap(me.players, pk, [target]), lg.slots) - myBase;
            var tg = lineupValue(swap(them.players, [target], pk), lg.slots) - thBase;
            if(mg >= 5 && tg >= 3) counters.push({send: pk, get: target, mg: mg, tg: tg});
          });
        });
        counters.sort(function(a,b){ return (b.tg+b.mg)-(a.tg+a.mg); });
        var seen = {}; counters = counters.filter(function(c){
          if(seen[c.get.id]) return false; seen[c.get.id]=1; return true; }).slice(0,3);
        if(counters.length && verdict === "Decline") verdict = "Counter";
      }
      var chtml = counters.map(function(c){
        return '<div class="counter"><b>Send '+
          c.send.map(function(p){return p.n;}).join(" + ")+" for "+c.get.n+"</b>"+
          "<span>you ~+"+Math.round(c.mg)+", them ~+"+Math.round(c.tg)+
          " -- realistic because they gain too</span></div>";
      }).join("");
      out.className = cls;
      out.innerHTML = "<h4>"+verdict+"</h4>"+body+chtml;
      logAudit("Evaluated offer from "+them.name+": "+
        (get.map(function(p){return p.n;}).join(", ")||"nothing")+" for "+
        (send.map(function(p){return p.n;}).join(", ")||"nothing")+" -> "+verdict);
    });
  });

  paintAudit(); paintUntouch(); show();
})();
</script>
"""


def main() -> int:
    OUT.write_text(build())
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
