#!/usr/bin/env python3
"""Build the Fantasy Command Center -- a small personal app for both leagues.

Reads data/state_*.json and data/news.json (pulled from ESPN on the Actions
runner) and writes one self-contained HTML page with three tabs:

  Today   -- the few moves worth making, biggest first
  Lineup  -- this week's lineup as set in ESPN, with byes, injuries and news
  Trades  -- every trade idea, plus a checker for offers that come in

Nothing here submits anything to ESPN. Every move is approval-first: the page
says what to do, Tyler does it in the ESPN app.
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

SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "D/ST", "K"]
SLOT_LABEL = {"RB/WR/TE": "FLEX", "D/ST": "DST"}
BAD = {"OUT", "INJURY_RESERVE", "IR", "SUSPENSION", "DOUBTFUL", "PUP", "NA"}
SHORT = {"INJURY_RESERVE": "IR", "QUESTIONABLE": "Q", "DOUBTFUL": "D",
         "SUSPENSION": "SUS", "OUT": "OUT", "IR": "IR"}


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def load(name: str) -> dict | None:
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


def status_of(p: dict) -> str:
    return (p.get("injury_status") or "").upper().replace(" ", "_")


def flag(p: dict) -> tuple[str, str] | None:
    """The one tag a player row needs, or none."""
    if p.get("on_bye"):
        return "BYE", "bad"
    st = status_of(p)
    if st in BAD:
        return SHORT.get(st, st[:3]), "bad"
    if st == "QUESTIONABLE":
        return "Q", "warn"
    return None


def flag_html(p: dict) -> str:
    f = flag(p)
    return f'<span class="tag {f[1]}">{esc(f[0])}</span>' if f else ""


def pos_html(pos: str) -> str:
    key = "dst" if pos == "D/ST" else pos.lower()
    label = "DST" if pos == "D/ST" else pos
    return f'<span class="pos pos-{esc(key)}">{esc(label)}</span>'


def week_pts(p: dict) -> float:
    if p.get("on_bye") or status_of(p) in BAD:
        return 0.0
    if p.get("week_projected") is not None:
        return float(p["week_projected"])
    return float(p.get("projected") or 0) / 17.0


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


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
                "injury trouble.",
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

    # Name -> player row, mine first, so action cards can draw real rows.
    people: dict[str, dict] = {}
    for p in state.get("free_agents", []):
        people[p["name"]] = p
    for t in state["teams"]:
        if not t.get("is_me"):
            for p in t["players"]:
                people[p["name"]] = p
    if me:
        for p in me["players"]:
            people[p["name"]] = p

    return {"state": state, "me": me, "drafted": drafted,
            "queue": queue, "people": people}


def split_queue(queue: list[Action]) -> tuple[list[Action], list[Action], list[Action]]:
    """(today, trade ideas, alternatives).

    Today carries every lineup/injury fix, the top two pickups and the single
    best trade idea -- nothing that competes with itself for the same player.
    """
    real = [a for a in queue if a.kind != "info"]
    trades = [a for a in real if a.kind in ("trade", "plan")
              and not a.title.startswith("Alternative")]
    alts = [a for a in real if a.kind == "trade" and a.title.startswith("Alternative")]
    today = [a for a in real if a.kind in ("lineup", "injury")]
    today += [a for a in real if a.kind == "waiver"][:2]
    if trades:
        today.append(max(trades, key=lambda a: a.gain))
    today.sort(key=lambda a: (-a.urgency, -a.gain))
    return today, trades, alts


# ------------------------------------------------------------- pieces


def row(p: dict | None, name: str, value: str = "", sub: str = "") -> str:
    p = p or {}
    meta = sub or esc(p.get("nfl_team") or "")
    val = f'<span class="pv">{value}</span>' if value else ""
    return (f'<li class="pr">{pos_html(p.get("position") or "")}'
            f'<span class="pn"><b>{esc(name)}</b><small>{meta}</small></span>'
            f'{flag_html(p)}{val}</li>')


def swap(out_label: str, out_rows: str, in_label: str, in_rows: str) -> str:
    return (f'<div class="swap">'
            f'<div class="side out"><span class="sl">{out_label}</span><ul>{out_rows}</ul></div>'
            f'<div class="side in"><span class="sl">{in_label}</span><ul>{in_rows}</ul></div>'
            f'</div>')


def wk(p: dict | None) -> str:
    if not p:
        return ""
    f = flag(p)
    if f and f[1] == "bad":
        return "0"
    return f"{week_pts(p):.1f}"


def opp_sub(p: dict | None) -> str:
    if not p:
        return ""
    opp = p.get("opponent")
    team = esc(p.get("nfl_team") or "")
    if opp == "BYE":
        return f"{team} · bye week"
    return f"{team} · vs {esc(opp)}" if opp else team


def bubble(text: str) -> str:
    return (f'<div class="chat"><p class="bub">{esc(text)}</p>'
            f'<button class="ghost" data-copy>Copy</button></div>')


TAGS = {"lineup": ("Lineup", "t-lineup"), "injury": ("Injury", "t-injury"),
        "waiver": ("Pickup", "t-pickup"), "trade": ("Trade", "t-trade"),
        "plan": ("2-step trade", "t-plan")}

OPEN_SPOT = '<li class="pr none">Open spot</li>'


def _rows(ps: list[dict], people: dict) -> str:
    return "".join(row(people.get(p["name"]) or p, p["name"]) for p in ps)


def card(a: Action, lg: str, people: dict) -> str:
    get = people.get
    tag, tcls = TAGS.get(a.kind, ("Move", ""))
    title, body, gain_n, gain_l, extra = a.title, "", "", "", ""

    if a.kind == "lineup" and a.title.startswith("Pick up"):
        tag, tcls = "Pickup", "t-pickup"
        add, drop = (a.players + [None])[:2]
        title = f"Pick up {add}"
        body = swap("Drop", row(get(drop), drop) if drop else OPEN_SPOT,
                    "Add", row(get(add), add, wk(get(add)), opp_sub(get(add))))
        gain_n, gain_l = f"+{a.gain:.1f}", "pts this week"
    elif a.kind == "lineup":
        start, sit = a.players[:2]
        title = f"Start {start}"
        body = swap("Bench", row(get(sit), sit, wk(get(sit)), opp_sub(get(sit))),
                    "Start", row(get(start), start, wk(get(start)), opp_sub(get(start))))
        gain_n, gain_l = f"+{a.gain:.1f}", "pts this week"
    elif a.kind == "injury":
        n = a.players[0]
        title = f"Bench {n}"
        body = (f'<ul class="solo">{row(get(n), n)}</ul>'
                '<p class="note">Out this week. Start your best healthy player.</p>')
    elif a.kind == "waiver":
        add, drop = (a.players + [None])[:2]
        title = f"Add {add}"
        body = swap("Drop", row(get(drop), drop) if drop else OPEN_SPOT,
                    "Add", row(get(add), add))
        gain_n, gain_l = f"+{a.gain:.0f}", "season pts"
    elif a.kind == "trade" and a.trade:
        t = a.trade
        title = "Get " + " + ".join(p["name"] for p in t["receive"])
        body = (f'<p class="who">with <b>{esc(t["partner_name"])}</b></p>'
                + swap("You give", _rows(t["send"], people),
                       "You get", _rows(t["receive"], people)))
        gain_n, gain_l = f"+{t['my_gain']:.0f}", "your lineup"
        extra = f'<span class="them">them +{t["their_gain"]:.0f}</span>'
    elif a.kind == "plan" and a.trade:
        c = a.trade
        notes = c.get("step_notes") or ["", ""]
        title = "Get " + " + ".join(p["name"] for p in c["step2"]["receive"])
        steps = ""
        for i, (st, note) in enumerate(((c["step1"], notes[0]), (c["step2"], notes[1])), 1):
            nline = f'<p class="note">{esc(note)}</p>' if note else ""
            steps += (
                f'<li class="step"><span class="sn">{i}</span><div class="sb">'
                f'<p class="who">with <b>{esc(st["partner_name"])}</b></p>'
                + swap("Give", _rows(st["send"], people), "Get", _rows(st["receive"], people))
                + nline + "</div></li>")
        body = f'<ol class="steps">{steps}</ol>'
        gain_n, gain_l = f"+{c['total_gain']:.0f}", "your lineup"

    chat = bubble(a.draft_message) if a.draft_message else ""
    gain = (f'<div class="gain"><b>{gain_n}</b><span>{gain_l}</span>{extra}</div>'
            if gain_n else '<div class="gain"></div>')
    key = esc(f"{lg}|{a.kind}|{a.title}")
    urgent = " urgent" if a.urgency >= 2 else ""
    return f"""
