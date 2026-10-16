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
    return f'<span class="flg {f[1]}">{esc(f[0])}</span>' if f else ""


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


def opp_sub(p: dict | None) -> str:
    if not p:
        return ""
    opp = p.get("opponent")
    team = esc(p.get("nfl_team") or "")
    if opp == "BYE":
        return f"{team} · bye"
    return f"{team} vs {esc(opp)}" if opp else team


def wk(p: dict | None) -> str:
    if not p:
        return ""
    f = flag(p)
    return "0" if f and f[1] == "bad" else f"{week_pts(p):.1f}"


def mv(sign: str, p: dict | None, name: str, value: str = "", sub: str = "") -> str:
    """One player line in a move: minus = leaves your lineup, plus = joins it."""
    p = p or {}
    meta = sub or esc(p.get("nfl_team") or "")
    val = f'<span class="val">{value}</span>' if value else ""
    glyph = "+" if sign == "in" else "&minus;"
    return (f'<li class="mv {sign}"><span class="sg">{glyph}</span>{pos_html(p.get("position") or "")}'
            f'<span class="nm"><b>{esc(name)}</b><small>{meta}</small></span>'
            f'{flag_html(p)}{val}</li>')


def mvs(sign: str, ps: list[dict], people: dict) -> str:
    return "".join(mv(sign, people.get(p["name"]) or p, p["name"]) for p in ps)


TAGS = {"lineup": ("Lineup", "k-line"), "injury": ("Injury", "k-hurt"),
        "waiver": ("Pickup", "k-add"), "trade": ("Trade", "k-trade"),
        "plan": ("2-step", "k-trade")}

CHECK = ('<button class="done" data-done aria-pressed="false" aria-label="Mark done">'
         '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12.5l4 4L18 8"/></svg></button>')


def card(a: Action, lg: str, people: dict, i: int) -> str:
    get = people.get
    tag, kcls = TAGS.get(a.kind, ("Move", ""))
    title, rows, gain, unit, foot = a.title, "", "", "", ""

    if a.kind == "lineup" and a.title.startswith("Pick up"):
        tag, kcls = "Pickup", "k-add"
        add, drop = (a.players + [None])[:2]
        title = f"Pick up {add}"
        rows = mv("in", get(add), add, wk(get(add)), opp_sub(get(add)))
        if drop:
            rows += mv("out", get(drop), drop)
        gain, unit = f"+{a.gain:.1f}", "this week"
    elif a.kind == "lineup":
        start, sit = a.players[:2]
        title = f"Start {start}"
        rows = (mv("in", get(start), start, wk(get(start)), opp_sub(get(start)))
                + mv("out", get(sit), sit, wk(get(sit)), opp_sub(get(sit))))
        gain, unit = f"+{a.gain:.1f}", "this week"
    elif a.kind == "injury":
        n = a.players[0]
        title = f"Bench {n}"
        rows = mv("out", get(n), n)
    elif a.kind == "waiver":
        add, drop = (a.players + [None])[:2]
        title = f"Add {add}"
        rows = mv("in", get(add), add) + (mv("out", get(drop), drop) if drop else "")
        gain, unit = f"+{a.gain:.0f}", "season"
    elif a.kind == "trade" and a.trade:
        t = a.trade
        title = "Get " + " + ".join(p["name"] for p in t["receive"])
        rows = mvs("in", t["receive"], people) + mvs("out", t["send"], people)
        gain, unit = f"+{t['my_gain']:.0f}", "season"
        foot = (f'<p class="with">w/ <b>{esc(t["partner_name"].strip())}</b>'
                f'<span>them +{t["their_gain"]:.0f}</span></p>')
    elif a.kind == "plan" and a.trade:
        c = a.trade
        title = "Get " + " + ".join(p["name"] for p in c["step2"]["receive"])
        for n, st in enumerate((c["step1"], c["step2"]), 1):
            rows += (f'<li class="stp"><span class="sn">{n}</span>'
                     f'<span>w/ <b>{esc(st["partner_name"].strip())}</b></span></li>'
                     + mvs("in", st["receive"], people) + mvs("out", st["send"], people))
        gain, unit = f"+{c['total_gain']:.0f}", "season"

    if a.draft_message:
        foot += (f'<button class="txt" data-copy="{esc(a.draft_message)}">'
                 '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14v9H10l-4 3v-3H5z"/></svg>'
                 'Copy text</button>')
    live = '<span class="live"><i></i>Now</span>' if a.urgency >= 2 else ""
    g = (f'<span class="gain"><b>{gain}</b><small>{unit}</small></span>' if gain else "")
    key = esc(f"{lg}|{a.kind}|{a.title}")
    foot_html = f'<div class="foot">{foot}</div>' if foot else ""
    return f"""
<article class="card {kcls}" data-key="{key}" style="--i:{i}">
  <div class="top-row"><span class="tag"><span>{tag}</span></span>{live}{CHECK}</div>
  <div class="head"><h3>{esc(title)}</h3>{g}</div>
  <div class="fold"><div><ul class="moves">{rows}</ul>{foot_html}</div></div>
</article>"""


