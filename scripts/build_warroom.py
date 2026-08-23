#!/usr/bin/env python3
"""Build the war room -- one page that answers "what should I do".

Reads data/state_*.json (pulled from ESPN on the runner) and writes a single
self-contained HTML page. Every number on the page traces back to those files.
Where there is nothing worth doing, the page says so; it never invents an
action to look busy.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ff.engines.trades import best_lineup, find_trades  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "warroom.html"

SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "D/ST", "K"]


def load(key: str) -> dict | None:
    path = DATA / f"state_{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def analyse(state: dict) -> dict:
    slots = state["roster_slots"]
    teams = state["teams"]
    for t in teams:
        lu = best_lineup(t["players"], slots)
        t["_lineup"] = lu
        t["_total"] = lu.projected
    teams.sort(key=lambda t: -t["_total"])
    for rank, t in enumerate(teams, 1):
        t["_rank"] = rank

    me = next((t for t in teams if t["is_me"]), None)
    if me is None or not me["players"]:
        return {"state": state, "teams": teams, "me": None, "drafted": False}

    opponents = [t for t in teams if not t["is_me"]]
    trades = find_trades(me["players"], opponents, slots, limit=6)

    # A waiver add only counts if it would actually crack the lineup. After a
    # 12-team draft it usually would not, and saying so is the useful answer.
    fa_upgrades = []
    my_ids = {p["player_id"] for p in me["players"]}
    for fa in state.get("free_agents", []):
        if fa["player_id"] in my_ids or not fa.get("projected"):
            continue
        trial = best_lineup(me["players"] + [fa], slots)
        gain = trial.projected - me["_total"]
        if gain >= 3.0:
            fa_upgrades.append({**fa, "gain": round(gain, 1)})
    fa_upgrades.sort(key=lambda p: -p["gain"])

    injured = [
        p for p in me["players"]
        if p.get("injury_status") and p["injury_status"].upper() not in ("ACTIVE", "NORMAL")
    ]

    return {
        "state": state,
        "teams": teams,
        "me": me,
        "drafted": True,
        "trades": trades,
        "waivers": fa_upgrades[:4],
        "injured": injured,
    }


# ---------------------------------------------------------------- rendering


def pos_chip(pos: str) -> str:
    key = "DST" if pos == "D/ST" else pos
    return f'<span class="p p-{esc(key)}">{esc(pos)}</span>'


def render_actions(a: dict) -> str:
    cards: list[str] = []

    # Two offers that both spend the same player are one offer, not two. Saying
    # so on the card stops a good idea from being read as two good ideas.
    spent: dict[str, int] = {}
    for t in a["trades"]:
        for p in t.send:
            spent[p["player_id"]] = spent.get(p["player_id"], 0) + 1

    for i, t in enumerate(a["trades"]):
        clash = sorted({p["name"] for p in t.send if spent.get(p["player_id"], 0) > 1})
        overlap = (
            f'<p class="clash">Uses {esc(" and ".join(clash))}, so this and the other '
            f"offer are alternatives &mdash; you can only do one.</p>"
            if clash else ""
        )
        send = "".join(
            f'<li>{pos_chip(p["position"])}<b>{esc(p["name"])}</b>'
            f'<em>{p["projected"]:.0f}</em></li>'
            for p in t.send
        )
        get = "".join(
            f'<li>{pos_chip(p["position"])}<b>{esc(p["name"])}</b>'
            f'<em>{p["projected"]:.0f}</em></li>'
            for p in t.receive
        )
        cards.append(f"""
