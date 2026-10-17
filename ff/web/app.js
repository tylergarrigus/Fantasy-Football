/* Fantasy Command Center -- client app.
   All data is embedded at build time (window.FCC). Nothing here talks to ESPN;
   every move is a recommendation Tyler carries out in the ESPN app. */
(function () {
  "use strict";
  var D = window.FCC || { leagues: {}, order: [] };
  var LGS = D.order.filter(function (k) { return D.leagues[k]; });

  /* ---------------- storage + state ---------------- */
  function get(k, fb) { try { var v = JSON.parse(localStorage.getItem("fcc:" + k)); return v == null ? fb : v; } catch (e) { return fb; } }
  function put(k, v) { try { localStorage.setItem("fcc:" + k, JSON.stringify(v)); } catch (e) { } }
  var S = {
    tab: get("tab", "home"), lg: get("lg", LGS[0]), sub: get("sub", "lineup"),
    done: get("done", {}), favs: get("favs", {}), untouch: get("untouch", {}),
    density: get("density", "comfortable"), accent: get("accent", "sky"), motion: get("motion", "auto"),
    takeAt: get("takeAt", 10),
    q: "", pos: "ALL", avail: "ALL", sort: "wk", favOnly: false, cmp: [], more: 0,
    wcat: "ALL", horizon: "season", tl: { partner: null, get: {}, send: {} }, sheet: null, open: {}
  };
  if (LGS.indexOf(S.lg) < 0) S.lg = LGS[0];
  if (["home", "leagues", "players", "moves", "insights"].indexOf(S.tab) < 0) S.tab = "home";
  var TABS = ["home", "leagues", "players", "moves", "insights"];
  var MOTION_OFF = function () {
    return S.motion === "off" || (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  };

  /* ---------------- helpers ---------------- */
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function n1(x) { return x == null || isNaN(x) ? "—" : (Math.round(x * 10) / 10).toFixed(1); }
  function n0(x) { return x == null || isNaN(x) ? "—" : String(Math.round(x)); }
  function sg(x, d) { if (x == null || isNaN(x)) return "—"; var v = d === 0 ? Math.round(x) : Math.round(x * 10) / 10; return (v > 0 ? "+" : "") + (d === 0 ? v : v.toFixed(1)); }
  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function L() { return D.leagues[S.lg]; }
  function P(lg, pid) { return lg.players[pid]; }
  function team(lg, id) { for (var i = 0; i < lg.teams.length; i++) if (lg.teams[i].id === id) return lg.teams[i]; return null; }
  function me(lg) { return team(lg, lg.myId); }
  function rec(t) { return t && t.w != null ? t.w + "-" + t.l + (t.t ? "-" + t.t : "") : null; }
  function ord(n) { var s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function ago(iso) {
    var t = Date.parse(iso); if (isNaN(t)) return { txt: "unknown", cls: "stale" };
    var m = Math.round((Date.now() - t) / 60000);
    var txt = m < 2 ? "just now" : m < 60 ? m + "m ago" : m < 36 * 60 ? Math.round(m / 60) + "h ago" : Math.round(m / 1440) + "d ago";
    return { txt: txt, cls: m < 240 ? "ok" : m < 900 ? "old" : "stale" };
  }
  function avg(a) { return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : null; }
  function playedRows(p) { return (p.hist || []).filter(function (r) { return r.played && r.pts != null && r.w < p.lgWeek; }); }
  function last3(p) { var r = playedRows(p); return avg(r.slice(-3).map(function (x) { return x.pts; })); }
  function seasonAvg(p) { var r = playedRows(p); return avg(r.map(function (x) { return x.pts; })); }
  function touches(r) { return (r.tgt || 0) + (r.car || 0) + (r.att ? r.att / 3 : 0); }
  function usage3(p) { var r = playedRows(p).slice(-3); return r.length ? avg(r.map(touches)) : null; }
  function touchPos(p) { return ["RB", "WR", "TE"].indexOf(p.pos) >= 0; }
  function HURT(p) { var s = (p.inj || "").toUpperCase(); return ["OUT", "INJURY_RESERVE", "IR", "DOUBTFUL", "SUSPENSION", "PUP", "NA"].indexOf(s) >= 0; }
  function Q(p) { return (p.inj || "").toUpperCase() === "QUESTIONABLE"; }
  function dead(p) { return p.bye || HURT(p); }
  function wkVal(p) { if (dead(p)) return 0; if (p.wk != null) return p.wk; return p.proj ? p.proj / 17 : 0; }
  function sVal(p) { return p.proj || 0; }
  function statusTag(p) {
    if (p.bye) return '<span class="tag dn">▼ BYE</span>';
    var s = (p.inj || "").toUpperCase();
    if (HURT(p)) return '<span class="tag dn">▼ ' + esc(s === "INJURY_RESERVE" ? "IR" : s === "DOUBTFUL" ? "DOUBT" : s.slice(0, 4)) + '</span>';
    if (Q(p)) return '<span class="tag cau">◆ QUES</span>';
    return "";
  }
  function oppWord(p) {
    if (p.bye) return { w: "Bye week", cls: "dn", g: "▼" };
    if (!p.opp) return { w: "", cls: "neu", g: "" };
    var r = p.oppRank;
    if (!r) return { w: "vs " + p.opp, cls: "neu", g: "●" };
    if (r <= 8) return { w: "vs " + p.opp + " · tough D (" + ord(r) + ")", cls: "dn", g: "▼" };
    if (r >= 25) return { w: "vs " + p.opp + " · soft D (" + ord(r) + ")", cls: "up", g: "▲" };
    return { w: "vs " + p.opp + " · even (" + ord(r) + ")", cls: "neu", g: "●" };
  }
  function sem(d, close) {
    close = close == null ? 1.5 : close;
    if (d == null || isNaN(d)) return { cls: "neu", g: "●", w: "NEUTRAL" };
    if (Math.abs(d) < close) return { cls: "cau", g: "◆", w: "CLOSE" };
    return d > 0 ? { cls: "up", g: "▲", w: "FAVORABLE" } : { cls: "dn", g: "▼", w: "RISK" };
  }
  function posB(pos) { var k = pos === "D/ST" ? "DST" : pos; return '<span class="pos ' + esc(k) + '">' + esc(k) + "</span>"; }
  function initials(n) { var w = n.replace(/ D\/ST$/, "").split(" "); return (w[0][0] || "") + (w.length > 1 ? w[w.length - 1][0] : ""); }
  function ownerWord(lg, p) { if (p.owner == null) return "Free agent"; var t = team(lg, p.owner); return t ? (t.me ? "Your team" : t.name) : ""; }
  function fmtKick(iso) { if (!iso) return ""; var d = new Date(iso); if (isNaN(d)) return ""; return d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }

  /* ---------------- lineup math (mirrors ff/engines/trades.best_lineup) ---------------- */
  var FIXED = ["QB", "RB", "WR", "TE", "K", "D/ST"], FLEX = "RB/WR/TE", FLEXP = { RB: 1, WR: 1, TE: 1 };
  function bestLineup(players, slots, val) {
    var pool = players.slice().sort(function (a, b) { return val(b) - val(a); });
    var used = {}, starters = [], total = 0, unfilled = [];
    FIXED.forEach(function (pos) {
      var need = +(slots[pos] || 0);
      for (var i = 0; i < pool.length && need > 0; i++) {
        var p = pool[i]; if (used[p.id] || p.pos !== pos) continue;
        used[p.id] = 1; starters.push({ p: p, slot: pos, v: val(p) }); total += val(p); need--;
      }
      for (var k = 0; k < need; k++) unfilled.push(pos);
    });
    var flex = +(slots[FLEX] || 0);
    for (var j = 0; j < pool.length && flex > 0; j++) {
      var q = pool[j]; if (used[q.id] || !FLEXP[q.pos]) continue;
      used[q.id] = 1; starters.push({ p: q, slot: "FLEX", v: val(q) }); total += val(q); flex--;
    }
    for (var m = 0; m < flex; m++) unfilled.push("FLEX");
    return { starters: starters, bench: pool.filter(function (p) { return !used[p.id]; }), total: total, unfilled: unfilled };
  }
  function lv(players, slots, val) { return bestLineup(players, slots, val).total; }
  function swap(roster, out, inn) { var g = {}; out.forEach(function (p) { g[p.id] = 1; }); return roster.filter(function (p) { return !g[p.id]; }).concat(inn); }
  function roster(lg, t) { return t.players.map(function (id) { return P(lg, id); }); }
  var SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "D/ST", "K"];
  var SLOT_LABEL = { "RB/WR/TE": "FLEX", "D/ST": "DST" };
  function startSlots(lg) {
    var out = []; SLOT_ORDER.forEach(function (s) { for (var i = 0; i < +(lg.slots[s] || 0); i++) out.push(s); }); return out;
  }
  function currentStarters(lg, t) {
    return roster(lg, t).filter(function (p) { return p.slot && p.slot !== "BE" && p.slot !== "IR"; });
  }
  function bySlot(lg, t) {
    var g = {}; currentStarters(lg, t).forEach(function (p) { (g[p.slot] = g[p.slot] || []).push(p); });
    Object.keys(g).forEach(function (k) { g[k].sort(function (a, b) { return wkVal(b) - wkVal(a); }); });
    return g;
  }

  /* ---------------- derived per league (computed once) ---------------- */
  var C = {};
  LGS.forEach(function (k) {
    var lg = D.leagues[k];
    Object.keys(lg.players).forEach(function (pid) { lg.players[pid].lgWeek = lg.week; lg.players[pid].lg = k; });
    var mine = me(lg), my = roster(lg, mine);
    var c = C[k] = {};
    c.me = mine;
    c.oppId = (mine.sched || [])[lg.week - 1];
    c.opp = c.oppId != null ? team(lg, c.oppId) : null;
    c.mu = null;
    (lg.matchups || []).forEach(function (m) { if (m.home === mine.id || m.away === mine.id) c.mu = m; });
    if (c.mu && !c.opp) { c.oppId = c.mu.home === mine.id ? c.mu.away : c.mu.home; c.opp = team(lg, c.oppId); }
    c.myScore = c.mu ? (c.mu.home === mine.id ? c.mu.hs : c.mu.as) : null;
    c.oppScore = c.mu ? (c.mu.home === mine.id ? c.mu.as : c.mu.hs) : null;
    c.myProj = c.mu ? (c.mu.home === mine.id ? c.mu.hp : c.mu.ap) : null;
    c.oppProj = c.mu ? (c.mu.home === mine.id ? c.mu.ap : c.mu.hp) : null;
    c.starters = currentStarters(lg, mine);
    c.curWeek = c.starters.reduce(function (s, p) { return s + wkVal(p); }, 0);
    c.bestWeek = bestLineup(my, lg.slots, wkVal);
    c.bestSeason = bestLineup(my, lg.slots, sVal);
    c.left = c.starters.filter(function (p) { return !p.bye && !(p.played >= 100); }).length;
    c.oppLeft = c.opp ? currentStarters(lg, c.opp).filter(function (p) { return !p.bye && !(p.played >= 100); }).length : null;
    c.deadStarters = c.starters.filter(dead);
    var real = lg.queue.filter(function (a) { return a.kind !== "info"; });
    c.alts = real.filter(function (a) { return a.kind === "trade" && /^Alternative/.test(a.title); });
    c.moves = real.filter(function (a) { return c.alts.indexOf(a) < 0; });
    // What Home and the badges count: every lineup fix, the two best pickups,
    // and the single best trade idea. The rest lives in Moves.
    var trades = c.moves.filter(function (a) { return a.kind === "trade" || a.kind === "plan"; });
    c.todo = c.moves.filter(function (a) { return a.kind === "lineup" || a.kind === "injury"; })
      .concat(c.moves.filter(function (a) { return a.kind === "waiver"; }).slice(0, 2));
    if (trades.length) c.todo.push(trades.slice().sort(function (a, b) { return b.gain - a.gain; })[0]);
    c.todo.sort(function (a, b) { return (b.urgency - a.urgency) || (b.gain - a.gain); });
    c.rankByProj = lg.teams.slice().sort(function (a, b) { return b.lineupProj - a.lineupProj; }).map(function (t) { return t.id; });
    // waiver scout
    var baseS = c.bestSeason.total, baseW = c.bestWeek.total;
    var needed = {}; c.bestWeek.starters.forEach(function (s) { if (s.v > 0) needed[s.p.id] = 1; });
    c.scout = lg.fa.map(function (pid) {
      var p = P(lg, pid), gs = lv(my.concat([p]), lg.slots, sVal) - baseS, gw = lv(my.concat([p]), lg.slots, wkVal) - baseW;
      var l3 = last3(p), sa = seasonAvg(p), rows = playedRows(p), u3 = usage3(p);
      var lastU = rows.length ? touches(rows[rows.length - 1]) : null, prevU = rows.length > 1 ? avg(rows.slice(0, -1).map(touches)) : null;
      var cats = [], skill = ["QB", "RB", "WR", "TE"].indexOf(p.pos) >= 0, touchPos = ["RB", "WR", "TE"].indexOf(p.pos) >= 0;
      if (gs >= 3) cats.push("start");
      if (gw >= 2 && !dead(p)) cats.push("week");
      if (skill && rows.length >= 2 && l3 >= 8 && sa && l3 >= 1.25 * sa) cats.push("hot");
      if (touchPos && lastU != null && prevU != null && lastU >= 8 && lastU >= 1.4 * prevU) cats.push("role");
      if (!cats.length && p.proj && skill) {
        var worst = Math.min.apply(null, c.bestSeason.starters.filter(function (s) { return s.p.pos === p.pos; }).map(function (s) { return s.v; }).concat([Infinity]));
        if (worst !== Infinity && p.proj >= 0.8 * worst) cats.push("stash");
      }
      if (l3 >= 10 && u3 != null && u3 < 4 && ["RB", "WR", "TE"].indexOf(p.pos) >= 0) cats.push("warn");
      var drop = null;
      if (gs > 0 || gw > 0) {
        var cand = my.filter(function (q) { return q.slot !== "IR" && !needed[q.id] && !(q.pos === p.pos && q.proj > p.proj); })
          .sort(function (a, b) { return sVal(a) - sVal(b); });
        drop = cand[0] || null;
      }
      return { p: p, gs: gs, gw: gw, l3: l3, sa: sa, u3: u3, lastU: lastU, prevU: prevU, cats: cats, drop: drop,
        score: gs * 2 + gw * 3 + (l3 || 0) * 0.5 + (cats.indexOf("role") >= 0 ? 4 : 0) - (dead(p) ? 6 : 0) };
    }).filter(function (x) { return x.cats.length; }).sort(function (a, b) { return b.score - a.score; });
    // trends league-wide
    var all = Object.keys(lg.players).map(function (pid) { return P(lg, pid); });
    c.trend = all.map(function (p) { var l = last3(p), s = seasonAvg(p); return { p: p, l3: l, sa: s, d: l != null && s != null && playedRows(p).length >= 3 ? l - s : null }; })
      .filter(function (x) { return x.d != null && (x.p.proj || 0) > 40; });
    c.risers = c.trend.slice().sort(function (a, b) { return b.d - a.d; }).slice(0, 5);
    c.fallers = c.trend.slice().sort(function (a, b) { return a.d - b.d; }).slice(0, 5);
    // bye outlook for my current starters
    c.byes = {};
    c.starters.forEach(function (p) { (p.byes || []).forEach(function (w) { if (w >= lg.week) (c.byes[w] = c.byes[w] || []).push(p); }); });
    // schedule ahead
    c.ahead = [];
    for (var w = lg.week + 1; w <= Math.min(lg.week + 3, (mine.sched || []).length); w++) {
      var oid = mine.sched[w - 1], ot = team(lg, oid); if (!ot) continue;
      var r = c.rankByProj.indexOf(oid) + 1, q = r / lg.teams.length;
      c.ahead.push({ w: w, t: ot, rank: r, word: q <= 0.34 ? "Tough" : q >= 0.67 ? "Soft" : "Even", cls: q <= 0.34 ? "dn" : q >= 0.67 ? "up" : "neu" });
    }
    // depth
    c.depth = ["QB", "RB", "WR", "TE"].map(function (pos) {
      var st = c.bestSeason.starters.filter(function (s) { return s.p.pos === pos; }), bn = c.bestSeason.bench.filter(function (p) { return p.pos === pos && p.slot !== "IR"; });
      var worst = st.length ? Math.min.apply(null, st.map(function (s) { return s.v; })) : 0, best = bn.length ? Math.max.apply(null, bn.map(sVal)) : 0;
      var ratio = worst ? best / worst : 0;
      return { pos: pos, starters: st.length, bench: bn.length, worst: worst, best: best, word: !bn.length || ratio < 0.5 ? "Thin" : ratio < 0.8 ? "OK" : "Deep", cls: !bn.length || ratio < 0.5 ? "dn" : ratio < 0.8 ? "cau" : "up" };
    });
  });

  /* ---------------- tiny components ---------------- */
  function spark(p, w) {
    var rows = (p.hist || []).filter(function (r) { return r.w < p.lgWeek; });
    if (rows.length < 2) return '<p class="empty">Not enough games yet.</p>';
    var W = w || 240, H = 44, pad = 6, n = rows.length;
    var max = Math.max.apply(null, rows.map(function (r) { return Math.max(r.pts || 0, r.proj || 0); }).concat([1]));
    var x = function (i) { return pad + i * (W - 2 * pad) / Math.max(1, n - 1); }, y = function (v) { return H - pad - (v / max) * (H - 2 * pad); };
    var pts = rows.map(function (r, i) { return r.played ? [x(i), y(r.pts || 0)] : null; });
    var d = "", area = "", started = false, lastPt = null;
    pts.forEach(function (pt, i) { if (!pt) { started = false; return; } d += (started ? " L" : " M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1); started = true; lastPt = pt; });
    var proj = rows.map(function (r, i) { return r.proj != null ? (i ? "L" : "M") + x(i).toFixed(1) + " " + y(r.proj).toFixed(1) : ""; }).join(" ");
    var byes = rows.map(function (r, i) { return r.played ? "" : '<text class="bye" x="' + (x(i) - 3).toFixed(1) + '" y="' + (H - 2) + '">B</text>'; }).join("");
    var dots = lastPt ? '<circle cx="' + lastPt[0].toFixed(1) + '" cy="' + lastPt[1].toFixed(1) + '" r="3"/>' : "";
    var first = rows[0].w, last = rows[rows.length - 1].w;
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="Points by week ' + first + " to " + last + '">' +
      (proj ? '<path class="proj" d="' + proj + '"/>' : "") + '<path class="l" d="' + d.trim() + '"/>' + dots + byes + "</svg>" +
      '<div class="row between"><small>Wk ' + first + "–" + last + " · pts (dashed = proj, B = bye)</small><small><b>" + n1(rows[rows.length - 1].pts) + "</b> last</small></div>";
  }
  function bar(v, max, cls, i) { var w = max ? Math.max(0, Math.min(1, v / max)) : 0; return '<div class="bar"><i class="' + (cls || "") + '" style="--w:' + w.toFixed(3) + ";--i:" + (i || 0) + '"></i></div>'; }
  function cmpBars(a, b, la, lb) {
    var max = Math.max(a || 0, b || 0, 1), s = sem((a || 0) - (b || 0), 0.5);
    return '<div class="cmp"><div class="bar l">' + '<i class="' + (s.cls === "up" ? "up" : "neu") + '" style="--w:' + ((a || 0) / max).toFixed(3) + '"></i></div><b>' + (la || n1(a)) + " · " + (lb || n1(b)) + '</b><div class="bar"><i class="' + (s.cls === "dn" ? "dn" : "neu") + '" style="--w:' + ((b || 0) / max).toFixed(3) + '"></i></div></div>';
  }
  function pline(lg, p, opts) {
    opts = opts || {};
    var o = oppWord(p), sub = opts.sub != null ? opts.sub : (esc(p.team || "") + (o.w ? " · " + esc(o.w) : ""));
    var val = opts.val != null ? opts.val : n1(wkVal(p)), unit = opts.unit != null ? opts.unit : "wk proj";
    var cls = "pl" + (opts.cls ? " " + opts.cls : "") + (dead(p) && opts.deadMark !== false ? " dead" : "");
    var sgn = opts.sign ? '<span class="sg ' + opts.sign + '">' + (opts.sign === "in" ? "+" : "−") + "</span>" : "";
    return '<li class="' + cls + '" data-player="' + esc(p.id) + '">' + sgn + posB(p.pos) +
      '<span class="nm"><b>' + esc(p.n) + "</b><small>" + sub + "</small></span>" + statusTag(p) +
      '<span class="val"><b>' + val + "</b><small>" + unit + "</small></span></li>";
  }
  function xbtn(label) { return '<button class="xbtn" data-x aria-expanded="false"><span>' + esc(label || "Details") + '</span><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>'; }
  function chk(key) { return '<button class="chk" data-done="' + esc(key) + '" aria-pressed="' + (S.done[key] ? "true" : "false") + '" aria-label="Mark done"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></button>'; }
  var KIND = { lineup: ["Lineup", "acc"], injury: ["Injury", "dn"], waiver: ["Pickup", "up"], trade: ["Trade", "cau"], plan: ["2-step trade", "cau"], info: ["Info", ""] };
  function kindChip(k) { var x = KIND[k] || ["Move", ""]; return '<span class="chip ' + x[1] + '">' + x[0] + "</span>"; }
  function lgChip(k) { return '<span class="chip">' + esc(D.leagues[k].name) + "</span>"; }
  function gainChip(a) {
    if (!a.gain) return "";
    var unit = a.kind === "lineup" ? "pts this wk" : "season pts";
    return '<span class="sign up">▲ ' + sg(a.gain, a.kind === "lineup" ? 1 : 0) + '</span> <small>' + unit + "</small>";
  }
  function playerNames(lg, names) {
    var by = {}; Object.keys(lg.players).forEach(function (pid) { by[lg.players[pid].n] = lg.players[pid]; });
    return names.map(function (n) { return by[n]; }).filter(Boolean);
  }
  function names(ps) { return ps.map(function (p) { return p.name || p.n; }).join(" + "); }
  function shortTitle(a) {
    var ps = a.players || [];
    if (a.kind === "trade" && a.trade) return "Get " + names(a.trade.receive);
    if (a.kind === "plan" && a.trade) return "Get " + names(a.trade.step2.receive) + " in two moves";
    if (a.kind === "waiver") return "Add " + (ps[0] || "");
    if (a.kind === "injury") return "Bench " + (ps[0] || "");
    if (a.kind === "lineup" && /^Pick up/.test(a.title)) return "Pick up " + (ps[0] || "");
    if (a.kind === "lineup") return "Start " + (ps[0] || "");
    return a.title;
  }
  function brief(a) {
    var ps = a.players || [], t = a.trade;
    if (a.kind === "trade" && t) return "Give " + names(t.send) + " to " + t.partner_name.trim() + ". Both lineups improve: you " + sg(t.my_gain, 0) + ", them " + sg(t.their_gain, 0) + ".";
    if (a.kind === "plan" && t) return "First " + names(t.step1.receive) + " from " + t.step1.partner_name.trim() + ", then " + names(t.step2.receive) + " from " + t.step2.partner_name.trim() + ". Step 2 only if step 1 lands.";
    if (a.kind === "waiver") return "Would start for you right now." + (ps[1] ? " Drop " + ps[1] + "." : "");
    if (a.kind === "lineup" && /^Pick up/.test(a.title)) return "Fills a dead spot this week." + (ps[1] ? " Drop " + ps[1] + "." : "");
    return a.why;
  }
  function actionCard(a, i, full) {
    var lg = D.leagues[a.lg], key = a.key, done = !!S.done[key];
    var ps = playerNames(lg, a.players || []);
    var rows = "";
    if (a.kind === "lineup" && /^Start /.test(a.title) && ps.length >= 2) rows = pline(lg, ps[0], { sign: "in", cls: "in" }) + pline(lg, ps[1], { sign: "out", cls: "out ghost", deadMark: true });
    else if (a.kind === "lineup" && ps.length) rows = pline(lg, ps[0], { sign: "in", cls: "in" }) + (ps[1] ? pline(lg, ps[1], { sign: "out", cls: "out ghost", val: n0(ps[1].proj), unit: "season" }) : "");
    else if (a.kind === "injury" && ps.length) rows = pline(lg, ps[0], { sign: "out", cls: "out", val: "0", unit: "this wk" });
    else if (a.kind === "waiver" && ps.length) rows = pline(lg, ps[0], { sign: "in", cls: "in", val: n0(ps[0].proj), unit: "season proj" }) + (ps[1] ? pline(lg, ps[1], { sign: "out", cls: "out ghost", val: n0(ps[1].proj), unit: "season proj" }) : "");
    else if ((a.kind === "trade") && a.trade) rows = a.trade.receive.map(function (p) { var q = playerNames(lg, [p.name])[0]; return q ? pline(lg, q, { sign: "in", cls: "in", val: n0(q.proj), unit: "season proj" }) : ""; }).join("") +
      a.trade.send.map(function (p) { var q = playerNames(lg, [p.name])[0]; return q ? pline(lg, q, { sign: "out", cls: "out ghost", val: n0(q.proj), unit: "season proj" }) : ""; }).join("");
    else if (a.kind === "plan" && a.trade) rows = [a.trade.step1, a.trade.step2].map(function (st, k) {
      return '<li class="pl"><span class="mono">' + (k + 1) + '</span><span class="nm"><b>with ' + esc(st.partner_name) + "</b><small>they gain " + sg(st.their_gain, 0) + " lineup pts</small></span></li>" +
        st.receive.map(function (p) { var q = playerNames(lg, [p.name])[0]; return q ? pline(lg, q, { sign: "in", cls: "in", val: n0(q.proj), unit: "season proj" }) : ""; }).join("") +
        st.send.map(function (p) { var q = playerNames(lg, [p.name])[0]; return q ? pline(lg, q, { sign: "out", cls: "out ghost", val: n0(q.proj), unit: "season proj" }) : ""; }).join("");
    }).join("");
    var partner = a.trade && a.trade.partner_name ? '<p class="sub">with <b>' + esc(a.trade.partner_name) + "</b> · they gain " + sg(a.trade.their_gain, 0) + "</p>" : "";
    var steps = a.steps && a.steps !== "—" ? '<div class="stack"><span class="lbl">Next</span><p class="sub">' + esc(a.steps) + "</p></div>" : "";
    var conf = a.conf ? '<p class="sub"><b>' + esc(a.conf) + " confidence</b> · " + esc(a.confWhy || "") + "</p>" : "";
    var msg = a.msg ? '<div class="stack"><span class="lbl">Group chat</span><p class="bub">' + esc(a.msg) + '</p><button class="btn sm" data-copy="' + esc(key) + '"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5h10"/></svg>Copy text</button></div>' : "";
    var urgent = a.urgency >= 2 ? '<span class="chip dn">▼ Now</span>' : "";
    var open = full || S.open[key] ? " open" : "";
    return '<article class="card' + (a.urgency >= 2 ? " risk" : "") + (done ? " done" : "") + open + '" data-key="' + esc(key) + '" style="--i:' + (i || 0) + '">' +
      '<div class="row"><div class="row grow" style="gap:6px;flex-wrap:wrap">' + kindChip(a.kind) + (LGS.length > 1 ? lgChip(a.lg) : "") + urgent + "</div>" + chk(key) + "</div>" +
      '<div class="row between"><h3 class="ttl grow">' + esc(shortTitle(a)) + '</h3><div class="kv">' + gainChip(a) + "</div></div>" +
      '<p class="sub">' + esc(brief(a)) + "</p>" +
      '<div class="x"><div><div class="stack">' + (rows ? '<ul class="stack" style="gap:0">' + rows + "</ul>" : "") + partner + (brief(a) !== a.why ? '<p class="sub">' + esc(a.why) + "</p>" : "") + conf + steps + msg + "</div></div></div>" +
      xbtn("Why & how") + "</article>";
  }

  /* ---------------- views ---------------- */
  function vHome() {
    var all = []; LGS.forEach(function (k) { C[k].todo.forEach(function (a) { all.push(a); }); });
    all.sort(function (a, b) { return (b.urgency - a.urgency) || (b.gain - a.gain); });
    var top = all.filter(function (a) { return !S.done[a.key]; })[0];
    var i = 0, h = '<div class="v">';
    h += '<section class="sec" style="--i:' + i++ + '"><h2>Right now</h2></section>';
    if (top) h += actionCard(top, i++, false).replace('class="card', 'class="card hi').replace('class="ttl grow"', 'class="ttl lg grow"');
    else h += '<div class="calm" style="--i:' + i++ + '"><b>No urgent moves</b><span>Lineups are set and nothing on waivers beats your starters.</span></div>';
    h += '<section class="sec" style="--i:' + i++ + '"><h2>Your leagues</h2></section>';
    LGS.forEach(function (k) {
      var lg = D.leagues[k], c = C[k], t = c.me, o = c.opp;
      var live = c.myScore != null && (c.myScore > 0 || c.oppScore > 0);
      var ms = live ? c.myScore : c.myProj, os = live ? c.oppScore : c.oppProj;
      var alerts = c.deadStarters.length ? '<span class="chip dn">▼ ' + c.deadStarters.length + " starter" + (c.deadStarters.length > 1 ? "s" : "") + " out/bye</span>" : "";
      var mv = c.todo.filter(function (a) { return !S.done[a.key]; }).length;
      h += '<article class="card" data-nav="leagues" data-lg="' + k + '" style="--i:' + i++ + '" role="button" tabindex="0">' +
        '<div class="row between"><div><h3 class="ttl">' + esc(lg.name) + '</h3><p class="sub">' + (rec(t) ? "<b>" + rec(t) + "</b> · " : "") + (t.standing ? ord(t.standing) + " of " + lg.teamCount : ord(t.rank) + " by projection") + (t.po != null ? " · " + n0(t.po) + "% playoff odds (ESPN)" : "") + "</p></div>" +
        '<div class="row" style="gap:6px;flex-wrap:wrap;justify-content:flex-end">' + (mv ? '<span class="chip acc">' + mv + " move" + (mv > 1 ? "s" : "") + "</span>" : '<span class="chip up">▲ Set</span>') + alerts + "</div></div>";
      if (o) {
        h += '<div class="mu"><div class="names"><b>' + esc(t.name) + '</b><span class="lbl">Wk ' + lg.week + '</span><b class="r">' + esc(o.name) + "</b></div>" +
          '<div class="scores"><b data-count="' + n1(ms) + '">' + n1(ms) + "</b><span>" + (live ? "SO FAR" : "PROJ") + '</span><b class="r" data-count="' + n1(os) + '">' + n1(os) + "</b></div>" +
          (live ? '<div class="pr"><span>proj ' + n1(c.myProj) + "</span><span></span><span class=\"r\">proj " + n1(c.oppProj) + "</span></div>" : "") +
          '<div class="pr"><span>' + c.left + " left to play</span><span></span><span class=\"r\">" + (c.oppLeft != null ? c.oppLeft + " left to play" : "") + "</span></div>" + cmpBars(ms, os) + "</div>";
      } else h += '<p class="empty">Matchup unavailable in this pull.</p>';
      h += '<div class="row between" style="padding-top:4px;border-top:1px solid var(--line)"><small>' + (c.left === 0 && live ? "All your starters have played" : "Matchup · standings · roster") + '</small><span class="sign" style="color:var(--acc)">Open ›</span></div></article>';
    });
    h += '<div class="quick" style="--i:' + i++ + '">' +
      '<button data-nav="moves" data-sub="lineup"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h10"/></svg>Set lineup</button>' +
      '<button data-nav="moves" data-sub="waivers"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>Waiver scout</button>' +
      '<button data-nav="moves" data-sub="trades"><svg viewBox="0 0 24 24"><path d="M7 7h12l-3-3M17 17H5l3 3"/></svg>Trade lab</button>' +
      '<button data-nav="players"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/></svg>Players</button></div>';
    var rest = all.filter(function (a) { return a !== top; });
    if (rest.length) {
      h += '<section class="sec" style="--i:' + i++ + '"><h2>Also worth doing</h2><small>' + rest.length + "</small></section>";
      rest = rest.slice(0, 6);
      rest.forEach(function (a) { h += actionCard(a, i++, false); });
    }
    var news = []; LGS.forEach(function (k) { D.leagues[k].news.forEach(function (n) { if (n.mine) news.push(n); }); });
    news = news.filter(function (n, idx, arr) { return arr.findIndex(function (m) { return m.h === n.h; }) === idx; }).slice(0, 4);
    if (news.length) {
      h += '<section class="sec" style="--i:' + i++ + '"><h2>Your players in the news</h2></section><article class="card" style="--i:' + i++ + '"><ul class="news">' +
        news.map(function (n) { return '<li><span class="who">' + esc(n.who) + "</span><p>" + (n.url ? '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.h) + "</a>" : esc(n.h)) + "</p><small>" + esc(n.src || "") + (n.when ? " · " + esc(n.when) : "") + "</small></li>"; }).join("") + "</ul></article>";
    }
    return h + "</div>";
  }

  function vLeagues() {
    var lg = L(), c = C[S.lg], t = c.me, o = c.opp, i = 0, h = '<div class="v">';
    // matchup
    var live = c.myScore != null && (c.myScore > 0 || c.oppScore > 0);
    h += '<article class="card" style="--i:' + i++ + '"><div class="row between"><h3 class="ttl">Week ' + lg.week + " matchup</h3><small>as of " + esc(ago(lg.pulledAt).txt) + "</small></div>";
    if (o) {
      var mine = bySlot(lg, t), theirs = bySlot(lg, o), slots = startSlots(lg), used = {}, usedT = {};
      h += '<div class="mu"><div class="names"><b>' + esc(t.name) + "</b><span></span><b class=\"r\">" + esc(o.name) + "</b></div>" +
        '<div class="pr"><span>' + (rec(t) || "") + "</span><span></span><span class=\"r\">" + (rec(o) || "") + "</span></div>" +
        '<div class="scores"><b>' + n1(live ? c.myScore : c.myProj) + "</b><span>" + (live ? "SO FAR" : "PROJ") + '</span><b class="r">' + n1(live ? c.oppScore : c.oppProj) + "</b></div>" +
        '<div class="pr"><span>proj ' + n1(c.myProj) + " · " + c.left + " left</span><span></span><span class=\"r\">proj " + n1(c.oppProj) + " · " + c.oppLeft + " left</span></div></div>" +
        '<div class="x"><div><div class="slots h2h">' + slots.map(function (s) {
          var a = (mine[s] || []).filter(function (p) { return !used[p.id]; })[0], b = (theirs[s] || []).filter(function (p) { return !usedT[p.id]; })[0];
          if (a) used[a.id] = 1; if (b) usedT[b.id] = 1;
          var av = a ? (a.played >= 100 ? a.wpts : wkVal(a)) : 0, bv = b ? (b.played >= 100 ? b.wpts : wkVal(b)) : 0, sm = sem(av - bv, 1.5);
          var side = function (p, r, v) { return p ? '<div class="side' + (r ? " r" : "") + '"><b>' + esc(p.n) + "</b><small>" + (p.played >= 100 ? "final " : dead(p) ? "" : "proj ") + "<b>" + n1(v) + "</b>" + (dead(p) ? " · " + (p.bye ? "bye" : "out") : "") + "</small></div>" : '<div class="side' + (r ? " r" : "") + '"><b class="dim">Empty</b></div>'; };
          return '<div class="slot">' + side(a, false, av) + '<div class="mid"><span>' + esc(SLOT_LABEL[s] || s) + '</span><b class="sign ' + sm.cls + '">' + sm.g + " " + sg(av - bv) + "</b></div>" + side(b, true, bv) + "</div>";
        }).join("") + '</div><p class="sub" style="padding-top:8px">▲ you lead the spot · ▼ they do · ◆ within 1.5. Final = game over; proj = ESPN projection.</p></div></div>' + xbtn("Position by position");
    } else h += '<p class="empty">No matchup found for this week.</p>';
    h += "</article>";
    // standings
    var st = lg.teams.slice().sort(function (a, b) { return (a.standing || 99) - (b.standing || 99) || (b.pf || 0) - (a.pf || 0); });
    h += '<article class="card" style="--i:' + i++ + '"><div class="row between"><h3 class="ttl">Standings</h3><small>top ' + lg.playoffTeams + " make playoffs</small></div><div class=\"scrollx\"><table class=\"tbl\"><thead><tr><th>#</th><th>Team</th><th class=\"n\">W-L</th><th class=\"n\">PF</th><th class=\"n\">Odds</th></tr></thead><tbody>" +
      st.map(function (x, idx) {
        var line = lg.playoffTeams && idx === lg.playoffTeams ? ' style="border-top:2px dashed var(--line2)"' : "";
        return '<tr class="' + (x.me ? "me" : "") + '" data-team="' + x.id + '"' + line + "><td>" + (x.standing || idx + 1) + "</td><td><b>" + esc(x.name) + "</b>" + (x.streak ? ' <small>' + esc(x.streak) + "</small>" : "") + '</td><td class="n">' + (rec(x) || "—") + '</td><td class="n">' + n0(x.pf) + '</td><td class="n">' + (x.po != null ? n0(x.po) + "%" : "—") + "</td></tr>";
      }).join("") + "</tbody></table></div><small>Odds = ESPN's own playoff simulation. Tap a team for its roster.</small></article>";
    // my roster
    h += '<article class="card" style="--i:' + i++ + '"><div class="row between"><h3 class="ttl">Your roster</h3><button class="btn sm" data-nav="moves" data-sub="lineup">Optimize</button></div>' + rosterSlots(lg, t, true) + "</article>";
    // settings
    var rules = (lg.rules || []).filter(function (r) { return ["PTD", "PY", "RY", "RTD", "REC", "REY", "RETD", "INTT", "FUML", "2PC"].indexOf(r.abbr) >= 0 || /TD|Yard|reception/i.test(r.label); }).slice(0, 10);
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">League settings</h3><div class="x"><div><div class="stack">' +
      '<div class="kvs"><div><b>' + lg.teamCount + "</b><small>teams</small></div><div><b>" + lg.playoffTeams + "</b><small>playoff spots</small></div><div><b>" + esc(lg.scoring.toUpperCase()) + "</b><small>scoring</small></div><div><b>" + (lg.waiverType === "faab" ? "FAAB $" + lg.faabBudget : "#" + (t.waiverRank || "?")) + "</small><small>" + (lg.waiverType === "faab" ? "budget" : "your waiver spot") + "</small></div></div>" +
      '<p class="sub">Starters: ' + startSlots(lg).map(function (s) { return SLOT_LABEL[s] || s; }).join(", ") + " · Bench " + (lg.slots.BE || 0) + " · IR " + (lg.slots.IR || 0) + "</p>" +
      (rules.length ? '<table class="tbl">' + rules.map(function (r) { return "<tr><td>" + esc(r.label) + '</td><td class="n"><b>' + sg(r.points, 1).replace(".0", "") + "</b></td></tr>"; }).join("") + "</table>" : "") +
      "</div></div></div>" + xbtn("Scoring & roster rules") + "</article>";
    // activity
    if (lg.activity && lg.activity.length) {
      h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">League activity</h3><div class="x"><div><ul class="news">' + lg.activity.slice(0, 15).map(function (a) {
        var d = a.at ? new Date(a.at) : null, when = d && !isNaN(d) ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : "";
        var k = a.kind === "FA ADDED" || a.kind === "WAIVER ADDED" ? "▲ Added" : a.kind === "DROPPED" ? "▼ Dropped" : a.kind.indexOf("TRADE") >= 0 ? "⇄ Traded" : esc(a.kind);
        return '<li><span class="who">' + esc(a.team || "") + "</span><p>" + k + " " + esc(a.player || "") + (a.bid ? " · $" + a.bid : "") + "</p><small>" + when + "</small></li>";
      }).join("") + "</ul></div></div>" + xbtn("Recent adds, drops, trades") + "</article>";
    }
    return h + "</div>";
  }
  function rosterSlots(lg, t, mine) {
    var g = bySlot(lg, t), used = {}, rows = "", max = Math.max.apply(null, roster(lg, t).map(wkVal).concat([1]));
    var moveIn = {}, moveOut = {};
    if (mine) C[lg.key].moves.forEach(function (a) { if (a.kind === "lineup" && /^Start /.test(a.title)) { moveIn[a.players[0]] = 1; moveOut[a.players[1]] = 1; } if (a.kind === "injury") moveOut[a.players[0]] = 1; });
    startSlots(lg).forEach(function (s, i) {
      var p = (g[s] || []).filter(function (q) { return !used[q.id]; })[0]; if (p) used[p.id] = 1;
      rows += slotRow(lg, s, p, max, i, moveIn, moveOut);
    });
    var bench = roster(lg, t).filter(function (p) { return !used[p.id] && p.slot !== "IR"; }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
    var ir = roster(lg, t).filter(function (p) { return p.slot === "IR"; });
    rows += bench.map(function (p, i) { return slotRow(lg, "BN", p, max, i + 9, moveIn, moveOut); }).join("") + ir.map(function (p, i) { return slotRow(lg, "IR", p, max, i + 20, moveIn, moveOut); }).join("");
    return '<div class="slots">' + rows + "</div>";
  }
  function slotRow(lg, s, p, max, i, moveIn, moveOut) {
    var k = SLOT_LABEL[s] || s, bn = s === "BN" || s === "IR";
    if (!p) return '<div class="slot"><span class="k">' + esc(k) + '</span><div class="n"><b class="dim">Empty</b></div><div class="p"><b class="sign dn">0</b></div></div>';
    var o = oppWord(p), v = wkVal(p), fin = p.played >= 100 && p.wpts != null;
    var mark = moveOut[p.n] ? '<span class="tag dn">▼ SIT</span>' : moveIn[p.n] ? '<span class="tag up">▲ START</span>' : "";
    return '<div class="slot' + (dead(p) ? " dead" : "") + '" data-player="' + esc(p.id) + '"><span class="k' + (bn ? " bn" : "") + '">' + esc(k) + '</span><div class="n"><b>' + esc(p.n) + "</b><small>" + posB(p.pos) + (o.w ? '<span class="sign ' + o.cls + '">' + o.g + "</span> " + esc(o.w) : esc(p.team || "")) + statusTag(p) + mark + '</small></div><div class="p"><b>' + (fin ? n1(p.wpts) : n1(v)) + '</b><span class="live' + (fin ? " on" : "") + '">' + (fin ? "FINAL" : dead(p) ? "0 PTS" : "PROJ") + "</span>" + bar(fin ? p.wpts : v, max, dead(p) ? "dn" : fin ? "up" : "", i) + "</div></div>";
  }

  function vPlayers() {
    var lg = L(), c = C[S.lg], i = 0, h = '<div class="v">';
    h += '<div class="search" style="--i:' + i++ + '"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg><input id="q" type="search" placeholder="Search players in ' + esc(lg.name) + '" value="' + esc(S.q) + '" autocomplete="off"></div>';
    h += '<div class="pills" style="--i:' + i++ + '">' + ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"].map(function (p) { return '<button class="pill" data-pill="pos:' + p + '" aria-pressed="' + (S.pos === p) + '">' + (p === "D/ST" ? "DST" : p === "ALL" ? "All" : p) + "</button>"; }).join("") + "</div>";
    h += '<div class="pills" style="--i:' + i++ + '">' + [["ALL", "Everyone"], ["FA", "Free agents"], ["ME", "My team"], ["OTH", "Other teams"]].map(function (x) { return '<button class="pill" data-pill="avail:' + x[0] + '" aria-pressed="' + (S.avail === x[0]) + '">' + x[1] + "</button>"; }).join("") +
      '<button class="pill" data-pill="fav:1" aria-pressed="' + S.favOnly + '">★ Favorites</button></div>';
    h += '<div class="row" style="--i:' + i++ + '"><select class="sel" id="sort" aria-label="Sort by">' + [["wk", "This week proj"], ["proj", "Season proj"], ["l3", "Last 3 games avg"], ["spts", "Season points"], ["use", "Usage (touches + targets)"], ["own", "% rostered"]].map(function (x) { return '<option value="' + x[0] + '"' + (S.sort === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; }).join("") + "</select></div>";
    var list = filteredPlayers(lg);
    var shown = list.slice(0, 40 + S.more * 40);
    h += '<article class="card" style="--i:' + i++ + ';padding-block:6px"><ul>' + (shown.length ? shown.map(function (p) { return playerRow(lg, p); }).join("") : '<li class="empty">No players match.</li>') + "</ul></article>";
    if (list.length > shown.length) h += '<button class="btn full" data-more>Show ' + Math.min(40, list.length - shown.length) + " more</button>";
    if (S.cmp.length) h += '<button class="btn pri full" data-sheet="compare" style="position:sticky;bottom:calc(96px + env(safe-area-inset-bottom,0px))">Compare ' + S.cmp.length + " player" + (S.cmp.length > 1 ? "s" : "") + "</button>";
    return h + "</div>";
  }
  function metric(p) {
    switch (S.sort) {
      case "proj": return [n0(p.proj), "season proj"];
      case "l3": return [n1(last3(p)), "last 3 avg"];
      case "spts": return [n1(p.spts), "season pts"];
      case "use": return [n1(usage3(p)), "touch+tgt /g"];
      case "own": return [p.own != null ? n0(p.own) + "%" : "—", "rostered"];
      default: return [n1(wkVal(p)), "wk proj"];
    }
  }
  function sortVal(p) {
    switch (S.sort) {
      case "proj": return p.proj || 0; case "l3": return last3(p) || 0; case "spts": return p.spts || 0;
      case "use": return usage3(p) || 0; case "own": return p.own || 0; default: return wkVal(p);
    }
  }
  function filteredPlayers(lg) {
    var q = S.q.trim().toLowerCase();
    return Object.keys(lg.players).map(function (pid) { return P(lg, pid); }).filter(function (p) {
      if (S.pos !== "ALL" && p.pos !== S.pos) return false;
      if (S.avail === "FA" && p.owner != null) return false;
      if (S.avail === "ME" && p.owner !== lg.myId) return false;
      if (S.avail === "OTH" && (p.owner == null || p.owner === lg.myId)) return false;
      if (S.favOnly && !S.favs[p.id]) return false;
      if (q && p.n.toLowerCase().indexOf(q) < 0 && (p.team || "").toLowerCase() !== q) return false;
      return true;
    }).sort(function (a, b) { return sortVal(b) - sortVal(a); });
  }
  function oppShort(p) {
    if (p.bye) return "bye"; if (!p.opp) return ""; var r = p.oppRank;
    return "vs " + p.opp + (r ? (r <= 8 ? " ▼ tough" : r >= 25 ? " ▲ soft" : " ● even") : "");
  }
  function playerRow(lg, p) {
    var m = metric(p), own = ownerWord(lg, p);
    return '<li class="pl' + (dead(p) ? " dead" : "") + '" data-player="' + esc(p.id) + '"><span class="mono">' + esc(initials(p.n)) + "</span>" + posB(p.pos) +
      '<span class="nm"><b>' + (S.favs[p.id] ? "★ " : "") + esc(p.n) + "</b><small>" + esc(p.team || "") + " · " + esc(own.length > 16 ? own.slice(0, 15) + "…" : own) + " · " + esc(oppShort(p)) + "</small></span>" + statusTag(p) +
      '<span class="val"><b>' + m[0] + "</b><small>" + m[1] + "</small></span></li>";
  }

  function vMoves() {
    var lg = L(), i = 0, h = '<div class="v">';
    h += '<div class="seg" style="--n:3;--x:' + ["lineup", "waivers", "trades"].indexOf(S.sub) + ';--i:' + i++ + '"><i></i>' + [["lineup", "Lineup"], ["waivers", "Waivers"], ["trades", "Trades"]].map(function (x) { return '<button data-sub="' + x[0] + '" aria-pressed="' + (S.sub === x[0]) + '">' + x[1] + "</button>"; }).join("") + "</div>";
    if (S.sub === "lineup") h += vLineup(lg, i); else if (S.sub === "waivers") h += vWaivers(lg, i); else h += vTrades(lg, i);
    return h + "</div>";
  }
  function vLineup(lg, i) {
    var c = C[lg.key], t = c.me, g = bySlot(lg, t), used = {}, h = "";
    var my = roster(lg, t), best = c.bestWeek, bestIds = {}; best.starters.forEach(function (s) { bestIds[s.p.id] = s.slot; });
    var gap = best.total - c.curWeek, sm = sem(gap, 1);
    h += '<article class="card' + (gap >= 2 ? " hi" : "") + '" style="--i:' + i++ + '"><div class="row between"><div><h3 class="ttl">Week ' + lg.week + " lineup</h3><p class=\"sub\">As set in ESPN vs best possible, by this week's projections.</p></div></div>" +
      '<div class="kvs"><div><b>' + n1(c.curWeek) + "</b><small>current proj</small></div><div><b>" + n1(best.total) + "</b><small>best possible</small></div><div><b class=\"sign " + sm.cls + '">' + sm.g + " " + sg(gap) + "</b><small>on the table</small></div></div>" +
      (gap < 1 ? '<p class="sub"><b>▲ Your lineup is already optimal.</b> Close calls below are judgment calls, not errors.</p>' : '<p class="sub"><b>◆ ' + n1(gap) + " points available</b> with the swaps marked below.</p>") + "</article>";
    var slots = startSlots(lg);
    slots.forEach(function (s) {
      var starter = (g[s] || []).filter(function (q) { return !used[q.id]; })[0]; if (starter) used[starter.id] = 1;
      var elig = my.filter(function (p) { return (p.slot === "BE") && (s === FLEX ? FLEXP[p.pos] : p.pos === s); }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
      var alt = elig[0], sv = starter ? wkVal(starter) : 0, av = alt ? wkVal(alt) : 0, d = av - sv;
      var verdict, vc, why;
      if (!starter) { verdict = "FILL"; vc = "dn"; why = "Empty slot scores zero."; }
      else if (dead(starter) && alt && av > 0) { verdict = "SWAP"; vc = "dn"; why = starter.n + " is " + (starter.bye ? "on bye" : "out") + " and scores 0."; }
      else if (!alt) { verdict = "KEEP"; vc = "up"; why = "No bench option for this spot."; }
      else if (d >= 3) { verdict = "SWAP"; vc = "up"; why = alt.n + " projects " + n1(d) + " more this week."; }
      else if (d >= 1.5) { verdict = "LEAN SWAP"; vc = "cau"; why = "Close call: " + alt.n + " projects " + n1(d) + " more."; }
      else if (d > -1.5) { verdict = "KEEP · CLOSE"; vc = "cau"; why = "Within " + n1(Math.abs(d)) + " points; either is fine."; }
      else { verdict = "KEEP"; vc = "up"; why = starter.n + " projects " + n1(-d) + " more than the best bench option."; }
      var key = "slot:" + lg.key + ":" + s + ":" + (starter ? starter.id : "x");
      var cmpT = function (p, label) {
        if (!p) return "";
        var o = oppWord(p);
        return '<div><div class="h">' + label + "</div><b>" + esc(p.n) + '</b><small>' + posB(p.pos) + " " + esc(p.team || "") + "</small>" +
          '<div class="delta"><span>This week</span><b class="n">' + n1(wkVal(p)) + "</b></div>" +
          '<div class="delta"><span>Opponent</span><b class="n sign ' + o.cls + '" style="font-size:12px">' + esc(o.w.replace(/^vs /, "")) + "</b></div>" +
          '<div class="delta"><span>Last 3 avg</span><b class="n">' + n1(last3(p)) + "</b></div>" +
          '<div class="delta"><span>Season avg</span><b class="n">' + n1(seasonAvg(p)) + "</b></div>" +
          '<div class="delta"><span>Status</span><b class="n" style="font-size:12px">' + (p.bye ? "BYE" : esc((p.inj || "Healthy").replace("_", " ")) || "Healthy") + "</b></div></div>";
      };
      h += '<article class="card" style="--i:' + i++ + '" data-key="' + esc(key) + '"><div class="row between"><div class="row"><span class="k lbl" style="color:var(--acc)">' + esc(SLOT_LABEL[s] || s) + '</span><span class="chip ' + vc + '">' + (vc === "up" ? "▲" : vc === "dn" ? "▼" : "◆") + " " + verdict + "</span></div>" + (alt && starter ? '<b class="sign ' + (verdict.indexOf("SWAP") === 0 ? "up" : vc === "cau" ? "cau" : "neu") + '">' + (verdict.indexOf("SWAP") === 0 ? sg(d) + " gain" : sg(-d) + " edge") + "</b>" : "") + "</div>" +
        '<ul class="stack" style="gap:0">' + (starter ? pline(lg, starter, { cls: verdict.indexOf("SWAP") === 0 ? "out ghost" : "", sub: "starting · " + esc(oppWord(starter).w || starter.team || "") }) : '<li class="pl"><span class="nm"><b class="dim">Empty</b></span></li>') +
        (alt ? pline(lg, alt, { cls: verdict.indexOf("SWAP") === 0 ? "in" : "", sign: verdict.indexOf("SWAP") === 0 ? "in" : null, sub: "bench · " + esc(oppWord(alt).w || alt.team || "") }) : "") + "</ul>" +
        '<p class="sub">' + esc(why) + "</p>" + (alt && starter ? '<div class="x"><div><div class="two">' + cmpT(starter, "Starter") + cmpT(alt, "Bench") + "</div></div></div>" + xbtn("Compare") : "") + "</article>";
    });
    return h;
  }
  var WCATS = [["ALL", "All"], ["start", "Starts for you"], ["week", "This-week fill-in"], ["hot", "Hot hand"], ["role", "Role growing"], ["stash", "Stash"], ["warn", "Buyer beware"]];
  var WWORD = { start: ["▲ Starts for you", "up"], week: ["▲ This-week fill-in", "up"], hot: ["▲ Hot hand", "up"], role: ["▲ Role growing", "up"], stash: ["● Stash", "neu"], warn: ["◆ Buyer beware", "cau"] };
  function vWaivers(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    h += '<div class="pills" style="--i:' + i++ + '">' + WCATS.map(function (x) { return '<button class="pill" data-pill="wcat:' + x[0] + '" aria-pressed="' + (S.wcat === x[0]) + '">' + x[1] + "</button>"; }).join("") + "</div>";
    h += '<p class="sub" style="--i:' + i++ + ';padding-inline:4px">Ranked for <b>your</b> roster and this league\'s rules. ' + (lg.waiverType === "faab" ? "FAAB budget $" + lg.faabBudget + "." : "You are #" + (t.waiverRank || "?") + " in the waiver order.") + "</p>";
    var list = c.scout.filter(function (x) { return S.wcat === "ALL" || x.cats.indexOf(S.wcat) >= 0; }).slice(0, 8);
    if (!list.length) h += '<div class="calm" style="--i:' + i++ + '"><b>Nothing worth a claim</b><span>Nobody available beats what you have.</span></div>';
    list.forEach(function (x) {
      var p = x.p, primary = x.cats[0], ww = WWORD[primary], o = oppWord(p);
      var why = primary === "start" ? p.n + " would start for you right now (" + sg(x.gs, 0) + " season lineup pts)." :
        primary === "week" ? "Fills a dead spot this week for " + sg(x.gw) + " projected." :
        primary === "hot" ? "Averaging " + n1(x.l3) + " over the last 3 vs " + n1(x.sa) + " on the season." :
        primary === "role" ? "Usage jumped: " + n0(x.lastU) + " touches/targets last game vs " + n1(x.prevU) + " before." :
        primary === "stash" ? "Projects near your worst " + p.pos + " starter; depth if injuries hit." :
        "Scoring (" + n1(x.l3) + "/g) on thin volume (" + n1(x.u3) + " touches+targets/g). Could be touchdown luck.";
      var fit = x.gs >= 3 ? ["▲ Starts for you", "up"] : x.gw >= 2 ? ["▲ Starts this week", "up"] : ["● Bench depth", "neu"];
      h += '<article class="card" style="--i:' + i++ + '" data-key="' + esc("fa:" + lg.key + ":" + p.id) + '"><div class="row between"><span class="chip ' + ww[1] + '">' + ww[0] + "</span>" + (x.cats.slice(1).map(function (k) { return '<span class="chip">' + WWORD[k][0].slice(2) + "</span>"; }).join(" ")) + "</div>" +
        '<ul class="stack" style="gap:0">' + pline(lg, p, { val: n1(wkVal(p)), unit: "wk proj" }) + "</ul>" +
        '<p class="sub"><b>Why:</b> ' + esc(why) + "</p>" +
        '<div class="kvs"><div><b class="sign ' + (x.gs >= 3 || x.gw >= 2 ? "up" : "neu") + '">' + (x.gs >= 3 ? sg(x.gs, 0) : x.gw >= 2 ? sg(x.gw) : "Bench") + "</b><small>" + (x.gs >= 3 ? "your lineup · szn" : x.gw >= 2 ? "your lineup · wk" : "fit for you") + "</small></div><div><b>" + n1(x.l3) + "</b><small>last 3 avg</small></div>" + (touchPos(p) ? "<div><b>" + n1(x.u3) + "</b><small>touch+tgt/g</small></div>" : "<div><b>" + n1(wkVal(p)) + "</b><small>this wk</small></div>") + "</div>" +
        '<div class="x"><div><div class="stack">' + spark(p) +
        '<div class="evidence"><span><b>Fit:</b> ' + fit[0] + " · " + (x.drop ? "drop <b>" + esc(x.drop.n) + "</b> (" + n0(x.drop.proj) + " season)" : "open roster spot") + "</span>" +
        '<span><b>Sustainable?</b> ' + (x.u3 != null ? (x.u3 >= 10 ? "▲ Yes, volume backs it (" + n1(x.u3) + "/g)" : x.u3 >= 5 ? "◆ Moderate volume (" + n1(x.u3) + "/g)" : "▼ Low volume (" + n1(x.u3) + "/g)") : "● Not enough games to say") + "</span>" +
        '<span><b>Matchup:</b> ' + (o.w ? o.g + " " + esc(o.w) : "—") + " · <b>Rostered:</b> " + (p.own != null ? n0(p.own) + "% of ESPN leagues" : "—") + "</span>" +
        '<span><b>Urgency:</b> ' + (x.gw >= 2 ? "▼ Before kickoff" : "◆ Before the next waiver run") + "</span>" +
        "</div></div></div></div>" + xbtn("Evidence") + "</article>";
    });
    return h;
  }
  function vTrades(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    var ideas = c.moves.filter(function (a) { return a.kind === "trade" || a.kind === "plan"; });
    h += '<section class="sec" style="--i:' + i++ + '"><h2>Ideas that help both sides</h2><small>' + ideas.length + "</small></section>";
    if (!ideas.length) h += '<div class="calm" style="--i:' + i++ + '"><b>No trade helps both sides right now</b><span>Ideas appear when a swap improves two starting lineups and looks fair on paper.</span></div>';
    ideas.forEach(function (a) { h += actionCard(a, i++, false); });
    if (c.alts.length) h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Other versions</h3><p class="sub">Same players, different partners or targets. Each helps both sides on paper.</p><div class="x"><div><div class="stack">' +
      c.alts.map(function (a) { var t = a.trade; return '<div class="delta"><span class="grow"><b>' + esc(names(t.send)) + '</b> for <b>' + esc(names(t.receive)) + '</b><br><small>' + esc(t.partner_name.trim()) + " · they " + sg(t.their_gain, 0) + '</small></span><b class="n sign up">' + sg(t.my_gain, 0) + "</b></div>"; }).join("") + "</div></div></div>" + xbtn(c.alts.length + " more ideas") + "</article>";
    // lab
    var others = lg.teams.filter(function (x) { return !x.me; });
    if (!S.tl.partner || !team(lg, S.tl.partner) || team(lg, S.tl.partner).me) S.tl.partner = others[0].id;
    var them = team(lg, S.tl.partner), my = roster(lg, t), theirs = roster(lg, them);
    h += '<section class="sec" style="--i:' + i++ + '"><h2>Trade lab</h2></section><article class="card" style="--i:' + i++ + '">' +
      '<select class="sel" id="tl-partner" aria-label="Trade partner">' + others.map(function (x) { return '<option value="' + x.id + '"' + (x.id === S.tl.partner ? " selected" : "") + ">" + esc(x.name) + (rec(x) ? " (" + rec(x) + ")" : "") + "</option>"; }).join("") + "</select>" +
      '<div class="opts"><button data-set="horizon:season" aria-pressed="' + (S.horizon === "season") + '">Rest of season</button><button data-set="horizon:week" aria-pressed="' + (S.horizon === "week") + '">This week</button></div>' +
      '<span class="lbl">You get</span><div class="chips">' + chips(lg, theirs, "get") + "</div>" +
      '<span class="lbl">You give</span><div class="chips">' + chips(lg, my, "send") + "</div>" +
      '<div id="tl-out">' + tradeVerdict(lg) + "</div></article>";
    return h;
  }
  function chips(lg, ps, side) {
    return ps.filter(function (p) { return p.proj != null; }).sort(function (a, b) { return b.proj - a.proj; }).map(function (p) {
      var on = !!S.tl[side][p.id], lock = side === "send" && S.untouch[p.id];
      return '<button class="pchip" data-tl="' + side + ":" + esc(p.id) + '" aria-pressed="' + on + '"' + (lock ? ' title="Untouchable"' : "") + ">" + posB(p.pos) + (lock ? "🔒 " : "") + esc(p.n) + "</button>";
    }).join("");
  }
  function tradeVerdict(lg) {
    var c = C[lg.key], t = c.me, them = team(lg, S.tl.partner), my = roster(lg, t), theirs = roster(lg, them);
    var get = theirs.filter(function (p) { return S.tl.get[p.id]; }), send = my.filter(function (p) { return S.tl.send[p.id]; });
    if (!get.length || !send.length) return '<div class="verdict"><p class="w">Pick players on both sides</p><p class="sub">Tap names above. Multiple per side is fine.</p></div>';
    var val = S.horizon === "week" ? wkVal : sVal, wk = S.horizon === "week";
    var b0 = bestLineup(my, lg.slots, val), b1 = bestLineup(swap(my, send, get), lg.slots, val);
    var t0 = lv(theirs, lg.slots, val), t1 = lv(swap(theirs, get, send), lg.slots, val);
    var mg = b1.total - b0.total, tg = t1 - t0;
    var rawS = send.reduce(function (s, p) { return s + (p.proj || 0); }, 0), rawG = get.reduce(function (s, p) { return s + (p.proj || 0); }, 0);
    var ratio = rawG ? rawS / rawG : 1;
    var take = wk ? S.takeAt / 4 : S.takeAt, win = wk ? 1 : 3, even = wk ? 1 : 3;
    var word, cls;
    if (mg >= take) { word = "▲ Take it"; cls = "up"; } else if (mg >= win) { word = "▲ Small win"; cls = "up"; } else if (mg > -even) { word = "◆ Even"; cls = "cau"; } else { word = "▼ Pass"; cls = "dn"; }
    var hurt = get.filter(HURT), byes = wk ? get.filter(function (p) { return p.bye; }) : [];
    var pos = ["QB", "RB", "WR", "TE"].map(function (ps) {
      var a = b0.starters.filter(function (s) { return s.p.pos === ps; }).reduce(function (s, x) { return s + x.v; }, 0), b = b1.starters.filter(function (s) { return s.p.pos === ps; }).reduce(function (s, x) { return s + x.v; }, 0);
      return { pos: ps, d: b - a };
    }).filter(function (x) { return Math.abs(x.d) >= 0.5; });
    var ids0 = {}; b0.starters.forEach(function (s) { ids0[s.p.id] = 1; }); var ids1 = {}; b1.starters.forEach(function (s) { ids1[s.p.id] = 1; });
    var depth = ["QB", "RB", "WR", "TE"].map(function (ps) { return { pos: ps, n: b1.bench.filter(function (p) { return p.pos === ps && p.slot !== "IR"; }).length }; }).filter(function (x) { return x.n === 0; });
    var fair = ratio >= 0.85 ? "▲ Looks fair on paper (" + ratio.toFixed(2) + "×)" : ratio >= 0.7 ? "◆ A bit light on paper (" + ratio.toFixed(2) + "×) — expect a counter" : "▼ Lopsided on paper (" + ratio.toFixed(2) + "×) — likely declined on sight";
    var conf = Math.abs(mg) >= (wk ? 6 : 30) ? "High" : Math.abs(mg) >= (wk ? 2 : 10) ? "Medium" : "Low";
    var confWhy = conf === "High" ? "far bigger than projection noise" : conf === "Medium" ? "real, but projections can miss by this much" : "inside projection noise; a coin flip on paper";
    var counter = "";
    if (mg < win) { var cc = bestCounter(lg, them, my, theirs, val); if (cc) counter = '<p class="sub"><b>Try instead:</b> ' + esc(cc.send.map(function (p) { return p.n; }).join(" + ")) + " for <b>" + esc(cc.get.n) + "</b> (you " + sg(cc.mg, 0) + ", them " + sg(cc.tg, 0) + ")</p>"; }
    var lineupList = function (b, ids) { return '<ul>' + b.starters.map(function (s) { var ch = !ids[s.p.id]; return '<li class="pl' + (ch ? " in" : "") + '" style="padding-block:4px"><span class="lbl" style="width:36px">' + esc(SLOT_LABEL[s.slot] || s.slot) + '</span><span class="nm"><b>' + esc(s.p.n) + "</b></span><span class=\"val\"><b>" + (wk ? n1(s.v) : n0(s.v)) + "</b></span></li>"; }).join("") + "</ul>"; };
    return '<div class="verdict ' + cls + ' pop"><p class="w">' + word + "</p>" +
      '<div class="kvs"><div><b class="sign ' + sem(mg, win).cls + '">' + sg(mg, wk ? 1 : 0) + "</b><small>your lineup</small></div><div><b class=\"sign " + sem(tg, win).cls + '">' + sg(tg, wk ? 1 : 0) + "</b><small>their lineup</small></div><div><b>" + n0(rawS) + " → " + n0(rawG) + "</b><small>raw value out → in</small></div></div>" +
      '<p class="sub">' + fair + "</p><p class=\"sub\"><b>" + conf + " confidence</b> · " + confWhy + "</p>" + (hurt.length ? '<p class="sub"><b>▼ Heads up:</b> ' + esc(hurt.map(function (p) { return p.n; }).join(", ")) + " is hurt right now.</p>" : "") + (byes.length ? '<p class="sub"><b>▼ Bye:</b> ' + esc(byes.map(function (p) { return p.n; }).join(", ")) + " sits this week.</p>" : "") +
      (pos.length ? '<div class="stack">' + pos.map(function (x) { var s = sem(x.d, 0.5); return '<div class="delta"><span>' + x.pos + " starters</span><b class=\"n sign " + s.cls + '">' + s.g + " " + sg(x.d, wk ? 1 : 0) + "</b></div>"; }).join("") + "</div>" : "") +
      (depth.length ? '<p class="sub"><b>◆ Depth:</b> no bench ' + esc(depth.map(function (x) { return x.pos; }).join("/")) + " left after this.</p>" : "") + counter +
      '<div class="x" id="tl-x"><div><div class="two"><div><div class="h">Starters before</div>' + lineupList(b0, ids1) + '</div><div><div class="h">Starters after</div>' + lineupList(b1, ids0) + "</div></div>" +
      '<p class="sub" style="padding-top:8px">Assumptions: ESPN ' + (wk ? "week " + lg.week : "rest-of-season") + " projections under this league's scoring; lineup = best starters each side could field. Highlighted rows change.</p></div></div>" +
      '<button class="xbtn" data-x="#tl-x" aria-expanded="false"><span>Before & after</span><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button></div>';
  }
  function bestCounter(lg, them, my, theirs, val) {
    var mine0 = lv(my, lg.slots, val), th0 = lv(theirs, lg.slots, val), best = null;
    var mine = my.filter(function (p) { return p.proj != null && !S.untouch[p.id]; }), packs = [];
    mine.forEach(function (p) { packs.push([p]); });
    for (var i = 0; i < mine.length; i++) for (var j = i + 1; j < mine.length; j++) packs.push([mine[i], mine[j]]);
    theirs.forEach(function (g) {
      if (g.proj == null || HURT(g)) return;
      packs.forEach(function (pk) {
        var raw = pk.reduce(function (s, p) { return s + (p.proj || 0); }, 0); if (raw < 0.7 * g.proj) return;
        var mg = lv(swap(my, pk, [g]), lg.slots, val) - mine0, tg = lv(swap(theirs, [g], pk), lg.slots, val) - th0;
        if (mg >= 5 && tg >= 12 && (!best || mg + tg > best.mg + best.tg)) best = { send: pk, get: g, mg: mg, tg: tg };
      });
    });
    return best;
  }

  function vInsights() {
    var lg = L(), c = C[S.lg], t = c.me, i = 0, h = '<div class="v">';
    // strength
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Where you\'re strong</h3><p class="sub">Your starters\' season projection vs the league median, by position.</p><div class="stack">' +
      Object.keys(lg.strength || {}).map(function (pos, k) { var s = lg.strength[pos], sm = sem(s.edge, 12), word = s.edge > 12 ? "Strong" : s.edge < -12 ? "Thin" : "Average"; return '<div class="delta">' + posB(pos) + '<span class="grow" style="display:flex;gap:8px;align-items:center">' + bar(s.mine, Math.max(s.mine, s.median) * 1.1, sm.cls === "cau" ? "neu" : sm.cls, k) + "</span><b class=\"n sign " + sm.cls + '">' + sm.g + " " + sg(s.edge, 0) + "</b><small style=\"grid-column:1/-1\">" + word + " · you " + n0(s.mine) + " vs median " + n0(s.median) + "</small></div>"; }).join("") + "</div></article>";
    // proj vs reality
    var my = roster(lg, t).filter(function (p) { return playedRows(p).length >= 3 && p.pavg; }).map(function (p) { return { p: p, d: seasonAvg(p) - p.pavg }; }).sort(function (a, b) { return b.d - a.d; });
    if (my.length) h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Projection vs reality</h3><p class="sub">Your players\' actual average vs ESPN\'s projected average, games played only.</p><div class="x"><div><div class="stack">' +
      my.map(function (x) { var s = sem(x.d, 2); return '<div class="delta">' + posB(x.p.pos) + '<span class="grow"><b>' + esc(x.p.n) + "</b><br><small>" + n1(seasonAvg(x.p)) + " actual · " + n1(x.p.pavg) + " projected</small></span><b class=\"n sign " + s.cls + '">' + s.g + " " + sg(x.d) + "</b></div>"; }).join("") + "</div></div></div>" + xbtn("All " + my.length + " players") + "</article>";
    // risers & fallers
    var tr = function (x) { var s = sem(x.d, 2); return '<div class="delta" data-player="' + esc(x.p.id) + '">' + posB(x.p.pos) + '<span class="grow"><b>' + esc(x.p.n) + "</b><br><small>" + esc(ownerWord(lg, x.p)) + " · last 3: " + n1(x.l3) + " vs season " + n1(x.sa) + "</small></span><b class=\"n sign " + s.cls + '">' + s.g + " " + sg(x.d) + "</b></div>"; };
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Risers & fallers</h3><p class="sub">Last 3 games vs season average, everyone in this league (3+ games).</p><span class="lbl">Rising</span><div class="stack">' + c.risers.map(tr).join("") + '</div><span class="lbl">Falling</span><div class="stack">' + c.fallers.map(tr).join("") + "</div></article>";
    // byes ahead
    var weeks = []; for (var w = lg.week; w <= (lg.regWeeks || 14); w++) weeks.push(w);
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Bye weeks ahead</h3><p class="sub">How many of your current starters sit each week.</p><div class="wkgrid">' +
      weeks.map(function (w) { var n = (c.byes[w] || []).length; return '<div class="' + (w === lg.week ? "now" : "") + (n >= 2 ? " bad" : "") + '"><span>W' + w + "</span><b>" + (n || "–") + "</b></div>"; }).join("") + "</div>" +
      '<div class="x"><div><div class="stack">' + Object.keys(c.byes).sort(function (a, b) { return a - b; }).map(function (w) { return '<div class="delta"><span class="lbl">Week ' + w + "</span><span class=\"grow\"><b>" + esc(c.byes[w].map(function (p) { return p.n; }).join(", ")) + "</b></span></div>"; }).join("") + "</div></div></div>" + xbtn("Who sits when") + "</article>";
    // schedule ahead
    if (c.ahead.length) h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Next up</h3><p class="sub">Opponents ranked by their best possible lineup.</p><div class="stack">' + c.ahead.map(function (a) { return '<div class="delta"><span class="lbl">Wk ' + a.w + '</span><span class="grow"><b>' + esc(a.t.name) + "</b><br><small>" + (rec(a.t) || "") + " · " + ord(a.rank) + " strongest roster</small></span><span class=\"chip " + a.cls + '">' + (a.cls === "dn" ? "▼" : a.cls === "up" ? "▲" : "●") + " " + a.word + "</span></div>"; }).join("") + "</div></article>";
    // depth
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Roster depth</h3><p class="sub">Best bench option vs your weakest starter, season projection.</p><div class="stack">' + c.depth.map(function (d) { return '<div class="delta">' + posB(d.pos) + '<span class="grow"><b>' + d.starters + " starting · " + d.bench + " on bench</b><br><small>bench best " + n0(d.best) + " vs weakest starter " + n0(d.worst) + "</small></span><span class=\"chip " + d.cls + '">' + (d.cls === "dn" ? "▼" : d.cls === "up" ? "▲" : "◆") + " " + d.word + "</span></div>"; }).join("") + "</div></article>";
    // trade targets
    var mySur = (lg.market || []).filter(function (m) { return m.teamId === lg.myId; })[0], fits = (lg.market || []).filter(function (m) { return m.teamId !== lg.myId && m.needs.length; });
    h += '<article class="card" style="--i:' + i++ + '"><h3 class="ttl">Who needs what</h3><p class="sub">Needs = a starting spot in the league\'s bottom quarter. Surplus = a benched player good enough to start elsewhere.</p>' + (mySur && mySur.surplus.length ? '<p class="sub"><b>Your surplus:</b> ' + esc(mySur.surplus.map(function (s) { return s.name + " (" + s.pos + ")"; }).join(", ")) + "</p>" : '<p class="sub"><b>Your surplus:</b> none — every good player of yours already starts.</p>') +
      '<div class="x"><div><div class="stack">' + fits.map(function (m) { var tt = team(lg, m.teamId); var fit = mySur ? mySur.surplus.filter(function (s) { return m.needs.indexOf(s.pos) >= 0; }) : []; return '<div class="delta"><span class="grow"><b>' + esc(tt ? tt.name : "") + "</b><br><small>needs " + m.needs.join(", ") + (m.surplus.length ? " · spare " + esc(m.surplus.map(function (s) { return s.name; }).join(", ")) : "") + "</small></span>" + (fit.length ? '<span class="chip up">▲ Fit</span>' : '<span class="chip">●</span>') + "</div>"; }).join("") + "</div></div></div>" + xbtn("Every team") + "</article>";
    return h + "</div>";
  }

  /* ---------------- sheets ---------------- */
  function sheet(html) { return '<div class="scrim" data-close></div><div class="sheet" role="dialog" aria-modal="true">' + html + "</div>"; }
  function playerSheet(pid) {
    var lg = L(), p = P(lg, pid); if (!p) return "";
    var o = oppWord(p), l3 = last3(p), sa = seasonAvg(p), rows = playedRows(p), u3 = usage3(p), last = rows[rows.length - 1];
    var why = [];
    if (l3 != null && sa != null && rows.length >= 3) { var d = l3 - sa; if (d >= 3) why.push("▲ Trending up: " + n1(l3) + " last 3 vs " + n1(sa) + " season avg."); else if (d <= -3) why.push("▼ Trending down: " + n1(l3) + " last 3 vs " + n1(sa) + " season avg."); }
    if (p.pavg && sa != null && rows.length >= 3) { var r = sa - p.pavg; if (r >= 2) why.push("▲ Beating projection by " + n1(r) + "/g."); else if (r <= -2) why.push("▼ Under projection by " + n1(-r) + "/g."); }
    if (last && rows.length > 1) { var pu = avg(rows.slice(0, -1).map(touches)); if (touches(last) >= 1.4 * pu && touches(last) >= 8) why.push("▲ Usage jumped last game: " + n0(touches(last)) + " vs " + n1(pu) + " before."); }
    if (o.cls === "up") why.push("▲ Soft matchup this week (" + ord(p.oppRank) + " vs " + p.pos + ")."); if (o.cls === "dn" && !p.bye) why.push("▼ Tough matchup this week (" + ord(p.oppRank) + " vs " + p.pos + ").");
    if (p.bye) why.push("▼ On bye this week."); if (HURT(p)) why.push("▼ Listed " + (p.inj || "").replace("_", " ").toLowerCase() + "."); if (Q(p)) why.push("◆ Questionable — check Sunday morning.");
    if ((p.byes || []).some(function (w) { return w > lg.week; })) why.push("● Bye still ahead: week " + p.byes.filter(function (w) { return w > lg.week; }).join(", ") + ".");
    if (!why.length) why.push("● Steady: nothing unusual in the numbers.");
    var fit = "";
    if (p.owner == null) { var c = C[lg.key], my = roster(lg, c.me); var gs = lv(my.concat([p]), lg.slots, sVal) - c.bestSeason.total, gw = lv(my.concat([p]), lg.slots, wkVal) - c.bestWeek.total; fit = '<div class="kvs"><div><b class="sign ' + (gs > 0 ? "up" : "neu") + '">' + sg(gs, 0) + "</b><small>your lineup, season</small></div><div><b class=\"sign " + (gw > 0 ? "up" : "neu") + '">' + sg(gw) + "</b><small>your lineup, this wk</small></div></div>"; }
    var usage = last ? [last.tgt != null ? n0(last.tgt) + " tgt" : "", last.car != null ? n0(last.car) + " car" : "", last.rec != null ? n0(last.rec) + " rec" : "", last.att != null ? n0(last.att) + " att" : ""].filter(Boolean).join(" · ") : "";
    return sheet('<div class="row"><span class="mono">' + esc(initials(p.n)) + '</span><div class="grow"><h3 class="ttl lg">' + esc(p.n) + '</h3><p class="sub">' + posB(p.pos) + " " + esc(p.team || "") + " · " + esc(ownerWord(lg, p)) + "</p></div>" + statusTag(p) + "</div>" +
      '<div class="kvs"><div><b>' + n1(wkVal(p)) + "</b><small>wk " + lg.week + " proj</small></div><div><b>" + n0(p.proj) + "</b><small>season proj</small></div><div><b>" + n1(p.spts) + "</b><small>season pts</small></div><div><b>" + n1(l3) + "</b><small>last 3 avg</small></div></div>" +
      (o.w ? '<p class="sub"><b class="sign ' + o.cls + '">' + o.g + "</b> " + esc(o.w) + (p.kick ? " · " + esc(fmtKick(p.kick)) : "") + "</p>" : "") +
      '<div class="stack">' + spark(p, 300) + "</div>" +
      (usage ? '<p class="sub"><b>Last game:</b> ' + esc(usage) + (u3 != null ? " · <b>last 3:</b> " + n1(u3) + " touches+targets/g" : "") + "</p>" : "") +
      '<div class="stack"><span class="lbl">Read</span>' + why.map(function (w) { return '<p class="sub"><b>' + esc(w) + "</b></p>"; }).join("") + "</div>" + fit +
      '<div class="kvs"><div><b>' + (p.posRank ? "#" + p.posRank : "—") + "</b><small>" + esc(p.pos) + " rank (ESPN)</small></div><div><b>" + (p.own != null ? n0(p.own) + "%" : "—") + "</b><small>rostered</small></div><div><b>" + ((p.byes || []).join(", ") || "—") + "</b><small>bye week</small></div></div>" +
      '<div class="row"><button class="btn star grow" data-fav="' + esc(p.id) + '" aria-pressed="' + !!S.favs[p.id] + '"><svg viewBox="0 0 24 24"><path d="M12 3l2.8 5.9 6.4.8-4.7 4.5 1.2 6.4L12 17.5 6.3 20.6l1.2-6.4L2.8 9.7l6.4-.8z"/></svg>' + (S.favs[p.id] ? "Favorited" : "Favorite") + '</button><button class="btn grow" data-cmp="' + esc(p.id) + '" aria-pressed="' + (S.cmp.indexOf(p.id) >= 0) + '">' + (S.cmp.indexOf(p.id) >= 0 ? "In compare" : "Compare") + "</button></div>" +
      '<button class="btn full" data-close>Close</button>');
  }
  function compareSheet() {
    var lg = L(), ps = S.cmp.map(function (id) { return P(lg, id); }).filter(Boolean);
    if (ps.length < 2) return sheet('<h3 class="ttl">Compare</h3><p class="sub">Pick two players from the list (open a player, tap Compare).</p><button class="btn full" data-close>Close</button>');
    var a = ps[0], b = ps[1];
    var rows = [["This week proj", wkVal(a), wkVal(b), 1], ["Season proj", a.proj, b.proj, 0], ["Season pts", a.spts, b.spts, 1], ["Last 3 avg", last3(a), last3(b), 1], ["Season avg", seasonAvg(a), seasonAvg(b), 1], ["Touch+tgt /g", usage3(a), usage3(b), 1]];
    return sheet('<h3 class="ttl">Compare</h3><div class="two"><div><div class="h">A</div><b>' + esc(a.n) + "</b><small>" + posB(a.pos) + " " + esc(a.team || "") + " · " + esc(ownerWord(lg, a)) + "</small>" + statusTag(a) + '</div><div><div class="h">B</div><b>' + esc(b.n) + "</b><small>" + posB(b.pos) + " " + esc(b.team || "") + " · " + esc(ownerWord(lg, b)) + "</small>" + statusTag(b) + "</div></div>" +
      '<div class="stack">' + rows.map(function (r) { var fa = r[3] ? n1(r[1]) : n0(r[1]), fb = r[3] ? n1(r[2]) : n0(r[2]); return '<div class="stack" style="gap:3px"><span class="lbl">' + r[0] + "</span>" + cmpBars(r[1] || 0, r[2] || 0, fa, fb) + "</div>"; }).join("") + "</div>" +
      '<p class="sub">Left bar = A, right bar = B. Longer is more. Matchup: A ' + esc(oppWord(a).w || "—") + " · B " + esc(oppWord(b).w || "—") + ".</p>" +
      '<div class="row"><button class="btn grow" data-cmp-clear>Clear</button><button class="btn pri grow" data-close>Close</button></div>');
  }
  function teamSheet(id) {
    var lg = L(), t = team(lg, id); if (!t) return "";
    var c = C[lg.key], r = c.rankByProj.indexOf(t.id) + 1;
    return sheet('<h3 class="ttl lg">' + esc(t.name) + '</h3><p class="sub">' + esc(t.owner || "") + (rec(t) ? " · <b>" + rec(t) + "</b>" : "") + " · " + ord(r) + " strongest roster · " + n0(t.pf) + " PF</p>" + rosterSlots(lg, t, false) + '<button class="btn full" data-close>Close</button>');
  }
  function settingsSheet() {
    var lg = L(), my = roster(lg, C[lg.key].me);
    var opt = function (field, vals, cur) { return '<div class="opts">' + vals.map(function (v) { return '<button data-set="' + field + ":" + v[0] + '" aria-pressed="' + (cur === v[0]) + '">' + v[1] + "</button>"; }).join("") + "</div>"; };
    return sheet('<h3 class="ttl lg">Settings</h3>' +
      '<div class="stack"><span class="lbl">Default league</span>' + opt("lg", LGS.map(function (k) { return [k, D.leagues[k].name]; }), S.lg) + "</div>" +
      '<div class="stack"><span class="lbl">Accent</span>' + opt("accent", [["sky", "Sky"], ["violet", "Violet"], ["mint", "Mint"]], S.accent) + "</div>" +
      '<div class="stack"><span class="lbl">Density</span>' + opt("density", [["comfortable", "Comfortable"], ["compact", "Compact"]], S.density) + "</div>" +
      '<div class="stack"><span class="lbl">Motion</span>' + opt("motion", [["auto", "On"], ["off", "Off"]], S.motion) + "</div>" +
      '<div class="toggle"><div class="t"><b>"Take it" bar</b><small>Season lineup points a trade must add</small></div><input class="num" id="takeAt" type="number" min="0" max="60" value="' + esc(S.takeAt) + '"></div>' +
      '<div class="stack"><span class="lbl">Untouchables (never offered)</span><div class="chips">' + my.filter(function (p) { return p.proj; }).sort(function (a, b) { return b.proj - a.proj; }).map(function (p) { return '<button class="pchip" data-untouch="' + esc(p.id) + '" aria-pressed="' + !!S.untouch[p.id] + '">' + posB(p.pos) + esc(p.n) + "</button>"; }).join("") + "</div></div>" +
      '<div class="stack"><span class="lbl">Data</span><p class="sub">' + LGS.map(function (k) { return esc(D.leagues[k].name) + " pulled " + esc(ago(D.leagues[k].pulledAt).txt); }).join(" · ") + ". ESPN is pulled twice a day on GitHub (about 12:20 and 23:20 UTC) and this page is republished about 30 minutes later. The page itself cannot refresh ESPN.</p></div>" +
      '<div class="row"><button class="btn grow" data-reset>Clear done & favorites</button><button class="btn pri grow" data-close>Done</button></div>');
  }

  /* ---------------- shell ---------------- */
  function header() {
    var latest = LGS.map(function (k) { return D.leagues[k].pulledAt; }).filter(Boolean).sort().pop() || D.generated;
    var f = ago(latest), week = D.week;
    var showSeg = S.tab !== "home" && LGS.length > 1;
    return '<div class="brand"><div class="logo"><i>TyG</i><div>Command Center<small>' + esc(D.season) + " · Week " + esc(week) + '</small></div></div>' +
      '<div class="fresh"><span>Updated<br><b>' + esc(f.txt) + '</b></span><i class="' + f.cls + '" title="Data freshness"></i></div>' +
      '<button class="icobtn" data-sheet="settings" aria-label="Settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></button></div>' +
      (showSeg ? '<div class="seg" style="--n:' + LGS.length + ";--x:" + LGS.indexOf(S.lg) + '"><i></i>' + LGS.map(function (k) { var n = C[k].todo.filter(function (a) { return !S.done[a.key]; }).length; return '<button data-lg="' + k + '" aria-pressed="' + (S.lg === k) + '">' + esc(D.leagues[k].name) + (n ? '<i class="n">' + n + "</i>" : '<i class="n ok">✓</i>') + "</button>"; }).join("") + "</div>" : "");
  }
  function nav() {
    var icons = { home: '<path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/>', leagues: '<path d="M8 21h8M12 17v4M6 4h12v4a6 6 0 01-12 0zM6 6H3v2a3 3 0 003 3M18 6h3v2a3 3 0 01-3 3"/>', players: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>', moves: '<path d="M7 7h12l-3-3M17 17H5l3 3"/>', insights: '<path d="M4 19h16M7 15l4-5 3 3 5-7"/>' };
    return '<i></i>' + TABS.map(function (t) { return '<button data-nav="' + t + '" aria-pressed="' + (S.tab === t) + '"><svg viewBox="0 0 24 24">' + icons[t] + "</svg>" + t[0].toUpperCase() + t.slice(1) + "</button>"; }).join("");
  }
  function render(soft) {
    document.documentElement.dataset.accent = S.accent; document.documentElement.dataset.density = S.density; document.documentElement.dataset.motion = S.motion;
    $("#top").innerHTML = header();
    var main = $("#main"); main.classList.toggle("soft", !!soft);
    main.innerHTML = S.tab === "home" ? vHome() : S.tab === "leagues" ? vLeagues() : S.tab === "players" ? vPlayers() : S.tab === "moves" ? vMoves() : vInsights();
    $("#nav").innerHTML = nav(); $("#nav > i").style.setProperty("--x", TABS.indexOf(S.tab));
    $("#sheet").innerHTML = S.sheet ? (S.sheet === "settings" ? settingsSheet() : S.sheet === "compare" ? compareSheet() : S.sheet.indexOf("team:") === 0 ? teamSheet(+S.sheet.slice(5)) : S.sheet.indexOf("player:") === 0 ? playerSheet(S.sheet.slice(7)) : "") : "";
    document.body.style.overflow = S.sheet ? "hidden" : "";
    if (!soft) countUp();
  }
  function countUp() {
    if (MOTION_OFF()) return;
    $$("[data-count]").forEach(function (el) {
      var n = parseFloat(el.dataset.count); if (isNaN(n) || !n) return; var t0 = null, dec = el.dataset.count.indexOf(".") >= 0 ? 1 : 0;
      function step(t) { if (!t0) t0 = t; var p = Math.min(1, (t - t0) / 700), v = n * (1 - Math.pow(1 - p, 3)); el.textContent = v.toFixed(dec); if (p < 1) requestAnimationFrame(step); }
      el.textContent = (0).toFixed(dec); requestAnimationFrame(step);
    });
  }
  var toastT;
  function toast(h, s) { var t = $("#toast"); t.innerHTML = esc(h) + (s ? "<small>" + esc(s) + "</small>" : ""); t.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove("on"); }, 2600); }
  function go(tab, extra) { S.tab = tab; put("tab", tab); if (extra && extra.sub) { S.sub = extra.sub; put("sub", S.sub); } if (extra && extra.lg) { S.lg = extra.lg; put("lg", S.lg); } S.sheet = null; render(); window.scrollTo(0, 0); }

  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-nav],[data-sub],[data-lg],[data-x],[data-done],[data-copy],[data-fav],[data-cmp],[data-cmp-clear],[data-player],[data-team],[data-sheet],[data-close],[data-set],[data-pill],[data-more],[data-tl],[data-untouch],[data-reset]");
    if (!el) return;
    var ds = el.dataset;
    if (ds.close != null) { S.sheet = null; render(true); return; }
    if (ds.sheet) { S.sheet = ds.sheet; render(true); return; }
    if (ds.nav) { go(ds.nav, { sub: ds.sub, lg: ds.lg }); return; }
    if (ds.sub) { S.sub = ds.sub; put("sub", S.sub); render(); return; }
    if (ds.lg) { if (S.lg !== ds.lg) { S.lg = ds.lg; put("lg", S.lg); S.cmp = []; S.tl = { partner: null, get: {}, send: {} }; render(); window.scrollTo(0, 0); } return; }
    if (ds.x != null) {
      var box = ds.x ? $(ds.x) : el.closest(".card, .verdict"), open;
      if (ds.x) { box.classList.toggle("open"); open = box.classList.contains("open"); }
      else { box.classList.toggle("open"); open = box.classList.contains("open"); if (box.dataset.key) { if (open) S.open[box.dataset.key] = 1; else delete S.open[box.dataset.key]; } }
      el.setAttribute("aria-expanded", String(open)); return;
    }
    if (ds.done) { if (S.done[ds.done]) delete S.done[ds.done]; else S.done[ds.done] = 1; put("done", S.done); var card = el.closest(".card"); card.classList.toggle("done", !!S.done[ds.done]); el.setAttribute("aria-pressed", String(!!S.done[ds.done])); if (S.done[ds.done]) toast("Marked done", "Make the move in the ESPN app."); $("#top").innerHTML = header(); return; }
    if (ds.copy) { var txt = (window.FCC_MSG || {})[ds.copy] || ""; var ok = function () { toast("Copied", txt); }, no = function () { toast("Copy this", txt); }; try { navigator.clipboard.writeText(txt).then(ok, no); } catch (err) { no(); } return; }
    if (ds.fav) { if (S.favs[ds.fav]) delete S.favs[ds.fav]; else S.favs[ds.fav] = 1; put("favs", S.favs); toast(S.favs[ds.fav] ? "Added to favorites" : "Removed from favorites"); render(true); return; }
    if (ds.cmp) { var ix = S.cmp.indexOf(ds.cmp); if (ix >= 0) S.cmp.splice(ix, 1); else { S.cmp.push(ds.cmp); if (S.cmp.length > 2) S.cmp.shift(); } if (S.cmp.length === 2) S.sheet = "compare"; else toast("Added to compare", "Pick one more player."); render(true); return; }
    if (ds.cmpClear != null) { S.cmp = []; S.sheet = null; render(true); return; }
    if (ds.player) { if (e.target.closest("[data-x],[data-done],[data-copy]")) return; S.sheet = "player:" + ds.player; render(true); return; }
    if (ds.team) { S.sheet = "team:" + ds.team; render(true); return; }
    if (ds.set) { var kv = ds.set.split(":"), f = kv[0], v = kv[1]; S[f] = v; if (["lg", "accent", "density", "motion"].indexOf(f) >= 0) put(f, v); if (f === "horizon") { $("#tl-out").innerHTML = tradeVerdict(L()); $$("[data-set^='horizon:']").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.set === "horizon:" + v)); }); return; } render(true); return; }
    if (ds.pill) { var pk = ds.pill.split(":"); if (pk[0] === "fav") S.favOnly = !S.favOnly; else S[pk[0]] = pk[1]; S.more = 0; render(true); return; }
    if (ds.more != null) { S.more++; render(true); return; }
    if (ds.tl) { var sp = ds.tl.split(":"), side = sp[0], id = sp.slice(1).join(":"); if (side === "send" && S.untouch[id]) { toast("Untouchable", "Change that in Settings if you mean it."); return; } if (S.tl[side][id]) delete S.tl[side][id]; else S.tl[side][id] = 1; el.setAttribute("aria-pressed", String(!!S.tl[side][id])); $("#tl-out").innerHTML = tradeVerdict(L()); return; }
    if (ds.untouch) { if (S.untouch[ds.untouch]) delete S.untouch[ds.untouch]; else S.untouch[ds.untouch] = 1; put("untouch", S.untouch); el.setAttribute("aria-pressed", String(!!S.untouch[ds.untouch])); return; }
    if (ds.reset != null) { S.done = {}; S.favs = {}; S.cmp = []; put("done", S.done); put("favs", S.favs); toast("Cleared"); render(true); return; }
  });
  document.addEventListener("input", function (e) {
    if (e.target.id === "q") { S.q = e.target.value; S.more = 0; var lg = L(), list = filteredPlayers(lg).slice(0, 40); var ul = $("#main .card ul"); if (ul) ul.innerHTML = list.length ? list.map(function (p) { return playerRow(lg, p); }).join("") : '<li class="empty">No players match.</li>'; }
    if (e.target.id === "takeAt") { S.takeAt = +e.target.value || 0; put("takeAt", S.takeAt); }
  });
  document.addEventListener("change", function (e) {
    if (e.target.id === "sort") { S.sort = e.target.value; S.more = 0; render(true); }
    if (e.target.id === "tl-partner") { S.tl = { partner: +e.target.value, get: {}, send: {} }; render(true); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && S.sheet) { S.sheet = null; render(true); } if (e.key === "Enter" && e.target.matches && e.target.matches("[role=button]")) e.target.click(); });

  render();
})();