<article class="card{urgent}" data-key="{key}">
  <header class="ch"><span class="kt {tcls}">{tag}</span><span class="dl">{esc(a.deadline)}</span></header>
  <h3>{esc(title)}</h3>
  <div class="cb">{body}{chat}</div>
  <footer class="cf">{gain}<button class="done" data-done aria-pressed="false"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 10.5l3.2 3L15 6.5"/></svg>Done</button></footer>
</article>"""


# ------------------------------------------------------------- views


def started(me: dict) -> list[dict]:
    return [p for p in me["players"] if (p.get("current_slot") or "BE") not in ("BE", "IR")]


def view_today(a: dict, key: str, today: list[Action], n_trades: int) -> str:
    st, me = a["state"], a["me"]
    if not a["drafted"]:
        return ('<div class="view" data-view="today"><section class="hero">'
                '<p class="big ok">Not drafted</p><p class="sub">This fills in after the draft.</p>'
                '</section></div>')
    n = len(today)
    pts = sum(week_pts(p) for p in started(me))
    rec = ""
    if me.get("wins") is not None:
        ties = f'-{me["ties"]}' if me.get("ties") else ""
        rec = (f'<div class="stat"><b>{me["wins"]}-{me["losses"]}{ties}</b>'
               f'<span>record</span></div>')
    po = st.get("playoff_teams") or 0
    total = len(st["teams"])
    cells = "".join(
        f'<i class="{"me" if t.get("is_me") else ""}{" in" if i < po else ""}"></i>'
        for i, t in enumerate(st["teams"])
    )
    ladder = ""
    if po:
        ladder = (f'<div class="ladder" aria-hidden="true">{cells}</div>'
                  f'<p class="lcap"><span>Roster strength: <b>{ordinal(me["_rank"])}</b> of {total}</span>'
                  f'<span>Top {po} make playoffs</span></p>')
    word = "move" if n == 1 else "moves"
    head = (f'<p class="big">{n}<span>{word}</span></p>' if n
            else '<p class="big ok">All set</p>')
    cards = "".join(card(x, key, a["people"]) for x in today)
    more = (f'<button class="more" data-goto="trades">All trade ideas<span>{n_trades}</span></button>'
            if n_trades > 1 else "")
    empty = ('<p class="calm">Lineup is set, nobody on waivers beats your guys, '
             'and no trade helps both sides. Check back later.</p>') if not n else ""
    return f"""