<article class="act" data-act="trade-{i}">
  <header class="act-h">
    <span class="kind">Trade</span>
    <span class="gain">+{t.my_gain:.0f} pts</span>
  </header>
  <h3>Offer this to <span class="who">{esc(t.partner_name)}</span></h3>
  <div class="swap">
    <div class="side out"><span class="side-l">You give</span><ul>{send}</ul></div>
    <div class="arrow" aria-hidden="true"></div>
    <div class="side in"><span class="side-l">You get</span><ul>{get}</ul></div>
  </div>
  <p class="why">Your season total goes up <b>{t.my_gain:.0f} points</b>.
     Theirs goes up <b>{t.their_gain:.0f}</b> &mdash; which is why they might say yes.</p>
  {overlap}
  <div class="act-do">
    <button class="btn yes" data-yes>Approve</button>
    <button class="btn no" data-no>Pass</button>
  </div>
  <div class="steps" hidden>
    <p><b>Do this in ESPN:</b> League &rarr; Team &rarr; <b>{esc(t.partner_name)}</b>
       &rarr; Propose Trade. Offer
       {esc(", ".join(p["name"] for p in t.send))} for
       {esc(", ".join(p["name"] for p in t.receive))}.</p>
  </div>
</article>""")

    for i, w in enumerate(a["waivers"]):
        cards.append(f"""
<article class="act" data-act="waiver-{i}">
  <header class="act-h">
    <span class="kind">Waivers</span>
    <span class="gain">+{w['gain']:.0f} pts</span>
  </header>
  <h3>Add {esc(w['name'])}</h3>
  <p class="why">{pos_chip(w['position'])} projects {w['projected']:.0f} and would start for you.</p>
  <div class="act-do">
    <button class="btn yes" data-yes>Approve</button>
    <button class="btn no" data-no>Pass</button>
  </div>
  <div class="steps" hidden>
    <p><b>Do this in ESPN:</b> Players &rarr; search <b>{esc(w['name'])}</b> &rarr; Add.</p>
  </div>
</article>""")

    if not cards:
        cards.append("""
<article class="act quiet">
  <header class="act-h"><span class="kind">All clear</span></header>
  <h3>Nothing to do right now</h3>
  <p class="why">No trade makes both sides better, and nobody on waivers would crack
     your lineup. Doing nothing is the right move today.</p>
</article>""")

    return "".join(cards)


def render_lineup(me: dict) -> str:
    lu = me["_lineup"]
    order = {s: i for i, s in enumerate(SLOT_ORDER)}
    starters = sorted(lu.starters, key=lambda s: (order.get(s["slot"], 9), -s["projected"]))
    rows = "".join(
        f'<tr><td class="slot">{esc(s["slot"])}</td>'
        f'<td class="who">{esc(s["name"])}'
        + (' <span class="flag">Q</span>' if (s.get("injury_status") or "").upper() == "QUESTIONABLE" else "")
        + f'</td><td>{pos_chip(s["position"])}</td>'
        f'<td class="n">{s["projected"]:.0f}</td></tr>'
        for s in starters
    )
    bench = "".join(
        f'<tr><td class="who">{esc(p["name"])}</td><td>{pos_chip(p["position"])}</td>'
        f'<td class="n">{(p.get("projected") or 0):.0f}</td></tr>'
        for p in sorted(lu.bench, key=lambda p: -(p.get("projected") or 0))
    )
    return f"""
<div class="two">
  <section class="panel">
    <h2 class="sh">Your starters</h2>
    <table class="grid start"><tbody>{rows}</tbody></table>
    <p class="tot"><span>Season projection</span><b>{lu.projected:,.0f}</b></p>
  </section>
  <section class="panel">
    <h2 class="sh">Your bench</h2>
    <table class="grid bench"><tbody>{bench}</tbody></table>
  </section>
</div>"""


def render_league(a: dict) -> str:
    teams = a["teams"]
    top = teams[0]["_total"]
    low = teams[-1]["_total"]
    span = max(top - low, 1)
    bars = "".join(
        f'<li class="{"mine" if t["is_me"] else ""}">'
        f'<span class="r">{t["_rank"]}</span>'
        f'<span class="t">{esc(t["name"])}</span>'
        f'<span class="bar"><i style="width:{18 + 82 * (t["_total"] - low) / span:.1f}%"></i></span>'
        f'<span class="v">{t["_total"]:,.0f}</span></li>'
        for t in teams
    )
    return f"""
<section class="panel">
  <h2 class="sh">Where you stand</h2>
  <p class="sub">Best possible starting lineup, whole season, by ESPN's own projections.</p>
  <ul class="bars">{bars}</ul>
</section>"""


def render_league_block(key: str, a: dict) -> str:
    state = a["state"]
    if not a["drafted"]:
        return f"""