# ------------------------------------------------------------- views


def started(me: dict) -> list[dict]:
    return [p for p in me["players"] if (p.get("current_slot") or "BE") not in ("BE", "IR")]


def view_today(a: dict, key: str, today: list[Action], n_trades: int) -> str:
    st, me = a["state"], a["me"]
    if not a["drafted"]:
        return ('<div class="view" data-view="today"><section class="bug">'
                '<p class="count">—</p><p class="cap">Not drafted yet</p></section></div>')
    n = len(today)
    pts = sum(week_pts(p) for p in started(me))
    po = st.get("playoff_teams") or 0
    total = len(st["teams"])
    cells = "".join(
        f'<i class="{"me" if t.get("is_me") else ""}{" po" if i < po else ""}" style="--j:{i}"></i>'
        for i, t in enumerate(st["teams"])
    )
    rec = ""
    if me.get("wins") is not None:
        rec = f'<div><b>{me["wins"]}-{me["losses"]}</b><small>record</small></div>'
    word = "move" if n == 1 else "moves"
    cards = "".join(card(x, key, a["people"], i) for i, x in enumerate(today))
    if not n:
        cards = '<p class="calm">Nothing to do. Your team is set.</p>'
    more = (f'<button class="more" data-goto="trades" style="--i:{n}">'
            f'<span>{n_trades} trade ideas</span><svg viewBox="0 0 24 24" aria-hidden="true">'
            '<path d="M9 6l6 6-6 6"/></svg></button>' if n_trades > 1 else "")
    return f"""
<div class="view" data-view="today">
  <section class="bug">
    <div class="bug-l"><p class="count" data-count="{n}">{n}</p><p class="cap">{word}</p></div>
    <div class="bug-r">
      <div><b>{pts:.0f}</b><small>proj pts</small></div>
      <div><b>{ordinal(me["_rank"])}</b><small>of {total}</small></div>{rec}
    </div>
    <div class="ladder" aria-label="Top {po} make playoffs">{cells}</div>
  </section>
  <div class="cards">{cards}</div>{more}
</div>"""


def view_lineup(a: dict) -> str:
    if not a["drafted"]:
        return '<div class="view" data-view="lineup" hidden></div>'
    me, st = a["me"], a["state"]

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

    def line(p, slot, i):
        v = week_pts(p)
        f = flag(p)
        dead = bool(f and f[1] == "bad")
        mark = ""
        if p["name"] in moves_out:
            mark = '<span class="arrow down" title="Sit">Sit</span>'
        elif p["name"] in moves_in:
            mark = '<span class="arrow up" title="Start">Start</span>'
        shown = "0" if dead else f"{v:.1f}"
        dcls = " dead" if dead else ""
        return (f'<li class="dr{dcls}" style="--i:{i};--w:{max(0.0, min(1.0, v / top)):.2f}">'
                f'<span class="slot">{esc(slot)}</span>'
                f'<span class="nm"><b>{esc(p["name"])}</b><small>{opp_sub(p)}</small></span>'
                f'{flag_html(p)}{mark}<span class="pts"><b>{shown}</b><i></i></span></li>')

    srows = "".join(line(p, SLOT_LABEL.get(p["current_slot"], p["current_slot"]), i)
                    for i, p in enumerate(starters))
    brows = "".join(line(p, "BN", i) for i, p in enumerate(bench))
    brows += "".join(line(p, "IR", len(bench) + i) for i, p in enumerate(ir))
    total = sum(week_pts(p) for p in starters)
    return f"""
<div class="view" data-view="lineup" hidden>
  <section class="sheet">
    <header class="sh"><h2>Week {esc(st.get("current_week"))}</h2><span><b>{total:.1f}</b> proj</span></header>
    <ul class="depth">{srows}</ul>
  </section>
  <section class="sheet">
    <header class="sh"><h2>Bench</h2></header>
    <ul class="depth bench">{brows}</ul>
  </section>
</div>"""


def view_trades(a: dict, key: str, trades: list[Action]) -> str:
    if not a["drafted"]:
        return '<div class="view" data-view="trades" hidden></div>'
    cards = "".join(card(x, key, a["people"], i)
                    for i, x in enumerate(sorted(trades, key=lambda x: -x.gain)))
    if not cards:
        cards = '<p class="calm">No trade helps both sides right now.</p>'
    return f"""
<div class="view" data-view="trades" hidden>
  <div class="cards">{cards}</div>
  <section class="sheet check" data-eval="{key}">
    <header class="sh"><h2>Got an offer?</h2></header>
    <select id="ev-team-{key}" aria-label="Who sent it"></select>
    <p class="lbl">They give</p><div class="chips" data-side="get"></div>
    <p class="lbl">You give</p><div class="chips" data-side="send"></div>
    <div class="verdict" aria-live="polite"><p class="vw">Pick players</p></div>
  </section>
</div>"""