<div class="view" data-view="today">
  <section class="hero">
    <div class="hrow">{head}
      <div class="stats"><div class="stat"><b>{pts:.0f}</b><span>proj this week</span></div>{rec}</div>
    </div>
    {ladder}
  </section>
  {empty}<div class="cards">{cards}</div>{more}
</div>"""


def view_lineup(a: dict, news: dict) -> str:
    if not a["drafted"]:
        return '<div class="view" data-view="lineup" hidden></div>'
    me, st = a["me"], a["state"]
    week = st.get("current_week")

    moves_in, moves_out = set(), set()
    for x in a["queue"]:
        if x.kind == "lineup" and x.title.startswith("Start"):
            moves_in.add(x.players[0])
            moves_out.add(x.players[1])
        elif x.kind == "injury":
            moves_out.update(x.players)

    order = {s: i for i, s in enumerate(SLOT_ORDER)}
    starters = sorted(started(me), key=lambda p: (order.get(p["current_slot"], 9), -week_pts(p)))
    bench = sorted((p for p in me["players"] if (p.get("current_slot") or "BE") == "BE"),
                   key=lambda p: -week_pts(p))
    ir = [p for p in me["players"] if p.get("current_slot") == "IR"]
    top = max([week_pts(p) for p in me["players"]] + [1])

    def line(p, slot):
        v = week_pts(p)
        f = flag(p)
        dead = bool(f and f[1] == "bad")
        mark = ""
        if p["name"] in moves_out:
            mark = '<span class="mv sit">Sit</span>'
        elif p["name"] in moves_in:
            mark = '<span class="mv go">Start</span>'
        bar = max(0.0, min(1.0, v / top))
        shown = "0" if dead else f"{v:.1f}"
        dcls = " dead" if dead else ""
        return (f'<li class="lr{dcls}">'
                f'<span class="slot">{esc(slot)}</span>'
                f'<span class="pn"><b>{esc(p["name"])}</b><small>{opp_sub(p)}</small></span>'
                f'{flag_html(p)}{mark}'
                f'<span class="pts"><b>{shown}</b><i style="--w:{bar:.2f}"></i></span></li>')

    srows = "".join(line(p, SLOT_LABEL.get(p["current_slot"], p["current_slot"])) for p in starters)
    brows = "".join(line(p, "BN") for p in bench)
    irows = "".join(line(p, "IR") for p in ir)
    total = sum(week_pts(p) for p in starters)

    mine = {str(p["espn_id"]): p["name"] for p in me["players"] if p.get("espn_id")}
    items = ""
    seen = 0
    for item in news.get("items", []):
        names = [mine[str(i)] for i in item.get("espn_athlete_ids", []) if str(i) in mine]
        if not names or seen >= 5:
            continue
        seen += 1
        when = (item.get("published") or "")[:10]
        link = esc(item.get("url") or "")
        head = esc(item["headline"])
        h = f'<a href="{link}" target="_blank" rel="noopener">{head}</a>' if link else head
        who = esc(", ".join(dict.fromkeys(names)))
        items += (f'<li><span class="nw">{who}</span><p>{h}</p>'
                  f'<small>{esc(when)}</small></li>')
    news_block = ""
    if items:
        news_block = (f'<section class="block"><h2>Your guys in the news</h2>'
                      f'<ul class="news">{items}</ul></section>')

    return f"""
<div class="view" data-view="lineup" hidden>
  <section class="block">
    <div class="bh"><h2>Week {esc(week)} starters</h2><span class="tot"><b>{total:.1f}</b> proj</span></div>
    <ul class="lineup">{srows}</ul>
  </section>
  <section class="block">
    <h2>Bench</h2>
    <ul class="lineup bench">{brows}{irows}</ul>
  </section>
  {news_block}
</div>"""


def view_trades(a: dict, key: str, trades: list[Action], alts: list[Action]) -> str:
    if not a["drafted"]:
        return '<div class="view" data-view="trades" hidden></div>'
    cards = "".join(card(x, key, a["people"]) for x in sorted(trades, key=lambda x: -x.gain))
    if not cards:
        cards = '<p class="calm">No trade helps both sides right now.</p>'
    alt_rows = ""
    for x in alts:
        t = x.trade
        give = esc(" + ".join(p["name"] for p in t["send"]))
        getn = esc(" + ".join(p["name"] for p in t["receive"]))
        alt_rows += (f'<li><span class="ag">{give}<i>for</i>{getn}</span>'
                     f'<small>{esc(t["partner_name"])}</small><b>+{t["my_gain"]:.0f}</b></li>')
    alt_block = ""
    if alt_rows:
        alt_block = (f'<section class="block"><h2>Other versions</h2>'
                     f'<ul class="alts">{alt_rows}</ul></section>')
    return f"""
<div class="view" data-view="trades" hidden>
  <div class="cards">{cards}</div>
  {alt_block}
  <section class="block check" data-eval="{key}">
    <h2>Got an offer?</h2>
    <label class="sel"><span>From</span><select id="ev-team-{key}"></select></label>
    <p class="eh">They give you</p><div class="chips" data-side="get"></div>
    <p class="eh">You give them</p><div class="chips" data-side="send"></div>
    <div class="verdict" aria-live="polite"><p class="vw">Tap players on both sides</p></div>
  </section>
