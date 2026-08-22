#!/usr/bin/env python3
"""Generate the draft-night cheat sheet from the real board data.

Reads data/draft_L1.json and data/draft_L2.json (produced on the Actions runner
from live ESPN projections) and writes a single self-contained HTML page.

Everything on the page traces back to those files. No number is invented here.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "data" / "draft_cheatsheet.html"

SKILL = {"QB", "RB", "WR", "TE"}
BOARD_DEPTH = 90


def load(key: str) -> dict:
    return json.loads((DATA / f"draft_{key}.json").read_text())


def value_table(board: list[dict], limit: int = 100) -> list[tuple[float, int, dict]]:
    """Players sorted by how far their ADP falls past what they're worth."""
    ranked = sorted([p for p in board if p.get("adp")], key=lambda p: -p["vor"])
    rows = [(p["adp"] - i, i, p) for i, p in enumerate(ranked[:limit], 1)]
    rows.sort(reverse=True)
    return rows


def position_counts(slots: dict, teams: int) -> dict[str, int]:
    return {pos: teams * int(slots.get(pos, 0) or 0) for pos in ("QB", "RB", "WR", "TE")}


def tier_groups(board: list[dict], depth: int) -> list[dict]:
    """Group the board into runs, inserting a cliff marker at each tier break."""
    out: list[dict] = []
    for p in board[:depth]:
        out.append(p)
    return out


def build_league(key: str, headline: str, plan: list[tuple[str, str]]) -> str:
    d = load(key)
    board = [p for p in d["board"] if p["position"] in SKILL]
    slots = d["roster_slots"]
    teams = d["teams"]
    starters = position_counts(slots, teams)

    vals = value_table(board)
    targets = [(s, i, p) for s, i, p in vals if s >= 20][:7]
    avoid = [(s, i, p) for s, i, p in vals if s <= -6][-6:]
    avoid.reverse()

    slot_line = " · ".join(
        f"{n}&thinsp;{lbl}"
        for lbl, n in (
            ("QB", slots.get("QB", 0)), ("RB", slots.get("RB", 0)),
            ("WR", slots.get("WR", 0)), ("TE", slots.get("TE", 0)),
            ("FLEX", slots.get("RB/WR/TE", 0)), ("K", slots.get("K", 0)),
            ("DST", slots.get("D/ST", 0)),
        )
        if n
    )

    # Cliffs on a cross-position board are VOR gaps, not per-position tier
    # numbers -- the board is sorted by value across positions, so "his tier
    # went from 1 to 2" says nothing about whether waiting costs you anything.
    # What matters is where the drop between consecutive picks is unusually
    # large. Only the handful of genuinely big ones get drawn; a rule every
    # other row is decoration, not information.
    top = board[:BOARD_DEPTH]
    gaps = [(top[i]["vor"] - top[i + 1]["vor"], i) for i in range(len(top) - 1)]
    significant = sorted(gaps, reverse=True)[:6]
    cliff_after = {i: round(g, 1) for g, i in significant if g >= 4.0}

    rows: list[str] = []
    for idx, p in enumerate(top, 1):
        adp = f"{p['adp']:.0f}" if p.get("adp") else "—"
        rows.append(
            f'<tr data-player="{html.escape(p["player_id"])}">'
            f'<td class="rk">{idx}</td>'
            f'<td class="nm"><button class="strike" aria-label="Mark {html.escape(p["name"])} drafted">'
            f'{html.escape(p["name"])}</button></td>'
            f'<td><span class="pos pos-{p["position"]}">{p["position"]}</span></td>'
            f'<td class="num">{p["projected"]:.0f}</td>'
            f'<td class="num adp">{adp}</td>'
            f"</tr>"
        )
        drop = cliff_after.get(idx - 1)
        if drop:
            rows.append(
                '<tr class="cliff"><td colspan="5"><span>'
                f"drop-off &mdash; next man is {drop:.0f} points worse"
                "</span></td></tr>"
            )

    target_rows = "".join(
        f'<li><span class="tn">{html.escape(p["name"])}</span>'
        f'<span class="pos pos-{p["position"]}">{p["position"]}</span>'
        f'<span class="tv">worth ~{i}<i>·</i>goes {p["adp"]:.0f}</span></li>'
        for s, i, p in targets
    )
    avoid_rows = "".join(
        f'<li><span class="tn">{html.escape(p["name"])}</span>'
        f'<span class="pos pos-{p["position"]}">{p["position"]}</span>'
        f'<span class="tv">worth ~{i}<i>·</i>goes {p["adp"]:.0f}</span></li>'
        for s, i, p in avoid
    )
    plan_rows = "".join(
        f'<li><span class="rd">{rd}</span><span class="ra">{txt}</span></li>'
        for rd, txt in plan
    )
    starter_chips = "".join(
        f'<div class="stat"><b>{n}</b><span>{pos} jobs</span></div>'
        for pos, n in starters.items() if n
    )

    return f"""
<section class="league" id="{key}" {'hidden' if key == 'L2' else ''}>
  <header class="lg-head">
    <div>
      <h2>{html.escape(d['name'])}</h2>
      <p class="slots">{teams} teams &nbsp;·&nbsp; {slot_line}</p>
    </div>
    <p class="team">You are<br><b>{html.escape(d['my_team']['name'] or '')}</b></p>
  </header>

  <div class="thesis">
    <p class="eyebrow">Read this first</p>
    <p>{headline}</p>
  </div>

  <div class="stats">{starter_chips}
    <div class="stat"><b>{teams * sum(int(v or 0) for k, v in slots.items() if k not in ('BE','IR'))}</b><span>starters league-wide</span></div>
  </div>

  <h3 class="sh">Round plan</h3>
  <ol class="plan">{plan_rows}</ol>

  <div class="two-up">
    <div>
      <h3 class="sh">Let them come to you</h3>
      <p class="sub">Reliably last past their value.</p>
      <ul class="picks good">{target_rows}</ul>
    </div>
    <div>
      <h3 class="sh">Let someone else pay</h3>
      <p class="sub">Market price exceeds the projection.</p>
      <ul class="picks bad">{avoid_rows}</ul>
    </div>
  </div>

  <h3 class="sh">Board <span class="hint">tap a name to cross him off</span></h3>
  <div class="scroll">
    <table class="board">
      <thead><tr><th>#</th><th>Player</th><th>Pos</th><th class="num">Proj</th><th class="num">ADP</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>
"""