def ticker(a: dict, news: dict, key: str, first: bool) -> str:
    """Bottom-line headlines about my own players, broadcast style."""
    if not a["drafted"]:
        return ""
    mine = {str(p["espn_id"]): p["name"] for p in a["me"]["players"] if p.get("espn_id")}
    items = []
    for item in news.get("items", []):
        names = [mine[str(i)] for i in item.get("espn_athlete_ids", []) if str(i) in mine]
        if names and len(items) < 6:
            who = esc(", ".join(dict.fromkeys(names)))
            items.append(f'<span class="ti"><b>{who}</b>{esc(item["headline"])}</span>')
    if not items:
        return ""
    run = "".join(items)
    length = sum(len(i) for i in items)
    hidden = "" if first else " hidden"
    return (f'<div class="ticker" data-lg="{key}"{hidden}><span class="tl">News</span>'
            f'<div class="tw"><div class="tt" style="--dur:{max(14, length // 9)}s">'
            f'{run}<span aria-hidden="true" class="dup">{run}</span></div></div></div>')


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
    switch, blocks, tickers = [], [], []
    week = None
    for key in ("L1", "L2"):
        state = load(f"state_{key}.json")
        if not state:
            continue
        a = analyse(state, news["items"], decisions)
        analyses[key] = a
        week = week or state.get("current_week")
        today, trades, _alts = split_queue(a["queue"])
        first = not switch
        n = len(today)
        badge = f'<i class="n">{n}</i>' if n else '<i class="n ok">✓</i>'
        switch.append(
            f'<button data-lg="{key}" aria-pressed="{"true" if first else "false"}">'
            f'<span>{esc(state["name"])}</span>{badge}</button>'
        )
        hidden = "" if first else " hidden"
        blocks.append(f"""
<section class="lg" data-lg="{key}"{hidden}>
  {view_today(a, key, today, len(trades))}
  {view_lineup(a)}
  {view_trades(a, key, trades)}
</section>""")
        tickers.append(ticker(a, news, key, first))

    now = datetime.now(timezone.utc)
    return (PAGE
            .replace("{{SWITCH}}", "".join(switch))
            .replace("{{BLOCKS}}", "".join(blocks))
            .replace("{{TICKERS}}", "".join(tickers))
            .replace("{{DATA}}", client_data(analyses))
            .replace("{{WEEK}}", esc(week or ""))
            .replace("{{ISO}}", now.strftime("%Y-%m-%dT%H:%M:%SZ")))