</div>"""


# ------------------------------------------------------------- shell


def client_data(analyses: dict[str, dict]) -> str:
    """The slim JSON the offer checker needs: rosters + slots per league."""
    payload = {}
    for key, a in analyses.items():
        if not a["drafted"]:
            continue
        st = a["state"]
        payload[key] = {
            "slots": st["roster_slots"],
            "teams": [
                {
                    "id": t["team_id"], "name": t["name"].strip(), "me": bool(t.get("is_me")),
                    "players": [
                        {"id": p["player_id"], "n": p["name"], "pos": p["position"],
                         "pr": p.get("projected"), "inj": status_of(p)}
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
    switch, blocks = [], []
    week = None
    for key in ("L1", "L2"):
        state = load(f"state_{key}.json")
        if not state:
            continue
        a = analyse(state, news["items"], decisions)
        analyses[key] = a
        week = week or state.get("current_week")
        today, trades, alts = split_queue(a["queue"])
        first = not switch
        n = len(today)
        count = f'<i class="cnt">{n}</i>' if n else '<i class="cnt ok">✓</i>'
        pressed = "true" if first else "false"
        switch.append(
            f'<button data-lg="{key}" aria-pressed="{pressed}">'
            f'<span class="ln">{esc(state["name"])}</span>'
            f'<span class="ls">{esc(state.get("team_count", ""))} teams</span>{count}</button>'
        )
        hidden = "" if first else " hidden"
        blocks.append(f"""
<section class="lg" data-lg="{key}"{hidden}>
  {view_today(a, key, today, len(trades))}
  {view_lineup(a, news)}
  {view_trades(a, key, trades, alts)}
</section>""")

    now = datetime.now(timezone.utc)
    return (PAGE
            .replace("{{SWITCH}}", "".join(switch))
            .replace("{{BLOCKS}}", "".join(blocks))
            .replace("{{DATA}}", client_data(analyses))
            .replace("{{WEEK}}", esc(week or ""))
            .replace("{{ISO}}", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            .replace("{{STAMP}}", now.strftime("%b %-d, %H:%M UTC")))


PAGE = r"""<title>Fantasy Command Center</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Figtree:wght@400;500;600;700;800&display=swap">
<style>
/* One narrow app column: sticky league switch on top, three tabs on the bottom. */
:root{
  --bg:#EDF2F7; --card:#FFFFFF; --soft:#F3F6FA; --line:#DDE5EE;
  --fg:#0E1726; --muted:#5F6C7D;
  --sky:#1E8FFF; --sky-soft:#DDEEFF; --sky-ink:#0B5CB5; --sky-line:#B9DBFF;
  --bub:#1E8FFF; --on-bub:#FFFFFF;
  --good:#11904A; --good-soft:#DCF4E5;
  --bad:#D63A2C; --bad-soft:#FCE2DF;
  --warn:#A86C00; --warn-soft:#FFEFCC;
  --qb:#B197FC; --rb:#3FD8C2; --wr:#FFC53D; --te:#FF8FA3; --k:#B8C2CF; --on-pos:#0B1220;
  --num:'Barlow Condensed','Arial Narrow',system-ui,sans-serif;
  --ui:Figtree,system-ui,-apple-system,'Segoe UI',sans-serif;
  --r:18px;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0A1019; --card:#131C28; --soft:#1A2431; --line:#243143;
  --fg:#EAF0F7; --muted:#8E9BAE;
  --sky:#5BB7FF; --sky-soft:#13314F; --sky-ink:#A6D7FF; --sky-line:#1F4C78;
  --bub:#2580E0; --on-bub:#FFFFFF;
  --good:#3FD68D; --good-soft:#0F3324;
  --bad:#FF6B5E; --bad-soft:#3B1816;
  --warn:#FFC24D; --warn-soft:#3A2A0C;
  color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0A1019; --card:#131C28; --soft:#1A2431; --line:#243143;
  --fg:#EAF0F7; --muted:#8E9BAE;
  --sky:#5BB7FF; --sky-soft:#13314F; --sky-ink:#A6D7FF; --sky-line:#1F4C78;
  --bub:#2580E0; --on-bub:#FFFFFF;
  --good:#3FD68D; --good-soft:#0F3324;
  --bad:#FF6B5E; --bad-soft:#3B1816;
  --warn:#FFC24D; --warn-soft:#3A2A0C;
  color-scheme:dark}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:500 16px/1.4 var(--ui);-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:30;background:var(--bg)}
button{font:inherit;color:inherit}
a{color:inherit}
b{font-weight:700}
small{font-size:13px;color:var(--muted);font-weight:500}
:focus-visible{outline:3px solid var(--sky);outline-offset:2px;border-radius:10px}

.app{max-width:560px;margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));
  padding-block:0 calc(96px + env(safe-area-inset-bottom,0px))}

/* top: brand + league switch */
.top{position:sticky;top:env(safe-area-inset-top,0px);z-index:20;background:var(--bg);padding-block:14px 12px;display:grid;gap:12px}
.brand{display:flex;align-items:center;justify-content:space-between;gap:12px}
.mark{display:flex;align-items:center;gap:10px;font:800 19px/1 var(--ui);letter-spacing:-.01em}
.mark i{display:grid;place-items:center;width:38px;height:34px;border-radius:11px;background:var(--sky);
  color:var(--on-bub);font:700 17px/1 var(--num);font-style:normal;letter-spacing:.02em}