<section class="league" id="{esc(key)}" hidden>
  <div class="panel quiet">
    <h2 class="sh">{esc(state['name'])}</h2>
    <p class="why">Draft hasn't happened yet. Once it does, this page fills in the
       same way &mdash; lineup, standings and what to do.</p>
  </div>
</section>"""

    me = a["me"]
    total = me["_total"]
    return f"""
<section class="league" id="{esc(key)}">
  <div class="score">
    <div class="sc-team">
      <span class="lbl">{esc(state['name'])} &middot; {state['team_count']} teams</span>
      <h1>{esc(me['name'])}</h1>
    </div>
    <div class="sc-fig">
      <span class="lbl">Rank</span>
      <b>{me['_rank']}<sup>of {state['team_count']}</sup></b>
    </div>
    <div class="sc-fig">
      <span class="lbl">Projected</span>
      <b>{total:,.0f}</b>
    </div>
  </div>

  <h2 class="sh big">Do this now</h2>
  <div class="acts">{render_actions(a)}</div>

  {render_lineup(me)}
  {render_league(a)}
</section>"""


def build() -> str:
    blocks = []
    tabs = []
    for key in ("L1", "L2"):
        state = load(key)
        if not state:
            continue
        a = analyse(state)
        first = not blocks
        tabs.append(
            f'<button data-tab="{esc(key)}" aria-pressed="{"true" if first else "false"}">'
            f'{esc(state["name"])}</button>'
        )
        block = render_league_block(key, a)
        if not first:
            block = block.replace(f'id="{key}">', f'id="{key}" hidden>', 1)
        elif ' hidden>' in block[:200]:
            block = block.replace(' hidden>', '>', 1)
        blocks.append(block)

    stamp = datetime.now(timezone.utc).strftime("%b %-d, %H:%M UTC")
    return PAGE.replace("{{TABS}}", "".join(tabs)) \
               .replace("{{BLOCKS}}", "".join(blocks)) \
               .replace("{{STAMP}}", stamp)


PAGE = """<title>TyG War Room</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500;600&family=Roboto+Mono:wght@500&display=swap">
<style>
:root{
  --ground:#0a0d12; --panel:#131922; --panel2:#1a212c; --line:#28303e;
  --ink:#f2f5fa; --muted:#8892a4; --faint:#5d6879;
  --hot:#ff3b1f; --good:#2fd87a; --warn:#ffc53d;
  --qb:#a97bff; --rb:#2fd4c4; --wr:#ffb020; --te:#ff6e8a; --dst:#7d8a9e; --k:#7d8a9e;
  --disp:"Barlow Condensed",Impact,"Haettenschweiler",sans-serif;
  --body:Barlow,system-ui,-apple-system,sans-serif;
  --mono:"Roboto Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--ground);color:var(--ink);
  font:400 17px/1.5 var(--body);
}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 90px}
sup{font-size:.42em;font-weight:600;margin-left:.35em;color:var(--muted);
    vertical-align:super;letter-spacing:.02em}

/* ---- top bar ---- */
.top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
     padding:22px 0 16px;flex-wrap:wrap}
.mark{font:700 20px/1 var(--disp);letter-spacing:.10em;text-transform:uppercase}
.mark i{color:var(--hot);font-style:normal}
.stamp{font:500 12px/1 var(--mono);color:var(--faint)}

/* ---- league tabs ---- */
.tabs{display:flex;gap:0;margin-bottom:26px;border-bottom:2px solid var(--line)}
.tabs button{
  appearance:none;border:0;background:none;cursor:pointer;color:var(--muted);
  padding:10px 20px 12px;margin-bottom:-2px;
  font:700 15px/1 var(--disp);letter-spacing:.13em;text-transform:uppercase;
  border-bottom:2px solid transparent;
}
.tabs button[aria-pressed="true"]{color:var(--ink);border-bottom-color:var(--hot)}
.tabs button:focus-visible{outline:2px solid var(--hot);outline-offset:-2px}