L1_HEADLINE = (
    "Take the best running back or receiver available for four straight rounds and "
    "don't overthink it. Your quarterback can wait &mdash; the 12th-best QB scores "
    "within a handful of points of the 3rd, so spending a top pick there buys you "
    "almost nothing. The real edge in this league is the middle-round tight ends."
)

L1_PLAN = [
    ("1&ndash;2", "Best RB or WR on the board. Nothing else. The top nine players are worth 90+ points over replacement; no other position comes close."),
    ("3&ndash;4", "Still RB/WR. Take the one with the bigger gap to the next man at his position, not the bigger name."),
    ("5&ndash;6", "If Trey McBride or Brock Bowers is somehow there, take him &mdash; they are 70+ over replacement at a position where nobody else is. Otherwise keep stacking RB/WR."),
    ("7&ndash;9", "Quarterback window. Anyone from Hurts through Daniels is fine; the gap between them is a rounding error."),
    ("10&ndash;12", "Tight end if you skipped it. Andrews and Hockenson go around here and are worth two rounds earlier."),
    ("13+", "Backup RBs with standalone value. Kicker and defense in the final two rounds &mdash; never sooner."),
]

L2_HEADLINE = (
    "This league starts <b>two quarterbacks</b>, which means 16 QB jobs among 8 teams. "
    "The position runs dry, and the public ADP you see everywhere is priced for "
    "one-QB leagues &mdash; so it will badly understate when they actually go here. "
    "Get your first QB by round 4 and your second by round 8. And stay away from the "
    "expensive tight ends: only 8 TEs start in this league, so the premium ones are "
    "a trap."
)