.upd{font-size:13px;color:var(--muted);text-align:right;line-height:1.35}
.upd b{color:var(--fg);font-weight:700}
.switch{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;background:var(--card);border:1px solid var(--line);border-radius:16px}
.switch button{appearance:none;border:0;background:transparent;cursor:pointer;border-radius:12px;padding:8px 10px 8px 12px;
  display:grid;grid-template-columns:1fr auto;text-align:left;column-gap:8px;align-items:center}
.switch .ln{font-weight:700;font-size:15px}
.switch .ls{grid-row:2;font-size:12px;color:var(--muted)}
.switch .cnt{grid-row:1/3;grid-column:2;font:700 15px/1 var(--num);font-style:normal;min-width:26px;height:26px;
  border-radius:99px;display:grid;place-items:center;background:var(--bad);color:var(--on-bub);padding:0 7px}
.switch .cnt.ok{background:var(--good-soft);color:var(--good)}
.switch button[aria-pressed="true"]{background:var(--sky-soft)}
.switch button[aria-pressed="true"] .ln{color:var(--sky-ink)}

/* hero */
.hero{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px 18px 16px;margin-block:4px 14px;display:grid;gap:14px}
.hrow{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.big{margin:0;font:700 66px/.82 var(--num);display:flex;align-items:baseline;gap:8px}
.big span{font:700 20px/1 var(--ui);color:var(--muted)}
.big.ok{color:var(--good);font-size:52px}
.sub{margin:0;color:var(--muted)}
.stats{display:flex;gap:20px}
.stat{display:grid;text-align:right}
.stat b{font:700 30px/1 var(--num);font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
.ladder{display:flex;gap:4px;align-items:center}
.ladder i{flex:1;height:12px;border-radius:4px;background:var(--soft);border:1px solid var(--line)}
.ladder i.in{background:var(--sky-soft);border-color:var(--sky-line)}
.ladder i.me{background:var(--sky);border-color:var(--sky);height:20px}
.lcap{margin:-4px 0 0;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
.lcap b{color:var(--fg)}
.calm{margin:0 0 14px;color:var(--muted);text-wrap:pretty}

/* move cards */
.cards{display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px 12px;display:grid;gap:10px}
.card.urgent{border-color:var(--bad);box-shadow:inset 0 0 0 1px var(--bad)}
.ch{display:flex;align-items:center;justify-content:space-between;gap:10px}
.kt{font:700 12px/1 var(--ui);letter-spacing:.06em;text-transform:uppercase;padding:5px 9px;border-radius:8px;background:var(--soft);color:var(--muted)}
.t-lineup{background:var(--sky-soft);color:var(--sky-ink)}
.t-pickup{background:var(--good-soft);color:var(--good)}
.t-injury{background:var(--bad-soft);color:var(--bad)}
.t-trade,.t-plan{background:var(--warn-soft);color:var(--warn)}
.dl{font-size:13px;color:var(--muted)}
.urgent .dl{color:var(--bad);font-weight:700}
.card h3{margin:0;font:800 21px/1.15 var(--ui);letter-spacing:-.01em;text-wrap:balance}
.cb{display:grid;gap:10px}
.who{margin:0;font-size:14px;color:var(--muted)}
.who b{color:var(--fg)}
.note{margin:0;font-size:14px;color:var(--muted)}

.swap{display:grid;grid-template-columns:minmax(0,1fr);gap:6px}
@media (min-width:540px){.swap{grid-template-columns:repeat(2,minmax(0,1fr))}}
.side{border-radius:14px;padding:9px 12px 5px;min-width:0}
.side.out{background:var(--soft)}
.side.in{background:var(--sky-soft)}
.sl{display:block;font:700 11px/1 var(--ui);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.side.in .sl{color:var(--sky-ink)}
.side ul,.solo{list-style:none;margin:0;padding:0;display:grid}
.pr{display:flex;align-items:center;gap:8px;padding-block:5px;min-width:0}
.pr+.pr{border-top:1px solid var(--line)}
.side.in .pr+.pr{border-top-color:var(--sky-line)}
.pr.none{color:var(--muted);font-size:14px}
.pn{display:grid;min-width:0;flex:1}
.pn b{font-size:15px;line-height:1.2;overflow-wrap:anywhere}
.pn small{font-size:12px;line-height:1.3}
.pv{font:700 20px/1 var(--num);font-variant-numeric:tabular-nums}
.pos{flex:none;width:34px;text-align:center;font:700 12px/1 var(--num);letter-spacing:.04em;padding:5px 0;border-radius:7px;color:var(--on-pos);background:var(--k)}
.pos-qb{background:var(--qb)} .pos-rb{background:var(--rb)} .pos-wr{background:var(--wr)} .pos-te{background:var(--te)}
.tag{flex:none;font:700 11px/1 var(--ui);letter-spacing:.04em;padding:4px 6px;border-radius:6px}
.tag.bad{background:var(--bad-soft);color:var(--bad)}
.tag.warn{background:var(--warn-soft);color:var(--warn)}

.steps{list-style:none;margin:0;padding:0;display:grid;gap:14px}
.step{display:grid;grid-template-columns:28px 1fr;gap:10px}
.sn{width:28px;height:28px;border-radius:99px;display:grid;place-items:center;background:var(--fg);color:var(--bg);font:700 16px/1 var(--num)}
.sb{display:grid;gap:8px;min-width:0}

.chat{display:flex;align-items:flex-end;gap:8px}
.bub{margin:0;flex:1;min-width:0;background:var(--bub);color:var(--on-bub);padding:10px 13px;border-radius:18px 18px 4px 18px;font-size:15px;line-height:1.35}
.ghost{appearance:none;cursor:pointer;border:1px solid var(--line);background:var(--card);border-radius:10px;padding:7px 11px;font-weight:700;font-size:13px}

.cf{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:10px;border-top:1px solid var(--line)}
.gain{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.gain b{font:700 30px/1 var(--num);color:var(--good)}
.gain span{font-size:13px;color:var(--muted)}
.gain .them{margin-left:6px;padding:3px 8px;border-radius:99px;background:var(--soft);font-weight:600}
.done{appearance:none;cursor:pointer;flex:none;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);border-radius:12px;padding:8px 12px;font-weight:700;font-size:14px}
.done svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.card.is-done{opacity:.6}
.card.is-done .cb{display:none}
.card.is-done h3{text-decoration:line-through;text-decoration-thickness:2px}
.card.is-done .done{background:var(--good);border-color:var(--good);color:var(--on-bub)}
.card.is-done.urgent{border-color:var(--line);box-shadow:none}

.more{appearance:none;cursor:pointer;margin-top:12px;width:100%;display:flex;justify-content:space-between;align-items:center;
  background:transparent;border:1px dashed var(--line);border-radius:14px;padding:13px 16px;font-weight:700}
.more span{font:700 18px/1 var(--num);color:var(--muted)}

/* lineup */
.block{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin-block:0 12px}
.block h2{margin:0 0 6px;font:800 17px/1.2 var(--ui)}
.bh{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.tot{font-size:13px;color:var(--muted)}
.tot b{font:700 22px/1 var(--num);color:var(--fg)}
.lineup{list-style:none;margin:0;padding:0}
.lr{display:flex;align-items:center;gap:10px;padding-block:9px;min-width:0}
.lr+.lr{border-top:1px solid var(--line)}
.slot{flex:none;width:36px;font:700 14px/1 var(--num);letter-spacing:.05em;color:var(--muted)}
.pts{flex:none;width:54px;display:grid;justify-items:end;gap:5px}
.pts b{font:700 21px/1 var(--num);font-variant-numeric:tabular-nums}
.pts i{display:block;height:4px;width:100%;border-radius:4px;background:linear-gradient(90deg,var(--sky) calc(var(--w)*100%),var(--soft) 0)}
.lr.dead .pts b{color:var(--bad)}
.bench .pn b{font-weight:600}
.mv{flex:none;font:700 11px/1 var(--ui);letter-spacing:.05em;text-transform:uppercase;padding:4px 7px;border-radius:6px;color:var(--on-bub)}
.mv.go{background:var(--good)}
.mv.sit{background:var(--bad)}
.news{list-style:none;margin:0;padding:0}
.news li{padding-block:10px;display:grid;gap:3px}
.news li+li{border-top:1px solid var(--line)}
.news p{margin:0;font-weight:600;line-height:1.3;text-wrap:pretty}
.news a{text-decoration:none}
.news a:hover{text-decoration:underline}
.nw{font-size:12px;font-weight:700;color:var(--sky-ink);text-transform:uppercase;letter-spacing:.05em}

/* trades */
.alts{list-style:none;margin:0;padding:0}
.alts li{display:grid;grid-template-columns:1fr auto;column-gap:10px;padding-block:9px}
.alts li+li{border-top:1px solid var(--line)}
.ag{font-weight:600;font-size:15px;min-width:0}
.ag i{font-style:normal;color:var(--muted);font-weight:500;margin-inline:6px}
.alts small{grid-row:2}
.alts b{grid-row:1/3;grid-column:2;align-self:center;font:700 22px/1 var(--num);color:var(--good)}
.check{display:grid;grid-template-columns:minmax(0,1fr);gap:8px}
.sel{display:flex;align-items:center;gap:10px;background:var(--soft);border-radius:12px;padding:4px 4px 4px 12px}
.sel span{font-size:13px;color:var(--muted);font-weight:600}
.sel select{flex:1;min-width:0;appearance:none;border:0;background:var(--card);color:var(--fg);border-radius:9px;padding:9px 12px;font:600 15px/1.2 var(--ui)}
.eh{margin:8px 0 0;font:700 11px/1 var(--ui);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{appearance:none;cursor:pointer;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
  border-radius:10px;padding:4px 10px 4px 4px;font-size:14px;font-weight:600;max-width:100%;text-align:left}
.chip .pos{width:30px;padding:4px 0;font-size:11px}
.chip[aria-pressed="true"]{background:var(--sky);border-color:var(--sky);color:var(--on-bub)}
.verdict{margin-top:8px;border-radius:14px;padding:14px;background:var(--soft);display:grid;gap:6px}
.vw{margin:0;font:800 20px/1.2 var(--ui)}
.verdict.good{background:var(--good-soft)} .verdict.good .vw{color:var(--good)}
.verdict.bad{background:var(--bad-soft)} .verdict.bad .vw{color:var(--bad)}
.verdict.meh{background:var(--warn-soft)} .verdict.meh .vw{color:var(--warn)}
.vn{display:flex;gap:16px;flex-wrap:wrap;font-size:14px;color:var(--muted)}
.vn b{font:700 22px/1 var(--num);color:var(--fg)}
.vx{margin:0;font-size:14px}

/* bottom tabs */
.tabs{position:fixed;left:0;right:0;bottom:0;z-index:25;background:var(--card);border-top:1px solid var(--line);
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));
  padding-block:6px calc(6px + env(safe-area-inset-bottom,0px))}
.tabs nav{max-width:560px;margin:0 auto;display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.tabs button{appearance:none;border:0;background:transparent;cursor:pointer;display:grid;justify-items:center;gap:3px;
  padding:7px 0;border-radius:12px;color:var(--muted);font-size:12px;font-weight:700}
.tabs svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tabs button[aria-pressed="true"]{color:var(--sky)}

@media (prefers-reduced-motion:no-preference){
  .card,.chip,.done,.switch button{transition:background-color .15s,opacity .2s,border-color .15s}
}
@media (max-width:380px){ .big{font-size:56px} }
</style>

<div class="app">
  <header class="top">
    <div class="brand">
      <span class="mark"><i>TyG</i>Fantasy</span>
      <span class="upd">Week {{WEEK}}<br><span data-ago="{{ISO}}">Updated {{STAMP}}</span></span>
    </div>
    <div class="switch" role="group" aria-label="League">{{SWITCH}}</div>
  </header>
  <main>{{BLOCKS}}</main>
</div>

<div class="tabs">
  <nav aria-label="Sections">
    <button data-tab="today" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.2 4L19 7"/></svg>Today</button>
    <button data-tab="lineup" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M4 12h16M12 4v16"/></svg>Lineup</button>
    <button data-tab="trades" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h12l-3-3M17 17H5l3 3"/></svg>Trades</button>
  </nav>
</div>

<script>
var LEAGUES = {{DATA}};
(function(){
  "use strict";
  function get(k, fb){ try{ var v = JSON.parse(localStorage.getItem(k)); return v == null ? fb : v; }catch(e){ return fb; } }
  function put(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }
  function $$(s, el){ return Array.prototype.slice.call((el||document).querySelectorAll(s)); }

  /* league + tab, remembered on this device */
  var lg = get("tyg-lg", null), tab = get("tyg-tab", "today");
  if(!document.querySelector('.lg[data-lg="'+lg+'"]')){ var f = document.querySelector(".lg"); lg = f ? f.dataset.lg : null; }
  if(["today","lineup","trades"].indexOf(tab) < 0) tab = "today";
  function show(){
    $$(".switch button").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.lg === lg)); });
    $$(".tabs button").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.tab === tab)); });
    $$(".lg").forEach(function(s){
      s.hidden = s.dataset.lg !== lg;
      $$(".view", s).forEach(function(v){ v.hidden = v.dataset.view !== tab; });
    });
  }
  $$(".switch button").forEach(function(b){ b.addEventListener("click", function(){
    lg = b.dataset.lg; put("tyg-lg", lg); show(); window.scrollTo(0, 0);
  }); });
  function goTab(t){ tab = t; put("tyg-tab", tab); show(); window.scrollTo(0, 0); }
  $$(".tabs button").forEach(function(b){ b.addEventListener("click", function(){ goTab(b.dataset.tab); }); });
  $$("[data-goto]").forEach(function(b){ b.addEventListener("click", function(){ goTab(b.dataset.goto); }); });

  /* done checkmarks */
  var done = get("tyg-done", {});
  $$(".card[data-key]").forEach(function(c){
    var k = c.dataset.key, btn = c.querySelector("[data-done]");
    function paint(){ var on = !!done[k]; c.classList.toggle("is-done", on); btn.setAttribute("aria-pressed", String(on)); }
    paint();
    btn.addEventListener("click", function(){ if(done[k]) delete done[k]; else done[k] = 1; put("tyg-done", done); paint(); });
  });

  /* copy the group-chat line */
  $$("[data-copy]").forEach(function(btn){
    btn.addEventListener("click", function(){
      var p = btn.parentNode.querySelector(".bub");
      function ok(){ btn.textContent = "Copied"; setTimeout(function(){ btn.textContent = "Copy"; }, 1600); }
      function pick(){ var r = document.createRange(); r.selectNodeContents(p); var s = getSelection(); s.removeAllRanges(); s.addRange(r); btn.textContent = "Selected"; }
      try{ navigator.clipboard.writeText(p.textContent).then(ok, pick); }catch(e){ pick(); }
    });
  });

  /* updated x ago */
  $$("[data-ago]").forEach(function(el){
    var t = Date.parse(el.dataset.ago); if(isNaN(t)) return;
    var m = Math.round((Date.now() - t) / 60000), txt;
    if(m < 2) txt = "just now"; else if(m < 60) txt = m + " min ago";
    else if(m < 48*60) txt = Math.round(m/60) + "h ago"; else txt = Math.round(m/1440) + " days ago";
    el.innerHTML = "Updated <b>" + txt + "</b>";
  });

  /* offer checker: same lineup math as the Python engine (season projections) */
  var FIXED = ["QB","RB","WR","TE","K","D/ST"], FLEXP = {RB:1,WR:1,TE:1};
  function value(players, slots){
    var pool = players.filter(function(p){ return p.pr != null; }).sort(function(a,b){ return b.pr - a.pr; });
    var used = {}, total = 0;
    FIXED.forEach(function(pos){
      var need = +(slots[pos]||0);
      for(var i=0;i<pool.length && need>0;i++){ var p = pool[i];
        if(used[p.id] || p.pos !== pos) continue; used[p.id]=1; total+=p.pr; need--; }
    });
    var flex = +(slots["RB/WR/TE"]||0);
    for(var j=0;j<pool.length && flex>0;j++){ var q = pool[j];
      if(used[q.id] || !FLEXP[q.pos]) continue; used[q.id]=1; total+=q.pr; flex--; }
    return total;
  }
  function swap(roster, out, inn){
    var gone = {}; out.forEach(function(p){ gone[p.id]=1; });
    return roster.filter(function(p){ return !gone[p.id]; }).concat(inn);
  }
  function raw(ps){ return ps.reduce(function(s,p){ return s + (p.pr||0); }, 0); }
  function escH(s){ return String(s).replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
  function sgn(x){ x = Math.round(x); return (x >= 0 ? "+" : "") + x; }
  var HURT = {OUT:1,INJURY_RESERVE:1,IR:1,DOUBTFUL:1,SUSPENSION:1};

  $$(".check[data-eval]").forEach(function(box){
    var L = LEAGUES[box.dataset.eval]; if(!L) return;
    var me = L.teams.filter(function(t){ return t.me; })[0];
    var others = L.teams.filter(function(t){ return !t.me; });
    var sel = box.querySelector("select"), getEl = box.querySelector('[data-side="get"]'),
        sendEl = box.querySelector('[data-side="send"]'), out = box.querySelector(".verdict");
    others.forEach(function(t){ var o = document.createElement("option"); o.value = t.id; o.textContent = t.name; sel.appendChild(o); });
    var picks = {get:{}, send:{}};
    function them(){ return others.filter(function(t){ return String(t.id) === sel.value; })[0] || others[0]; }
    function chips(el, team, side){
      el.innerHTML = team.players.filter(function(p){ return p.pr != null; })
        .sort(function(a,b){ return b.pr - a.pr; })
        .map(function(p){
          var k = p.pos === "D/ST" ? "dst" : p.pos.toLowerCase(), lab = p.pos === "D/ST" ? "DST" : p.pos;
          return '<button class="chip" data-id="'+escH(p.id)+'" aria-pressed="false">'+
            '<span class="pos pos-'+k+'">'+lab+'</span>'+escH(p.n)+'</button>';
        }).join("");
      $$(".chip", el).forEach(function(c){ c.addEventListener("click", function(){
        var id = c.dataset.id; if(picks[side][id]) delete picks[side][id]; else picks[side][id] = 1;
        c.setAttribute("aria-pressed", String(!!picks[side][id])); judge();
      }); });
    }
    function fill(){ picks = {get:{}, send:{}}; chips(getEl, them(), "get"); chips(sendEl, me, "send"); judge(); }
    function judge(){
      var t = them();
      var get = t.players.filter(function(p){ return picks.get[p.id]; });
      var send = me.players.filter(function(p){ return picks.send[p.id]; });
      if(!get.length || !send.length){ out.className = "verdict"; out.innerHTML = '<p class="vw">Tap players on both sides</p>'; return; }
      var mine0 = value(me.players, L.slots), th0 = value(t.players, L.slots);
      var mg = value(swap(me.players, send, get), L.slots) - mine0;
      var tg = value(swap(t.players, get, send), L.slots) - th0;
      var word, cls;
      if(mg >= 10){ word = "Take it"; cls = "good"; }
      else if(mg >= 3){ word = "Small win"; cls = "good"; }
      else if(mg > -3){ word = "Basically even"; cls = "meh"; }
      else { word = "Pass"; cls = "bad"; }
      var hurt = get.filter(function(p){ return HURT[p.inj]; });
      var h = '<p class="vw">'+word+'</p>'+
        '<div class="vn"><span>Your lineup <b>'+sgn(mg)+'</b></span><span>Theirs <b>'+sgn(tg)+'</b></span></div>';
      if(hurt.length) h += '<p class="vx">Heads up: '+escH(hurt[0].n)+' is hurt right now.</p>';
      if(mg < 3){
        var c = counter(t, mine0, th0);
        if(c) h += '<p class="vx"><b>Try instead:</b> '+escH(c.send.map(function(p){ return p.n; }).join(" + "))+
          ' for '+escH(c.get.n)+' (you '+sgn(c.mg)+', them '+sgn(c.tg)+')</p>';
      }
      out.className = "verdict " + cls; out.innerHTML = h;
    }
    /* best counter with this manager: helps both lineups and looks fair on paper */
    function counter(t, mine0, th0){
      var mine = me.players.filter(function(p){ return p.pr != null; }), packs = [], best = null;
      mine.forEach(function(p){ packs.push([p]); });
      for(var i=0;i<mine.length;i++) for(var j=i+1;j<mine.length;j++) packs.push([mine[i], mine[j]]);
      t.players.forEach(function(g){
        if(g.pr == null || HURT[g.inj]) return;
        packs.forEach(function(pk){
          if(raw(pk) < 0.7 * g.pr) return;
          var mg = value(swap(me.players, pk, [g]), L.slots) - mine0;
          var tg = value(swap(t.players, [g], pk), L.slots) - th0;
          if(mg >= 5 && tg >= 12 && (!best || mg + tg > best.mg + best.tg)) best = {send: pk, get: g, mg: mg, tg: tg};
        });
      });
      return best;
    }
    sel.addEventListener("change", fill);
    fill();
  });

  show();
})();
</script>
"""


def main() -> int:
    OUT.write_text(build())
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