/* ---- scoreboard ---- */
.score{
  display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap;
  background:var(--panel);border:1px solid var(--line);
  border-top:4px solid var(--hot);
  padding:22px 24px;margin-bottom:38px;
}
.sc-team{flex:1 1 260px;min-width:0}
.sc-team h1{
  margin:4px 0 0;font:700 clamp(38px,8vw,62px)/.9 var(--disp);
  letter-spacing:-.005em;text-transform:uppercase;
}
.lbl{display:block;font:500 11px/1 var(--mono);letter-spacing:.16em;
     text-transform:uppercase;color:var(--faint)}
.sc-fig{text-align:right;padding-left:22px;border-left:1px solid var(--line)}
.sc-fig b{display:block;margin-top:5px;font:700 clamp(34px,6vw,50px)/.9 var(--disp);
          font-variant-numeric:tabular-nums}

/* ---- headings ---- */
.sh{margin:0 0 4px;font:700 14px/1 var(--disp);letter-spacing:.16em;
    text-transform:uppercase;color:var(--muted)}
.sh.big{font-size:24px;color:var(--ink);margin-bottom:16px}
.sub{margin:0 0 16px;color:var(--faint);font-size:14px}

/* ---- action cards ---- */
.acts{display:flex;flex-direction:column;gap:14px;margin-bottom:42px}
.act{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--hot);
     padding:20px 22px 18px}
.act.quiet{border-left-color:var(--good)}
.act[data-state="done"]{border-left-color:var(--good);opacity:.96}
.act[data-state="passed"]{border-left-color:var(--line);opacity:.5}
.act-h{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}
.kind{font:500 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--hot)}
.act.quiet .kind{color:var(--good)}
.gain{font:700 20px/1 var(--disp);color:var(--good);font-variant-numeric:tabular-nums}
.act h3{margin:0 0 14px;font:700 clamp(24px,4vw,32px)/1.05 var(--disp);
        letter-spacing:-.005em;text-wrap:balance}
.act h3 .who{color:var(--hot)}
.why{margin:0;color:var(--muted);font-size:15.5px;max-width:66ch}
.why b{color:var(--ink);font-weight:600}
.clash{margin:10px 0 0;padding-left:11px;border-left:2px solid var(--warn);
       color:var(--warn);font-size:14.5px}

.swap{display:flex;align-items:stretch;gap:14px;margin-bottom:14px;flex-wrap:wrap}
.side{flex:1 1 220px;background:var(--panel2);border:1px solid var(--line);padding:12px 14px}
.side-l{display:block;margin-bottom:8px;font:500 10.5px/1 var(--mono);
        letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.side.out .side-l{color:var(--hot)}
.side.in .side-l{color:var(--good)}
.side ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:7px}
.side li{display:flex;align-items:center;gap:9px}
.side li b{flex:1;font-weight:600;font-size:16px}
.side li em{font:500 13px/1 var(--mono);font-style:normal;color:var(--muted);
            font-variant-numeric:tabular-nums}
.arrow{align-self:center;width:26px;height:2px;background:var(--line);position:relative;flex:0 0 auto}
.arrow::after{content:"";position:absolute;right:-1px;top:-4px;
              border:5px solid transparent;border-left-color:var(--line)}