PAGE = r"""<title>Fantasy Command Center</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800;900&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@700;800&display=swap">
<style>
/* Broadcast package: score bug on top, lower-third move cards, bottom line + tabs. Single dark look by choice. */
:root{
  color-scheme:dark;
  --bg:#07101C; --panel:#0E1B2D; --panel2:#132741; --line:#1C3049; --line2:#284468;
  --fg:#F2F7FD; --muted:#8597B0; --dim:#4F6380;
  --sky:#56B9FF; --sky2:#B4E0FF; --ink:#03101F;
  --win:#43E595; --loss:#FF5E58; --gold:#FFC74D;
  --qb:#B59CFF; --rb:#43DCC6; --wr:#FFC74D; --te:#FF92A6; --k:#9DB0C8;
  --disp:'Big Shoulders Display','Saira Condensed','Arial Narrow',sans-serif;
  --ui:Archivo,system-ui,-apple-system,'Segoe UI',sans-serif;
  --ease:cubic-bezier(.2,.8,.2,1);
  --spring:cubic-bezier(.34,1.4,.5,1);
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:500 16px/1.35 var(--ui);-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:40;background:var(--bg)}
.field{position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(90% 50% at 50% -8%,rgba(86,185,255,.22),transparent 70%),
    repeating-linear-gradient(180deg,transparent 0 71px,rgba(180,224,255,.035) 71px 72px);
  -webkit-mask-image:linear-gradient(180deg,#000 0,#000 45%,transparent 85%);mask-image:linear-gradient(180deg,#000 0,#000 45%,transparent 85%)}
button{font:inherit;color:inherit}
b{font-weight:700}
small{color:var(--muted);font-weight:500}
:focus-visible{outline:2px solid var(--sky);outline-offset:3px;border-radius:10px}

.app{max-width:540px;margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));
  padding-block:0 calc(130px + env(safe-area-inset-bottom,0px))}

/* header */
.top{position:sticky;top:env(safe-area-inset-top,0px);z-index:30;padding-block:14px 12px;display:grid;gap:12px;
  background:linear-gradient(180deg,var(--bg) 75%,rgba(7,16,28,0));animation:drop .5s var(--ease) both}
.brand{display:flex;align-items:center;justify-content:space-between}
.logo{font:italic 900 30px/1 var(--disp);letter-spacing:.01em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
.logo i{width:10px;height:26px;background:var(--sky);transform:skewX(-18deg);border-radius:2px}
.meta{font:700 12px/1.3 var(--ui);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);text-align:right;font-stretch:90%}
.meta b{color:var(--fg)}
.switch{position:relative;display:grid;grid-template-columns:1fr 1fr;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:4px;isolation:isolate}
.switch .ind{position:absolute;z-index:-1;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);border-radius:10px;
  background:linear-gradient(135deg,var(--sky),#3A9BEA);box-shadow:0 6px 24px -6px rgba(86,185,255,.6);
  transform:translateX(calc(var(--x,0) * 100%));transition:transform .45s var(--spring)}
.switch button{appearance:none;border:0;background:none;cursor:pointer;padding:10px 12px;display:flex;align-items:center;justify-content:center;gap:8px;
  font:800 15px/1 var(--ui);font-stretch:110%;letter-spacing:.01em;color:var(--muted);transition:color .3s}
.switch button[aria-pressed="true"]{color:var(--ink)}
.switch .n{font:800 13px/1 var(--ui);font-style:normal;min-width:22px;height:22px;padding:0 6px;border-radius:99px;display:grid;place-items:center;background:var(--loss);color:#fff}
.switch .n.ok{background:rgba(67,229,149,.16);color:var(--win)}
.switch button[aria-pressed="true"] .n.ok{background:rgba(3,16,31,.18);color:var(--ink)}

/* score bug */
.bug{position:relative;overflow:hidden;margin-block:6px 18px;padding:18px 18px 16px;border-radius:20px;
  background:linear-gradient(160deg,var(--panel2),var(--panel) 60%);border:1px solid var(--line);
  display:grid;grid-template-columns:auto 1fr;gap:14px 18px;align-items:end}
.bug::after{content:"";position:absolute;inset:0;background:linear-gradient(105deg,transparent 40%,rgba(180,224,255,.09) 50%,transparent 60%);
  transform:translateX(-100%);animation:sheen 1.6s .35s var(--ease) both;pointer-events:none}
.bug-l{display:flex;align-items:baseline;gap:8px}
.count{margin:0;font:900 104px/.78 var(--disp);letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.cap{margin:0;font:800 15px/1 var(--ui);font-stretch:120%;letter-spacing:.14em;text-transform:uppercase;color:var(--sky)}
.bug-r{display:flex;justify-content:flex-end;gap:18px;padding-bottom:4px}
.bug-r div{display:grid;justify-items:end}
.bug-r b{font:800 34px/1 var(--disp);font-variant-numeric:tabular-nums}
.bug-r small{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:700}
.ladder{grid-column:1/-1;display:flex;gap:4px;align-items:end;height:22px}
.ladder i{flex:1;height:8px;border-radius:3px;background:var(--line);transform-origin:bottom;animation:rise .5s calc(.25s + var(--j)*35ms) var(--spring) both}
.ladder i.po{background:var(--line2)}
.ladder i.me{height:22px;background:var(--sky);box-shadow:0 0 16px rgba(86,185,255,.7)}

/* move cards: lower thirds */
.cards{display:grid;gap:12px}
.card{position:relative;border-radius:18px;padding:14px 14px 12px 16px;background:linear-gradient(170deg,var(--panel2),var(--panel) 70%);
  border:1px solid var(--line);display:grid;gap:10px;
  animation:wipe .6s calc(.15s + var(--i)*80ms) var(--ease) both;transition:opacity .35s,transform .35s var(--ease),border-color .3s}
@media (hover:hover){.card:hover{border-color:var(--line2);transform:translateY(-2px)}}
.top-row{display:flex;align-items:center;gap:8px}
.tag{display:inline-block;transform:skewX(-14deg);background:var(--line2);border-radius:4px;padding:5px 10px}
.tag span{display:block;transform:skewX(14deg);font:800 11px/1 var(--ui);font-stretch:115%;letter-spacing:.14em;text-transform:uppercase}
.k-line .tag{background:var(--sky);color:var(--ink)}
.k-add .tag{background:var(--win);color:var(--ink)}
.k-hurt .tag{background:var(--loss);color:#fff}
.k-trade .tag{background:var(--gold);color:var(--ink)}
.live{display:flex;align-items:center;gap:6px;font:800 11px/1 var(--ui);letter-spacing:.14em;text-transform:uppercase;color:var(--loss)}
.live i{width:7px;height:7px;border-radius:99px;background:var(--loss);animation:pulse 1.6s infinite}
.done{appearance:none;cursor:pointer;margin-left:auto;width:34px;height:34px;border-radius:99px;border:1.5px solid var(--line2);background:transparent;display:grid;place-items:center;
  transition:background .3s,border-color .3s,transform .2s var(--spring)}
.done:active{transform:scale(.88)}
.done svg{width:18px;height:18px;fill:none;stroke:var(--muted);stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:20;stroke-dashoffset:0}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px}
.head h3{margin:0;font:800 23px/1.08 var(--ui);font-stretch:108%;letter-spacing:-.01em;text-wrap:balance;min-width:0}
.gain{flex:none;display:grid;justify-items:end}
.gain b{font:900 40px/.85 var(--disp);color:var(--sky);font-variant-numeric:tabular-nums}
.gain small{font-size:10px;letter-spacing:.14em;text-transform:uppercase;font-weight:700;margin-top:3px}
.fold{display:grid;grid-template-rows:1fr;transition:grid-template-rows .45s var(--ease),opacity .3s}
.fold>div{overflow:hidden;min-height:0}
.moves{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.mv{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:10px;min-width:0}
.mv.in{background:rgba(86,185,255,.09)}
.sg{flex:none;width:18px;font:800 18px/1 var(--ui);text-align:center}
.mv.in .sg{color:var(--sky)}
.mv.out .sg{color:var(--loss)}
.mv.out .nm b{color:var(--muted)}
.nm{display:grid;min-width:0;flex:1}
.nm b{font-size:15.5px;line-height:1.2;overflow-wrap:anywhere}
.nm small{font-size:12px}
.val{font:800 22px/1 var(--disp);font-variant-numeric:tabular-nums}
.mv.out .val{color:var(--muted)}
.pos{flex:none;width:32px;text-align:center;font:800 11px/1 var(--ui);font-stretch:90%;letter-spacing:.04em;padding:5px 0;border-radius:6px;color:var(--ink);background:var(--k)}
.pos-qb{background:var(--qb)} .pos-rb{background:var(--rb)} .pos-wr{background:var(--wr)} .pos-te{background:var(--te)}
.flg{flex:none;padding:3px 6px;border-radius:5px;font:800 10.5px/1 var(--ui);letter-spacing:.06em}
.flg.bad{background:rgba(255,94,88,.16);color:var(--loss)}
.flg.warn{background:rgba(255,199,77,.14);color:var(--gold)}
.stp{display:flex;align-items:center;gap:10px;padding:8px 2px 4px;font-size:14px;color:var(--muted)}
.stp b{color:var(--fg)}
.sn{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;border:1.5px solid var(--sky);color:var(--sky);font:800 15px/1 var(--disp)}
.foot{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:10px;margin-top:8px;border-top:1px solid var(--line);flex-wrap:wrap}
.with{margin:0;font-size:13.5px;color:var(--muted);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.with b{color:var(--fg)}
.with span{font-weight:700;color:var(--win)}
.txt{appearance:none;cursor:pointer;margin-left:auto;display:flex;align-items:center;gap:7px;border:0;border-radius:99px;padding:8px 14px;
  background:var(--sky);color:var(--ink);font:800 13px/1 var(--ui);transition:transform .2s var(--spring)}
.txt:active{transform:scale(.94)}
.txt svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linejoin:round}

.card.is-done{opacity:.45}
.card.is-done .fold{grid-template-rows:0fr;opacity:0}
.card.is-done .gain{opacity:.5}
.card.is-done .done{background:var(--win);border-color:var(--win)}
.card.is-done .done svg{stroke:var(--ink);animation:draw .4s var(--ease) both}
.card.is-done h3{text-decoration:line-through;text-decoration-thickness:2px;text-decoration-color:var(--muted)}
.calm{margin:0;padding:22px;border-radius:18px;border:1px dashed var(--line2);text-align:center;color:var(--muted);font-weight:600}
.more{appearance:none;cursor:pointer;margin-top:12px;width:100%;display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-radius:14px;
  border:1px solid var(--line);background:transparent;font:800 15px/1 var(--ui);font-stretch:108%;animation:wipe .6s calc(.15s + var(--i)*80ms) var(--ease) both}
.more svg{width:20px;height:20px;fill:none;stroke:var(--sky);stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round;transition:transform .3s var(--spring)}
.more:hover svg{transform:translateX(4px)}

/* lineup sheet */
.sheet{background:linear-gradient(170deg,var(--panel2),var(--panel) 50%);border:1px solid var(--line);border-radius:18px;padding:12px 14px 6px;margin-block:6px 12px;
  animation:wipe .6s .1s var(--ease) both}
.sh{display:flex;justify-content:space-between;align-items:baseline;padding:2px 2px 6px}
.sh h2{margin:0;font:900 26px/1 var(--disp);text-transform:uppercase;letter-spacing:.02em}
.sh span{font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--muted)}
.sh span b{font:800 26px/1 var(--disp);color:var(--fg);letter-spacing:0}
.depth{list-style:none;margin:0;padding:0}
.dr{display:flex;align-items:center;gap:10px;padding-block:9px;min-width:0;border-top:1px solid var(--line);animation:slide .45s calc(.15s + var(--i)*35ms) var(--ease) both}
.slot{flex:none;width:34px;font:800 15px/1 var(--disp);letter-spacing:.06em;color:var(--sky)}
.bench .slot{color:var(--dim)}
.pts{flex:none;width:52px;display:grid;justify-items:end;gap:5px}
.pts b{font:800 22px/1 var(--disp);font-variant-numeric:tabular-nums}
.pts i{display:block;height:3px;width:100%;border-radius:3px;background:var(--line);position:relative;overflow:hidden}
.pts i::after{content:"";position:absolute;inset:0;background:var(--sky);transform-origin:left;transform:scaleX(var(--w));animation:grow .8s calc(.3s + var(--i)*35ms) var(--ease) both}
.dr.dead .pts b{color:var(--loss)}
.dr.dead .pts i::after{background:var(--loss)}
.bench .nm b{color:var(--muted);font-weight:600}
.arrow{flex:none;font:800 10.5px/1 var(--ui);letter-spacing:.1em;text-transform:uppercase;padding:4px 7px;border-radius:5px}
.arrow.up{background:var(--win);color:var(--ink)}
.arrow.down{background:var(--loss);color:#fff}

/* offer checker */
.check{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;padding-bottom:14px}
.check select{appearance:none;width:100%;border:1px solid var(--line2);background:var(--panel);color:var(--fg);border-radius:12px;padding:12px 14px;font:700 15px/1.2 var(--ui)}
.lbl{margin:6px 0 0;font:800 11px/1 var(--ui);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{appearance:none;cursor:pointer;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:4px 10px 4px 4px;
  font:600 13.5px/1.2 var(--ui);max-width:100%;text-align:left;transition:background .2s,border-color .2s,transform .2s var(--spring)}
.chip:active{transform:scale(.94)}
.chip .pos{width:28px;font-size:10px;padding:4px 0}
.chip[aria-pressed="true"]{background:var(--sky);border-color:var(--sky);color:var(--ink)}
.verdict{margin-top:8px;border-radius:14px;padding:14px 16px;background:var(--panel);border:1px solid var(--line);display:grid;gap:6px;transition:background .3s,border-color .3s}
.vw{margin:0;font:900 30px/1 var(--disp);text-transform:uppercase;letter-spacing:.01em;color:var(--muted)}
.verdict.good{border-color:rgba(67,229,149,.5);background:rgba(67,229,149,.08)} .verdict.good .vw{color:var(--win)}
.verdict.bad{border-color:rgba(255,94,88,.5);background:rgba(255,94,88,.08)} .verdict.bad .vw{color:var(--loss)}
.verdict.meh{border-color:rgba(255,199,77,.45);background:rgba(255,199,77,.07)} .verdict.meh .vw{color:var(--gold)}
.vn{display:flex;gap:18px;font-size:12px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;color:var(--muted)}
.vn b{font:800 24px/1 var(--disp);color:var(--fg);letter-spacing:0;margin-left:4px}
.vx{margin:0;font-size:14px;color:var(--muted)}
.vx b{color:var(--fg)}
.pop{animation:pop .35s var(--spring)}

/* bottom: news ticker + tabs */
.dock{position:fixed;left:0;right:0;bottom:0;z-index:30;background:rgba(9,19,33,.86);-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);
  border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom,0px)}
.ticker{display:flex;align-items:center;height:32px;border-bottom:1px solid var(--line);overflow:hidden;
  padding-left:max(0px,env(safe-area-inset-left,0px));padding-right:max(0px,env(safe-area-inset-right,0px))}
.tl{flex:none;height:100%;display:grid;place-items:center;padding:0 12px 0 max(16px,env(safe-area-inset-left,0px));background:var(--sky);color:var(--ink);
  font:900 14px/1 var(--disp);letter-spacing:.12em;text-transform:uppercase;clip-path:polygon(0 0,100% 0,calc(100% - 10px) 100%,0 100%);padding-right:20px}
.tw{flex:1;overflow:hidden;-webkit-mask-image:linear-gradient(90deg,transparent,#000 24px,#000 calc(100% - 24px),transparent);mask-image:linear-gradient(90deg,transparent,#000 24px,#000 calc(100% - 24px),transparent)}
.tt{display:inline-flex;white-space:nowrap;animation:tick var(--dur) linear infinite}
.ti{padding-inline:20px;font-size:13px;font-weight:600;color:var(--muted)}
.ti b{color:var(--sky2);text-transform:uppercase;font-size:12px;letter-spacing:.08em;margin-right:8px}
.tabs{position:relative;max-width:540px;margin:0 auto;display:grid;grid-template-columns:repeat(3,1fr);
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:6px}
.tabs .ind{position:absolute;top:0;height:3px;left:max(16px,env(safe-area-inset-left,0px));width:calc((100% - 2*max(16px,env(safe-area-inset-left,0px)))/3);
  display:flex;justify-content:center;transform:translateX(calc(var(--x,0)*100%));transition:transform .45s var(--spring)}
.tabs .ind::after{content:"";width:36px;height:3px;border-radius:0 0 3px 3px;background:var(--sky);box-shadow:0 0 14px var(--sky)}
.tabs button{appearance:none;border:0;background:none;cursor:pointer;display:grid;justify-items:center;gap:3px;padding:8px 0 4px;color:var(--dim);
  font:800 11px/1 var(--ui);letter-spacing:.12em;text-transform:uppercase;transition:color .3s}
.tabs svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;transition:transform .35s var(--spring)}
.tabs button[aria-pressed="true"]{color:var(--fg)}
.tabs button[aria-pressed="true"] svg{transform:translateY(-2px) scale(1.08);stroke:var(--sky)}

.toast{position:fixed;left:50%;bottom:calc(110px + env(safe-area-inset-bottom,0px));z-index:50;width:min(92vw,420px);
  background:var(--fg);color:var(--ink);border-radius:16px;padding:12px 14px;font-size:14px;line-height:1.35;box-shadow:0 18px 40px -12px rgba(0,0,0,.6);
  transform:translate(-50%,20px);opacity:0;pointer-events:none;transition:transform .4s var(--spring),opacity .25s}
.toast b{display:block;font:800 11px/1 var(--ui);letter-spacing:.14em;text-transform:uppercase;margin-bottom:5px;color:#1A6FC0}
.toast.on{transform:translate(-50%,0);opacity:1}

@keyframes drop{from{transform:translateY(-10px);opacity:0}}
@keyframes wipe{from{clip-path:inset(0 100% 0 0 round 18px);transform:translateX(-14px)}to{clip-path:inset(0 0 0 0 round 18px)}}
@keyframes slide{from{opacity:0;transform:translateX(-10px)}}
@keyframes rise{from{transform:scaleY(0)}}
@keyframes grow{from{transform:scaleX(0)}}
@keyframes sheen{to{transform:translateX(100%)}}
@keyframes pulse{50%{opacity:.25}}
@keyframes draw{from{stroke-dashoffset:20}}
@keyframes pop{0%{transform:scale(.96)}60%{transform:scale(1.02)}}
@keyframes tick{to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
  .tw{overflow-x:auto}
  .dup{display:none}
}
@media (max-width:370px){.count{font-size:88px}.bug-r{gap:12px}.bug-r b{font-size:28px}}
</style>

<div class="field" aria-hidden="true"></div>
<div class="app">
  <header class="top">
    <div class="brand">
      <span class="logo"><i></i>TyG</span>
      <span class="meta">Week {{WEEK}}<br><span data-ago="{{ISO}}">&nbsp;</span></span>
    </div>
    <div class="switch" role="group" aria-label="League"><span class="ind" aria-hidden="true"></span>{{SWITCH}}</div>
  </header>
  <main>{{BLOCKS}}</main>
</div>

<div class="dock">
  {{TICKERS}}
  <nav class="tabs" aria-label="Sections">
    <span class="ind" aria-hidden="true"></span>
    <button data-tab="today" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/></svg>Today</button>
    <button data-tab="lineup" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg>Lineup</button>
    <button data-tab="trades" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h12l-3-3M17 17H5l3 3"/></svg>Trades</button>
  </nav>
</div>
<div class="toast" role="status" aria-live="polite"><b>Copied</b><span></span></div>

<script>
var LEAGUES = {{DATA}};
(function(){
  "use strict";
  function get(k, fb){ try{ var v = JSON.parse(localStorage.getItem(k)); return v == null ? fb : v; }catch(e){ return fb; } }
  function put(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }
  function $$(s, el){ return Array.prototype.slice.call((el||document).querySelectorAll(s)); }
  var calm = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var TABS = ["today","lineup","trades"];

  var lg = get("tyg-lg", null), tab = get("tyg-tab", "today");
  var lgs = $$(".switch button").map(function(b){ return b.dataset.lg; });
  if(lgs.indexOf(lg) < 0) lg = lgs[0];
  if(TABS.indexOf(tab) < 0) tab = "today";

  function countUp(){
    $$('.lg:not([hidden]) .view:not([hidden]) [data-count]').forEach(function(el){
      var n = +el.dataset.count; if(calm || !n){ el.textContent = n; return; }
      var t0 = null;
      function step(t){ if(!t0) t0 = t; var p = Math.min(1, (t - t0) / 650);
        el.textContent = Math.round(n * (1 - Math.pow(1 - p, 3))); if(p < 1) requestAnimationFrame(step); }
      el.textContent = 0; requestAnimationFrame(step);
    });
  }
  function show(){
    $$(".switch button").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.lg === lg)); });
    document.querySelector(".switch .ind").style.setProperty("--x", Math.max(0, lgs.indexOf(lg)));
    $$(".tabs button").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.tab === tab)); });
    document.querySelector(".tabs .ind").style.setProperty("--x", TABS.indexOf(tab));
    $$(".lg").forEach(function(s){
      s.hidden = s.dataset.lg !== lg;
      $$(".view", s).forEach(function(v){ v.hidden = v.dataset.view !== tab; });
    });
    $$(".ticker").forEach(function(t){ t.hidden = t.dataset.lg !== lg; });
    countUp();
  }
  $$(".switch button").forEach(function(b){ b.addEventListener("click", function(){
    if(lg === b.dataset.lg) return; lg = b.dataset.lg; put("tyg-lg", lg); show(); window.scrollTo(0, 0);
  }); });
  function goTab(t){ if(tab === t) return; tab = t; put("tyg-tab", tab); show(); window.scrollTo(0, 0); }
  $$(".tabs button").forEach(function(b){ b.addEventListener("click", function(){ goTab(b.dataset.tab); }); });
  $$("[data-goto]").forEach(function(b){ b.addEventListener("click", function(){ goTab(b.dataset.goto); }); });

  /* done */
  var done = get("tyg-done", {});
  $$(".card[data-key]").forEach(function(c){
    var k = c.dataset.key, btn = c.querySelector("[data-done]");
    function paint(){ var on = !!done[k]; c.classList.toggle("is-done", on); btn.setAttribute("aria-pressed", String(on)); }
    paint();
    btn.addEventListener("click", function(){ if(done[k]) delete done[k]; else done[k] = 1; put("tyg-done", done); paint(); });
  });

  /* copy group-chat text */
  var toast = document.querySelector(".toast"), tt;
  function say(head, text){
    toast.querySelector("b").textContent = head; toast.querySelector("span").textContent = text;
    toast.classList.add("on"); clearTimeout(tt); tt = setTimeout(function(){ toast.classList.remove("on"); }, 3200);
  }
  $$("[data-copy]").forEach(function(btn){
    btn.addEventListener("click", function(){
      var text = btn.dataset.copy;
      try{ navigator.clipboard.writeText(text).then(function(){ say("Copied", text); }, function(){ say("Copy this", text); }); }
      catch(e){ say("Copy this", text); }
    });
  });

  /* updated x ago */
  $$("[data-ago]").forEach(function(el){
    var t = Date.parse(el.dataset.ago); if(isNaN(t)) return;
    var m = Math.round((Date.now() - t) / 60000), txt;
    if(m < 2) txt = "just now"; else if(m < 60) txt = m + "m ago";
    else if(m < 48*60) txt = Math.round(m/60) + "h ago"; else txt = Math.round(m/1440) + "d ago";
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
    var picks = {get:{}, send:{}}, last = "";
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
      var cls = "verdict", h;
      if(!get.length || !send.length){ h = '<p class="vw">Pick players</p>'; }
      else {
        var mine0 = value(me.players, L.slots), th0 = value(t.players, L.slots);
        var mg = value(swap(me.players, send, get), L.slots) - mine0;
        var tg = value(swap(t.players, get, send), L.slots) - th0;
        var word;
        if(mg >= 10){ word = "Take it"; cls += " good"; }
        else if(mg >= 3){ word = "Small win"; cls += " good"; }
        else if(mg > -3){ word = "Even"; cls += " meh"; }
        else { word = "Pass"; cls += " bad"; }
        h = '<p class="vw">'+word+'</p><div class="vn"><span>You<b>'+sgn(mg)+'</b></span><span>Them<b>'+sgn(tg)+'</b></span></div>';
        var hurt = get.filter(function(p){ return HURT[p.inj]; });
        if(hurt.length) h += '<p class="vx">'+escH(hurt[0].n)+' is hurt.</p>';
        if(mg < 3){
          var c = counter(t, mine0, th0);
          if(c) h += '<p class="vx">Try <b>'+escH(c.send.map(function(p){ return p.n; }).join(" + "))+
            '</b> for <b>'+escH(c.get.n)+'</b> ('+sgn(c.mg)+')</p>';
        }
      }
      out.className = cls; out.innerHTML = h;
      if(h !== last && !calm){ out.classList.remove("pop"); void out.offsetWidth; out.classList.add("pop"); }
      last = h;
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