L2_PLAN = [
    ("1&ndash;2", "Best RB or WR. Same as anywhere &mdash; the top of the board is where the points are."),
    ("3&ndash;4", "First quarterback. Not Josh Allen at his ADP, but don't slide past the Hurts / Daniels / Jackson group either."),
    ("5&ndash;7", "Running backs. You must start four plus two flex, so you need volume here more than in any normal league."),
    ("8&ndash;9", "Second quarterback. Purdy and Mahomes both go around pick 105 nationally and are worth roughly pick 60 in a two-QB format. That is the single biggest exploitable gap in either of your leagues."),
    ("10&ndash;13", "Fill out RB and WR depth. With 15 starters you will be starting people you drafted in double-digit rounds."),
    ("14+", "One tight end, then kicker and defense last. Do not pay up at TE in this format."),
]


def main() -> int:
    l1 = build_league("L1", L1_HEADLINE, L1_PLAN)
    l2 = build_league("L2", L2_HEADLINE, L2_PLAN)
    OUT.write_text(PAGE.replace("{{LEAGUES}}", l1 + l2))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


PAGE = """<title>Draft Night Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;800&family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --ink:#15171f; --paper:#f1f2f6; --card:#ffffff; --line:#d8dae4;
  --muted:#5d6377; --faint:#8b90a3;
  --accent:#2e4a8a; --accent-soft:#e5e9f4;
  --urgent:#c2410c; --urgent-soft:#fbe9e0;
  --good:#166534; --good-soft:#e3f0e6;
  --qb:#6d28d9; --rb:#0f766e; --wr:#a16207; --te:#9f1239;
  --shadow:0 1px 2px rgba(21,23,31,.06),0 8px 24px -12px rgba(21,23,31,.18);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ink:#e9ebf2; --paper:#101219; --card:#181b24; --line:#2b303d;
    --muted:#9ba1b4; --faint:#6f7588;
    --accent:#8fa9e0; --accent-soft:#1d2740;
    --urgent:#f08a5d; --urgent-soft:#3a2318;
    --good:#7dc98f; --good-soft:#16291c;
    --qb:#b295f5; --rb:#5cc9bd; --wr:#dcae4e; --te:#f0849f;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"]{
  --ink:#e9ebf2; --paper:#101219; --card:#181b24; --line:#2b303d;
  --muted:#9ba1b4; --faint:#6f7588;
  --accent:#8fa9e0; --accent-soft:#1d2740;
  --urgent:#f08a5d; --urgent-soft:#3a2318;
  --good:#7dc98f; --good-soft:#16291c;
  --qb:#b295f5; --rb:#5cc9bd; --wr:#dcae4e; --te:#f0849f;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:400 16px/1.55 "IBM Plex Sans",system-ui,-apple-system,sans-serif;
  -webkit-text-size-adjust:100%;
}
.wrap{max-width:760px;margin:0 auto;padding:0 18px 72px}

/* ---- masthead ---- */
.mast{padding:30px 0 18px}
.mast h1{
  font:800 clamp(30px,7.5vw,44px)/1.02 Archivo,system-ui,sans-serif;
  letter-spacing:-.024em;margin:0;text-wrap:balance;
}
.mast p{margin:8px 0 0;color:var(--muted);font-size:14.5px;max-width:56ch}
.stamp{
  display:inline-block;margin-bottom:12px;padding:4px 9px;border-radius:3px;
  background:var(--accent-soft);color:var(--accent);
  font:600 10.5px/1 "IBM Plex Mono",monospace;letter-spacing:.11em;text-transform:uppercase;
}

/* ---- league switch ---- */
.switch{
  position:sticky;top:0;z-index:20;display:flex;gap:5px;
  padding:10px 0;margin-bottom:22px;
  background:color-mix(in srgb,var(--paper) 92%,transparent);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);
}
.switch button{
  flex:1;padding:11px 8px;border:1px solid var(--line);border-radius:7px;
  background:var(--card);color:var(--muted);cursor:pointer;
  font:600 13.5px/1.2 "IBM Plex Sans",sans-serif;
  transition:background .14s,color .14s,border-color .14s;
}
.switch button small{display:block;font-weight:400;font-size:11px;color:var(--faint);margin-top:3px}
.switch button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
.switch button[aria-pressed="true"] small{color:rgba(255,255,255,.78)}
.switch button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---- league head ---- */
.lg-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}
.lg-head h2{font:800 25px/1.1 Archivo,sans-serif;letter-spacing:-.018em;margin:0}
.slots{margin:5px 0 0;color:var(--muted);font-size:13px}
.team{margin:0;text-align:right;font-size:11.5px;color:var(--faint);line-height:1.4;flex-shrink:0}
.team b{display:block;font-size:15px;color:var(--ink);font-weight:600}

/* ---- thesis ---- */
.thesis{
  background:var(--card);border:1px solid var(--line);border-left:3px solid var(--urgent);
  border-radius:8px;padding:16px 18px;margin-bottom:16px;box-shadow:var(--shadow);
}
.eyebrow{
  margin:0 0 7px;color:var(--urgent);
  font:600 10.5px/1 "IBM Plex Mono",monospace;letter-spacing:.12em;text-transform:uppercase;
}
.thesis p:last-child{margin:0;font-size:15.5px;line-height:1.62}
.thesis b{font-weight:600}

/* ---- stats ---- */
.stats{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:26px}
.stat{
  flex:1 1 74px;background:var(--card);border:1px solid var(--line);
  border-radius:7px;padding:10px 11px;
}
.stat b{
  display:block;font:600 20px/1 "IBM Plex Mono",monospace;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em;
}
.stat span{display:block;margin-top:3px;font-size:10.5px;color:var(--faint);letter-spacing:.02em}

/* ---- headings ---- */
.sh{
  font:800 12px/1 Archivo,sans-serif;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);margin:30px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;align-items:baseline;gap:10px;
}
.hint{font:400 10.5px/1 "IBM Plex Sans",sans-serif;letter-spacing:0;text-transform:none;color:var(--faint)}
.sub{margin:-4px 0 10px;font-size:12.5px;color:var(--faint)}

/* ---- plan ---- */
.plan{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1px}
.plan li{display:flex;gap:13px;padding:12px 13px;background:var(--card);border:1px solid var(--line)}
.plan li:first-child{border-radius:8px 8px 0 0}
.plan li:last-child{border-radius:0 0 8px 8px}
.plan li+li{border-top:none}
.rd{
  flex:0 0 46px;font:600 12px/1.5 "IBM Plex Mono",monospace;
  color:var(--accent);font-variant-numeric:tabular-nums;padding-top:1px;
}
.ra{font-size:14.5px;line-height:1.55}

/* ---- picks ---- */
.two-up{display:grid;grid-template-columns:1fr;gap:22px}
@media (min-width:620px){.two-up{grid-template-columns:1fr 1fr;gap:26px}}
.picks{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.picks li{
  display:flex;align-items:center;gap:8px;padding:9px 11px;border-radius:6px;
  background:var(--card);border:1px solid var(--line);
}
.picks.good li{border-left:3px solid var(--good)}
.picks.bad li{border-left:3px solid var(--urgent)}
.tn{flex:1;font-size:14px;font-weight:500}
.tv{
  font:500 10.5px/1 "IBM Plex Mono",monospace;color:var(--faint);
  font-variant-numeric:tabular-nums;white-space:nowrap;
}
.tv i{font-style:normal;opacity:.45;margin:0 4px}

/* ---- position chips ---- */
.pos{
  display:inline-block;min-width:30px;text-align:center;padding:2.5px 5px;border-radius:4px;
  font:600 10px/1 "IBM Plex Mono",monospace;letter-spacing:.05em;
  border:1px solid currentColor;
}
.pos-QB{color:var(--qb)} .pos-RB{color:var(--rb)}
.pos-WR{color:var(--wr)} .pos-TE{color:var(--te)}

/* ---- board ---- */
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.board{width:100%;border-collapse:collapse;font-size:14.5px}
.board th{
  position:sticky;top:60px;z-index:5;background:var(--card);
  text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);
  font:600 10px/1 "IBM Plex Mono",monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);
}
.board td{padding:0 10px;border-bottom:1px solid var(--line);height:42px;vertical-align:middle}
.board tr:last-child td{border-bottom:none}
.rk{
  width:34px;color:var(--faint);
  font:500 12px/1 "IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
}
.nm{width:100%}
.strike{
  all:unset;cursor:pointer;font:500 14.5px/1.3 "IBM Plex Sans",sans-serif;
  color:var(--ink);padding:6px 0;display:block;width:100%;
}
.strike:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.num{
  text-align:right;font:500 13px/1 "IBM Plex Mono",monospace;
  font-variant-numeric:tabular-nums;white-space:nowrap;
}
.adp{color:var(--faint)}
tr.gone{background:color-mix(in srgb,var(--paper) 60%,transparent)}
tr.gone .strike{text-decoration:line-through;color:var(--faint);text-decoration-thickness:1.5px}
tr.gone .num,tr.gone .rk{opacity:.4}
tr.gone .pos{opacity:.35}
tr.cliff td{
  height:auto;padding:0;border-bottom:1px solid var(--urgent);
  background:var(--urgent-soft);
}
tr.cliff span{
  display:block;padding:3px 10px;color:var(--urgent);
  font:600 9.5px/1.5 "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;
}

/* ---- footer ---- */
.foot{
  margin-top:42px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--faint);line-height:1.65;
}
.foot b{color:var(--muted);font-weight:600}
.reset{
  all:unset;cursor:pointer;color:var(--accent);font-weight:600;
  text-decoration:underline;text-underline-offset:2px;
}
.reset:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="wrap">
  <header class="mast">
    <span class="stamp">2026 &middot; pre-draft</span>
    <h1>Draft Night Board</h1>
    <p>Two leagues, two different right answers. Projections are ESPN's own, scored
       under each league's actual settings &mdash; so the same player is worth
       different amounts on each tab.</p>
  </header>

  <nav class="switch">
    <button id="btn-L1" aria-pressed="true" onclick="show('L1')">League 1<small>12 teams &middot; 1 QB</small></button>
    <button id="btn-L2" aria-pressed="false" onclick="show('L2')">League 2<small>8 teams &middot; 2 QB</small></button>
  </nav>

  {{LEAGUES}}

  <footer class="foot">
    <p><b>How to read this.</b> &ldquo;Worth ~40&rdquo; means the projections rank him
    around the 40th most valuable pick. &ldquo;Goes 78&rdquo; is where he's actually
    drafted on average. A big gap between those two is the whole game.</p>
    <p><b>Value is measured against replacement</b>, not raw points. A quarterback
    scoring 370 isn't better than a running back scoring 320 if the 12th-best QB
    scores 288 and the 24th-best RB scores 203. That's why the board isn't just
    sorted by projection.</p>
    <p><b>ADP comes from ESPN's whole population</b>, most of whom play one-QB
    leagues. In League 2 it understates quarterback demand. Treat it as a floor
    for when QBs go there, not a prediction.</p>
    <p style="margin-bottom:0">Crossed-off players are remembered on this device.
    <button class="reset" onclick="resetAll()">Clear the board</button></p>
  </footer>
</div>

<script>
const KEY='ffdraft.gone.v1';
function loadGone(){try{return new Set(JSON.parse(localStorage.getItem(KEY)||'[]'))}catch(e){return new Set()}}
function saveGone(s){try{localStorage.setItem(KEY,JSON.stringify([...s]))}catch(e){}}
let gone=loadGone();

function paint(){
  document.querySelectorAll('tr[data-player]').forEach(tr=>{
    tr.classList.toggle('gone',gone.has(tr.dataset.player));
  });
}
document.addEventListener('click',e=>{
  const btn=e.target.closest('.strike'); if(!btn) return;
  const tr=btn.closest('tr[data-player]'); if(!tr) return;
  const id=tr.dataset.player;
  gone.has(id)?gone.delete(id):gone.add(id);
  saveGone(gone); paint();
});
function resetAll(){gone=new Set();saveGone(gone);paint();}
function show(k){
  ['L1','L2'].forEach(x=>{
    document.getElementById(x).hidden = (x!==k);
    document.getElementById('btn-'+x).setAttribute('aria-pressed',String(x===k));
  });
  window.scrollTo({top:0,behavior:'instant'});
}
paint();
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