.act-do{display:flex;gap:10px;margin-top:16px}
.btn{
  appearance:none;cursor:pointer;border:1px solid var(--line);
  padding:12px 26px;background:var(--panel2);color:var(--ink);
  font:700 14px/1 var(--disp);letter-spacing:.13em;text-transform:uppercase;
  transition:background .13s,border-color .13s,color .13s;
}
.btn.yes{background:var(--hot);border-color:var(--hot);color:#fff}
.btn.yes:hover{background:#ff5537;border-color:#ff5537}
.btn.no:hover{border-color:var(--muted);color:var(--muted)}
.btn:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.steps{margin-top:14px;padding:13px 15px;background:var(--panel2);
       border-left:3px solid var(--good)}
.steps p{margin:0;font-size:15px;color:var(--muted)}
.steps b{color:var(--ink);font-weight:600}

/* ---- panels + tables ---- */
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:760px){.two{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);padding:20px 22px;margin-bottom:14px}
.grid{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.grid td{padding:9px 0;border-bottom:1px solid var(--line);vertical-align:middle}
.grid tr:last-child td{border-bottom:0}
.grid .slot{width:52px;font:700 12px/1 var(--disp);letter-spacing:.12em;color:var(--faint)}
.grid .who{font-weight:500;font-size:16.5px}
.grid .n{width:56px;text-align:right;font:500 14px/1 var(--mono)}
.bench .who{color:var(--muted)}
.flag{display:inline-block;margin-left:6px;padding:2px 5px;background:var(--warn);
      color:#231a00;font:700 10px/1 var(--mono);letter-spacing:.06em;vertical-align:1px}
.tot{display:flex;justify-content:space-between;align-items:baseline;
     margin:16px 0 0;padding-top:14px;border-top:2px solid var(--line)}
.tot span{font:500 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.tot b{font:700 30px/1 var(--disp);font-variant-numeric:tabular-nums}

/* position chips */
.p{display:inline-block;min-width:38px;text-align:center;padding:3px 6px;
   font:700 11px/1 var(--mono);letter-spacing:.05em;color:#0a0d12}
.p-QB{background:var(--qb)}.p-RB{background:var(--rb)}.p-WR{background:var(--wr)}
.p-TE{background:var(--te)}.p-DST{background:var(--dst)}.p-K{background:var(--k)}

/* ---- standings bars ---- */
.bars{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.bars li{display:flex;align-items:center;gap:12px}
.bars .r{width:22px;font:500 12px/1 var(--mono);color:var(--faint);text-align:right}
.bars .t{width:190px;flex:0 0 auto;font-size:15px;color:var(--muted);
         white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars .bar{flex:1;height:16px;background:var(--panel2);min-width:60px}
.bars .bar i{display:block;height:100%;background:var(--line)}
.bars .v{width:58px;text-align:right;font:500 13px/1 var(--mono);color:var(--muted)}
.bars .mine .t{color:var(--ink);font-weight:600}
.bars .mine .bar i{background:var(--hot)}
.bars .mine .v{color:var(--ink)}
@media(max-width:600px){.bars .t{width:110px}}

.foot{margin-top:34px;color:var(--faint);font-size:13.5px;max-width:70ch}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
  <div class="top">
    <span class="mark">TyG <i>War Room</i></span>
    <span class="stamp">Updated {{STAMP}}</span>
  </div>

  <nav class="tabs">{{TABS}}</nav>

  {{BLOCKS}}

  <p class="foot">Projections are ESPN's own full-season numbers under each league's
     scoring. They are estimates, not results &mdash; treat a gap of a few points as
     noise and a gap of fifty as real.</p>
</div>

<script>
(function(){
  var KEY = "tyg-warroom-decisions";
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { saved = {}; }

  function store(){
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
  }

  function paint(card, state){
    card.dataset.state = state || "";
    var steps = card.querySelector(".steps");
    if (steps) steps.hidden = state !== "done";
    var yes = card.querySelector("[data-yes]");
    if (yes) yes.textContent = state === "done" ? "Approved" : "Approve";
  }

  document.querySelectorAll(".act[data-act]").forEach(function(card){
    var id = card.dataset.act;
    if (saved[id]) paint(card, saved[id]);
    var yes = card.querySelector("[data-yes]");
    var no = card.querySelector("[data-no]");
    if (yes) yes.addEventListener("click", function(){
      saved[id] = saved[id] === "done" ? "" : "done"; store(); paint(card, saved[id]);
    });
    if (no) no.addEventListener("click", function(){
      saved[id] = saved[id] === "passed" ? "" : "passed"; store(); paint(card, saved[id]);
    });
  });

  var tabs = document.querySelectorAll(".tabs button");
  tabs.forEach(function(btn){
    btn.addEventListener("click", function(){
      tabs.forEach(function(b){ b.setAttribute("aria-pressed", String(b === btn)); });
      document.querySelectorAll(".league").forEach(function(sec){
        sec.hidden = sec.id !== btn.dataset.tab;
      });
    });
  });
})();
</script>
"""


def main() -> int:
    OUT.write_text(build())
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
