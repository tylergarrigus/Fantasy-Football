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
    accent: get("accent", "sky"), motion: get("motion", "auto"), takeAt: get("takeAt", 10),
    q: "", pos: "ALL", avail: "ALL", sort: "wk", more: 0, favOnly: false,
    wcat: "ALL", horizon: "season", tl: { partner: null, get: {}, send: {} }, sheet: null, cmp: [], open: {}
  };
  if (LGS.indexOf(S.lg) < 0) S.lg = LGS[0];
  var TABS = ["home", "leagues", "players", "moves", "insights"];
  if (TABS.indexOf(S.tab) < 0) S.tab = "home";
  function motionOff() { return S.motion === "off" || (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches); }

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
  function rec(t) { return t && t.w != null ? t.w + "-" + t.l + (t.t ? "-" + t.t : "") : ""; }
  function ord(n) { var s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function ago(iso) {
    var t = Date.parse(iso); if (isNaN(t)) return { txt: "?", cls: "stale" };
    var m = Math.round((Date.now() - t) / 60000);
    var txt = m < 2 ? "now" : m < 60 ? m + "m" : m < 36 * 60 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d";
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
  function stTag(p) {
    if (p.bye) return '<span class="st dn">▼ Bye</span>';
    var s = (p.inj || "").toUpperCase();
    if (HURT(p)) return '<span class="st dn">▼ ' + esc(s === "INJURY_RESERVE" ? "IR" : s === "DOUBTFUL" ? "Dbt" : s.slice(0, 3)) + "</span>";
    if (Q(p)) return '<span class="st cau">◆ Q</span>';
    return "";
  }
  function opp(p) {
    if (p.bye) return { w: "Bye", cls: "dn", g: "▼" };
    if (!p.opp) return { w: "", cls: "neu", g: "" };
    var r = p.oppRank;
    if (!r) return { w: "vs " + p.opp, cls: "neu", g: "" };
    if (r <= 8) return { w: "vs " + p.opp + " · tough", cls: "dn", g: "▼", r: r };
    if (r >= 25) return { w: "vs " + p.opp + " · soft", cls: "up", g: "▲", r: r };
    return { w: "vs " + p.opp, cls: "neu", g: "", r: r };
  }
  function oppHtml(p) { var o = opp(p); return o.w ? '<span class="' + o.cls + '">' + (o.g ? o.g + " " : "") + esc(o.w) + "</span>" : ""; }
  function sem(d, close) {
    close = close == null ? 1.5 : close;
    if (d == null || isNaN(d)) return { cls: "neu", g: "●" };
    if (Math.abs(d) < close) return { cls: "cau", g: "◆" };
    return d > 0 ? { cls: "up", g: "▲" } : { cls: "dn", g: "▼" };
  }
  function posT(pos) { var k = pos === "D/ST" ? "DST" : pos; return '<span class="pos ' + esc(k) + '">' + esc(k) + "</span>"; }
  function initials(n) { var w = n.replace(/ D\/ST$/, "").split(" "); return (w[0][0] || "") + (w.length > 1 ? w[w.length - 1][0] : ""); }
  function pic(p, cls) { cls = cls || ""; var src = (D.photos || {})[p.eid]; return src ? '<img class="ph ' + cls + '" src="' + src + '" alt="" loading="lazy">' : '<span class="ph ini ' + cls + '">' + esc(initials(p.n)) + "</span>"; }
  function ownerWord(lg, p) { if (p.owner == null) return "FA"; var t = team(lg, p.owner); return t ? (t.me ? "You" : t.name.trim()) : ""; }
  function fmtKick(iso) { if (!iso) return ""; var d = new Date(iso); if (isNaN(d)) return ""; return d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
  function names(ps) { return ps.map(function (p) { return p.name || p.n; }).join(" + "); }
  function shortName(n) { return /D\/ST$/.test(n) ? n : n.replace(/^(\w)\S* /, "$1. "); }
  function byName(lg, n) { if (!lg._byName) { lg._byName = {}; Object.keys(lg.players).forEach(function (pid) { lg._byName[lg.players[pid].n] = lg.players[pid]; }); } return lg._byName[n]; }

  /* ---------------- lineup math (mirrors ff/engines/trades.best_lineup) ---------------- */
  var FIXED = ["QB", "RB", "WR", "TE", "K", "D/ST"], FLEX = "RB/WR/TE", FLEXP = { RB: 1, WR: 1, TE: 1 };
  function bestLineup(players, slots, val) {
    var pool = players.slice().sort(function (a, b) { return val(b) - val(a); });
    var used = {}, starters = [], total = 0, unfilled = [];
    FIXED.forEach(function (pos) {
      var need = +(slots[pos] || 0);
      for (var i = 0; i < pool.length && need > 0; i++) { var p = pool[i]; if (used[p.id] || p.pos !== pos) continue; used[p.id] = 1; starters.push({ p: p, slot: pos, v: val(p) }); total += val(p); need--; }
      for (var k = 0; k < need; k++) unfilled.push(pos);
    });
    var flex = +(slots[FLEX] || 0);
    for (var j = 0; j < pool.length && flex > 0; j++) { var q = pool[j]; if (used[q.id] || !FLEXP[q.pos]) continue; used[q.id] = 1; starters.push({ p: q, slot: "FLEX", v: val(q) }); total += val(q); flex--; }
    for (var m = 0; m < flex; m++) unfilled.push("FLEX");
    return { starters: starters, bench: pool.filter(function (p) { return !used[p.id]; }), total: total, unfilled: unfilled };
  }
  function lv(players, slots, val) { return bestLineup(players, slots, val).total; }
  function swap(roster, out, inn) { var g = {}; out.forEach(function (p) { g[p.id] = 1; }); return roster.filter(function (p) { return !g[p.id]; }).concat(inn); }
  function roster(lg, t) { return t.players.map(function (id) { return P(lg, id); }); }
  var SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "D/ST", "K"], SLOT_LABEL = { "RB/WR/TE": "FLEX", "D/ST": "DST" };
  function startSlots(lg) { var out = []; SLOT_ORDER.forEach(function (s) { for (var i = 0; i < +(lg.slots[s] || 0); i++) out.push(s); }); return out; }
  function currentStarters(lg, t) { return roster(lg, t).filter(function (p) { return p.slot && p.slot !== "BE" && p.slot !== "IR"; }); }
  function bySlot(lg, t) {
    var g = {}; currentStarters(lg, t).forEach(function (p) { (g[p.slot] = g[p.slot] || []).push(p); });
    Object.keys(g).forEach(function (k) { g[k].sort(function (a, b) { return wkVal(b) - wkVal(a); }); });
    return g;
  }
  function slotLines(lg, t) {
    var g = bySlot(lg, t), used = {}, out = [];
    startSlots(lg).forEach(function (s) { var p = (g[s] || []).filter(function (q) { return !used[q.id]; })[0]; if (p) used[p.id] = 1; out.push({ slot: s, p: p }); });
    return { lines: out, used: used };
  }

  /* ---------------- derived per league ---------------- */
  var C = {};
  LGS.forEach(function (k) {
    var lg = D.leagues[k];
    Object.keys(lg.players).forEach(function (pid) { lg.players[pid].lgWeek = lg.week; lg.players[pid].lg = k; });
    var mine = me(lg), my = roster(lg, mine), c = C[k] = {};
    c.me = mine;
    c.oppId = (mine.sched || [])[lg.week - 1];
    c.opp = c.oppId != null ? team(lg, c.oppId) : null;
    c.mu = null; (lg.matchups || []).forEach(function (m) { if (m.home === mine.id || m.away === mine.id) c.mu = m; });
    if (c.mu && !c.opp) { c.oppId = c.mu.home === mine.id ? c.mu.away : c.mu.home; c.opp = team(lg, c.oppId); }
    var home = c.mu && c.mu.home === mine.id;
    c.myScore = c.mu ? (home ? c.mu.hs : c.mu.as) : null; c.oppScore = c.mu ? (home ? c.mu.as : c.mu.hs) : null;
    c.myProj = c.mu ? (home ? c.mu.hp : c.mu.ap) : null; c.oppProj = c.mu ? (home ? c.mu.ap : c.mu.hp) : null;
    c.live = c.myScore != null && (c.myScore > 0 || c.oppScore > 0);
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
    var trades = c.moves.filter(function (a) { return a.kind === "trade" || a.kind === "plan"; });
    c.todo = c.moves.filter(function (a) { return a.kind === "lineup" || a.kind === "injury"; })
      .concat(c.moves.filter(function (a) { return a.kind === "waiver"; }).slice(0, 2));
    if (trades.length) c.todo.push(trades.slice().sort(function (a, b) { return b.gain - a.gain; })[0]);
    c.todo.sort(function (a, b) { return (b.urgency - a.urgency) || (b.gain - a.gain); });
    c.rankByProj = lg.teams.slice().sort(function (a, b) { return b.lineupProj - a.lineupProj; }).map(function (t) { return t.id; });
    // waiver scout
    var baseS = c.bestSeason.total, baseW = c.bestWeek.total, needed = {};
    c.bestWeek.starters.forEach(function (s) { if (s.v > 0) needed[s.p.id] = 1; });
    c.scout = lg.fa.map(function (pid) {
      var p = P(lg, pid), gs = lv(my.concat([p]), lg.slots, sVal) - baseS, gw = lv(my.concat([p]), lg.slots, wkVal) - baseW;
      var l3 = last3(p), sa = seasonAvg(p), rows = playedRows(p), u3 = usage3(p);
      var lastU = rows.length ? touches(rows[rows.length - 1]) : null, prevU = rows.length > 1 ? avg(rows.slice(0, -1).map(touches)) : null;
      var cats = [], skill = ["QB", "RB", "WR", "TE"].indexOf(p.pos) >= 0;
      if (gs >= 3) cats.push("start");
      if (gw >= 2 && !dead(p)) cats.push("week");
      if (skill && rows.length >= 2 && l3 >= 8 && sa && l3 >= 1.25 * sa) cats.push("hot");
      if (touchPos(p) && lastU != null && prevU != null && lastU >= 8 && lastU >= 1.4 * prevU) cats.push("role");
      if (!cats.length && p.proj && skill) {
        var worst = Math.min.apply(null, c.bestSeason.starters.filter(function (s) { return s.p.pos === p.pos; }).map(function (s) { return s.v; }).concat([Infinity]));
        if (worst !== Infinity && p.proj >= 0.8 * worst) cats.push("stash");
      }
      if (l3 >= 10 && u3 != null && u3 < 4 && touchPos(p)) cats.push("warn");
      var drop = null;
      if (gs > 0 || gw > 0) {
        var cand = my.filter(function (q) { return q.slot !== "IR" && !needed[q.id] && !(q.pos === p.pos && q.proj > p.proj); }).sort(function (a, b) { return sVal(a) - sVal(b); });
        drop = cand[0] || null;
      }
      return { p: p, gs: gs, gw: gw, l3: l3, sa: sa, u3: u3, lastU: lastU, prevU: prevU, cats: cats, drop: drop,
        score: gs * 2 + gw * 3 + (l3 || 0) * 0.5 + (cats.indexOf("role") >= 0 ? 4 : 0) - (dead(p) ? 6 : 0) };
    }).filter(function (x) { return x.cats.length; }).sort(function (a, b) { return b.score - a.score; });
    var all = Object.keys(lg.players).map(function (pid) { return P(lg, pid); });
    c.trend = all.map(function (p) { var l = last3(p), s = seasonAvg(p); return { p: p, l3: l, sa: s, d: l != null && s != null && playedRows(p).length >= 3 ? l - s : null }; })
      .filter(function (x) { return x.d != null && (x.p.proj || 0) > 40; });
    c.risers = c.trend.slice().sort(function (a, b) { return b.d - a.d; }).slice(0, 5);
    c.fallers = c.trend.slice().sort(function (a, b) { return a.d - b.d; }).slice(0, 5);
    c.byes = {}; c.starters.forEach(function (p) { (p.byes || []).forEach(function (w) { if (w >= lg.week) (c.byes[w] = c.byes[w] || []).push(p); }); });
    c.ahead = [];
    for (var w = lg.week + 1; w <= Math.min(lg.week + 3, (mine.sched || []).length); w++) {
      var oid = mine.sched[w - 1], ot = team(lg, oid); if (!ot) continue;
      var r = c.rankByProj.indexOf(oid) + 1, q = r / lg.teams.length;
      c.ahead.push({ w: w, t: ot, rank: r, word: q <= 0.34 ? "Tough" : q >= 0.67 ? "Soft" : "Even", cls: q <= 0.34 ? "dn" : q >= 0.67 ? "up" : "neu", g: q <= 0.34 ? "▼" : q >= 0.67 ? "▲" : "●" });
    }
    c.depth = ["QB", "RB", "WR", "TE"].map(function (pos) {
      var st = c.bestSeason.starters.filter(function (s) { return s.p.pos === pos; }), bn = c.bestSeason.bench.filter(function (p) { return p.pos === pos && p.slot !== "IR"; });
      var worst = st.length ? Math.min.apply(null, st.map(function (s) { return s.v; })) : 0, best = bn.length ? Math.max.apply(null, bn.map(sVal)) : 0, ratio = worst ? best / worst : 0;
      var cls = !bn.length || ratio < 0.5 ? "dn" : ratio < 0.8 ? "cau" : "up";
      return { pos: pos, starters: st.length, bench: bn.length, worst: worst, best: best, word: cls === "dn" ? "Thin" : cls === "cau" ? "OK" : "Deep", cls: cls, g: cls === "dn" ? "▼" : cls === "cau" ? "◆" : "▲" };
    });
  });

  /* ---------------- components ---------------- */
  function label(t, right) { return '<div class="label"><span>' + t + "</span>" + (right ? "<b>" + right + "</b>" : "") + "</div>"; }
  function chev() { return '<svg class="chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>'; }
  function rx(id, head, body, open) { return '<div class="rx' + (open ? " open" : "") + '" data-rx="' + esc(id) + '">' + head + '<div class="x"><div>' + body + "</div></div></div>"; }
  function prow(lg, p, o) {
    o = o || {};
    var meta = o.meta != null ? o.meta : posT(p.pos) + "<span>" + esc(p.team || "") + "</span>" + (o.ind != null ? o.ind : oppHtml(p));
    var cls = "row" + (o.b ? " b" : "") + (o.cls ? " " + o.cls : "") + (dead(p) && o.deadMark !== false ? " dead" : "");
    var val = o.val != null ? o.val : n1(wkVal(p)), unit = o.unit != null ? o.unit : "proj";
    var attrs = (o.b ? " data-xt" : (o.open ? ' data-player="' + esc(p.id) + '"' : "")) + (o.attrs || "");
    return '<div class="' + cls + '"' + attrs + ">" + (o.sign ? '<span class="sg ' + o.sign + '">' + (o.sign === "in" ? "+" : "−") + "</span>" : "") + (o.k != null ? '<span class="k' + (o.kbn ? " bn" : "") + '">' + esc(o.k) + "</span>" : "") +
      pic(p, o.pic || "") + '<span class="t"><b>' + esc(p.n) + "</b><small>" + meta + stTag(p) + "</small></span>" +
      (o.right != null ? o.right : '<span class="n"><span class="num">' + val + "</span><small>" + unit + "</small></span>") + (o.b ? chev() : "") + "</div>";
  }
  function spark(p) {
    var rows = (p.hist || []).filter(function (r) { return r.w < p.lgWeek; });
    if (rows.length < 2) return '<p class="empty">Not enough games.</p>';
    var W = 280, H = 46, pad = 10, n = rows.length;
    var max = Math.max.apply(null, rows.map(function (r) { return Math.max(r.pts || 0, r.proj || 0); }).concat([1]));
    var x = function (i) { return pad + i * (W - 2 * pad) / Math.max(1, n - 1); }, y = function (v) { return H - 12 - (v / max) * (H - 18); };
    var d = "", started = false, lastPt = null;
    rows.forEach(function (r, i) { if (!r.played) { started = false; return; } var pt = [x(i), y(r.pts || 0)]; d += (started ? " L" : " M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1); started = true; lastPt = pt; });
    var proj = rows.map(function (r, i) { return r.proj != null ? (i ? "L" : "M") + x(i).toFixed(1) + " " + y(r.proj).toFixed(1) : ""; }).join(" ");
    var lab = rows.map(function (r, i) { return '<text x="' + x(i).toFixed(1) + '" y="' + (H - 2) + '" text-anchor="middle"' + (r.played ? "" : ' class="bye"') + ">" + (r.played ? n0(r.pts) : "B") + "</text>"; }).join("");
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="Points by week">' + (proj ? '<path class="p" d="' + proj + '"/>' : "") + '<path class="l" d="' + d.trim() + '"/>' + (lastPt ? '<circle cx="' + lastPt[0].toFixed(1) + '" cy="' + lastPt[1].toFixed(1) + '" r="2.6"/>' : "") + lab + "</svg>" +
      '<div class="tiny"><span>WK ' + rows[0].w + "–" + rows[rows.length - 1].w + " pts</span><span class=\"dim\">- - proj · B bye</span></div>";
  }
  function bar(v, max, cls, i) { var w = max ? Math.max(0, Math.min(1, v / max)) : 0; return '<div class="bar"><i class="' + (cls || "") + '" style="--w:' + w.toFixed(3) + ";--i:" + (i || 0) + '"></i></div>'; }
  function cmp(a, b, la, lb) {
    var max = Math.max(a || 0, b || 0, 1), s = sem((a || 0) - (b || 0), 0.5);
    return '<div class="cmp"><div class="bar l"><i class="' + (s.cls === "up" ? "" : "neu") + '" style="--w:' + ((a || 0) / max).toFixed(3) + '"></i></div><b>' + (la || n1(a)) + " · " + (lb || n1(b)) + '</b><div class="bar"><i class="' + (s.cls === "dn" ? "dn" : "neu") + '" style="--w:' + ((b || 0) / max).toFixed(3) + '"></i></div></div>';
  }
  function check(key) { return '<button class="check" data-done="' + esc(key) + '" aria-pressed="' + (S.done[key] ? "true" : "false") + '" aria-label="Done"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></button>'; }
  var KIND = { lineup: "Start", injury: "Bench", waiver: "Add", trade: "Trade", plan: "2-step", info: "" };
  function verb(a) { if (a.kind === "lineup" && /^Pick up/.test(a.title)) return "Pick up"; return KIND[a.kind] || "Move"; }
  function subject(lg, a) {
    var ps = a.players || [];
    if (a.kind === "trade" && a.trade) return a.trade.receive.map(function (p) { return byName(lg, p.name); }).filter(Boolean);
    if (a.kind === "plan" && a.trade) return a.trade.step2.receive.map(function (p) { return byName(lg, p.name); }).filter(Boolean);
    return ps.slice(0, 1).map(function (n) { return byName(lg, n); }).filter(Boolean);
  }
  function gainTxt(a) { return a.kind === "lineup" ? [sg(a.gain), "proj this wk"] : [sg(a.gain, 0), "season lineup"]; }
  function line2(lg, a) {
    var ps = a.players || [], t = a.trade;
    if (a.kind === "trade" && t) return "Give " + names(t.send) + " · " + t.partner_name.trim() + " · them " + sg(t.their_gain, 0);
    if (a.kind === "plan" && t) return "2 steps · " + names(t.step1.receive) + " first, from " + t.step1.partner_name.trim();
    if (a.kind === "waiver" || /^Pick up/.test(a.title)) return ps[1] ? "Drop " + ps[1] : "Open spot";
    if (a.kind === "lineup") { var q = byName(lg, ps[1]); return "Bench " + ps[1] + (q ? " · " + (q.bye ? "bye" : HURT(q) ? "out" : n1(wkVal(q)) + " proj") : ""); }
    if (a.kind === "injury") return "Out · start best healthy option";
    return "";
  }
  function moveDetail(lg, a) {
    var t = a.trade, rows = "";
    var inR = function (q, season) { return q ? prow(lg, q, { sign: "in", cls: "in", pic: "sm", val: season ? n0(q.proj) : n1(wkVal(q)), unit: season ? "season" : "proj" }) : ""; };
    var outR = function (q, season) { return q ? prow(lg, q, { sign: "out", cls: "ghost", pic: "sm", val: season ? n0(q.proj) : n1(wkVal(q)), unit: season ? "season" : "proj" }) : ""; };
    if (a.kind === "trade" && t) rows = t.receive.map(function (p) { return inR(byName(lg, p.name), true); }).join("") + t.send.map(function (p) { return outR(byName(lg, p.name), true); }).join("");
    if (a.kind === "plan" && t) rows = [t.step1, t.step2].map(function (st, k) { return '<div class="cap" style="padding:6px 0 2px">' + (k + 1) + " · " + esc(st.partner_name.trim()) + " · them " + sg(st.their_gain, 0) + "</div>" + st.receive.map(function (p) { return inR(byName(lg, p.name), true); }).join("") + st.send.map(function (p) { return outR(byName(lg, p.name), true); }).join(""); }).join("");
    if ((a.kind === "lineup" || a.kind === "waiver") && a.players.length > 1) rows = inR(byName(lg, a.players[0]), a.kind === "waiver") + outR(byName(lg, a.players[1]), a.kind === "waiver");
    var steps = a.steps && a.steps !== "—" ? '<p class="tiny"><span><b>ESPN</b> ' + esc(a.steps.replace(/^ESPN app: /, "")) + "</span></p>" : "";
    var msg = a.msg ? '<p class="bub">' + esc((window.FCC_MSG || {})[a.key] || "") + "</p>" : "";
    return (rows ? '<div class="list">' + rows + "</div>" : "") + steps + msg;
  }
  function moveRow(lg, a, showLg) {
    var subj = subject(lg, a)[0], g = gainTxt(a), done = !!S.done[a.key];
    var title = verb(a) + " " + (subj ? shortName(subj.n) : (a.players || [])[0] || "");
    if (a.kind === "trade") title = "Get " + shortName(subj ? subj.n : names(a.trade.receive));
    if (a.kind === "plan") title = "Get " + shortName(names(a.trade.step2.receive));
    var meta = (showLg ? "<span>" + esc(lg.name) + "</span>" : "") + "<span>" + esc(line2(lg, a)) + "</span>";
    var head = '<div class="row b' + (done ? " done" : "") + '" data-xt>' + (subj ? pic(subj) : '<span class="ph ini">' + esc(verb(a).slice(0, 2).toUpperCase()) + "</span>") +
      '<span class="t"><b>' + esc(title) + "</b><small>" + meta + '</small></span><span class="n"><span class="num up">' + g[0] + "</span><small>" + g[1] + "</small></span>" + chev() + "</div>";
    var acts = '<div style="display:flex;gap:8px;align-items:center">' + (subj ? '<button class="btn g" data-player="' + esc(subj.id) + '">View player</button>' : "") + (a.msg ? '<button class="btn g" data-copy="' + esc(a.key) + '">Copy text</button>' : "") + '<span style="flex:1"></span>' + check(a.key) + "</div>";
    return rx(a.key, head, '<div class="xin">' + moveDetail(lg, a) + acts + "</div>", !!S.open[a.key]);
  }

  function hero(lg, c) {
    var t = c.me, o = c.opp; if (!o) return '<div class="hero"><p class="empty">No matchup this week.</p></div>';
    var ms = c.live ? c.myScore : c.myProj, os = c.live ? c.oppScore : c.oppProj, share = (ms + os) > 0 ? ms / (ms + os) : 0.5, f = ago(lg.pulledAt);
    return '<div class="hero"><div class="names"><div><b>' + esc(t.name) + "</b><small>" + rec(t) + (t.standing ? " · " + ord(t.standing) : "") + '</small></div><span class="cap">WK ' + lg.week + '</span><div class="r"><b>' + esc(o.name.trim()) + "</b><small>" + rec(o) + (o.standing ? " · " + ord(o.standing) : "") + "</small></div></div>" +
      '<div class="scores"><span class="num" data-count="' + n1(ms) + '">' + n1(ms) + '</span><span class="mid">' + (c.live ? "SO FAR" : "PROJ") + '</span><span class="num r" data-count="' + n1(os) + '">' + n1(os) + "</span></div>" +
      (c.live ? '<div class="proj"><span>Proj <b>' + n1(c.myProj) + '</b></span><span class="r">Proj <b>' + n1(c.oppProj) + "</b></span></div>" : "") +
      '<div class="hbar"><i style="--w:' + share.toFixed(3) + '"></i></div>' +
      '<div class="foot"><span>' + c.left + ' left</span><span class="mid">' + (c.live ? "as of " + f.txt + " ago" : "pre-kickoff") + '</span><span class="r">' + c.oppLeft + " left</span></div></div>";
  }
  function bestMove(lg, a) {
    var subj = subject(lg, a)[0], g = gainTxt(a), done = !!S.done[a.key];
    var who = subj ? pic(subj, "lg") + '<span class="who"><b>' + esc(subj.n) + "</b><small>" + posT(subj.pos) + "<span>" + esc(subj.team || "") + "</span>" + oppHtml(subj) + stTag(subj) + "</small></span>" : '<span class="who"><b>' + esc(a.title) + "</b></span>";
    var acts = (subj ? '<button class="btn g" data-player="' + esc(subj.id) + '">View player</button>' : "") +
      (a.kind === "lineup" ? '<button class="btn g" data-nav="moves" data-sub="lineup" data-lg="' + lg.key + '">Lineup</button>' : a.msg ? '<button class="btn g" data-copy="' + esc(a.key) + '">Copy text</button>' : "") +
      '<span style="flex:1"></span>' + check(a.key);
    return '<div class="move' + (done ? " done" : "") + '"><div class="row" style="min-height:0;padding:0;border:0;gap:8px"><span class="tag ' + (a.urgency >= 2 ? "o" : "c") + '">' + esc(verb(a)) + "</span>" + (LGS.length > 1 ? '<span class="cap">' + esc(lg.name) + "</span>" : "") + "</div>" +
      '<div class="head">' + who + '<span class="gain"><span class="num">' + g[0] + "</span><small>" + g[1] + "</small></span></div>" +
      '<p class="sub">' + esc(line2(lg, a)) + '</p><div class="acts">' + acts + "</div></div>";
  }

  /* ---------------- views ---------------- */
  function vHome() {
    var lg = L(), c = C[S.lg], i = 0, h = '<div class="v">';
    h += '<div style="--i:' + i++ + ';margin-top:6px">' + hero(lg, c) + "</div>";
    var all = []; LGS.forEach(function (k) { C[k].todo.forEach(function (a) { all.push(a); }); });
    all.sort(function (a, b) { return (b.urgency - a.urgency) || (b.gain - a.gain); });
    var top = all.filter(function (a) { return !S.done[a.key]; })[0];
    h += '<div style="--i:' + i++ + '">' + label("Best move") + (top ? bestMove(D.leagues[top.lg], top) : '<p class="calm"><b>Nothing to do.</b> Lineups are set.</p>') + "</div>";
    h += '<div style="--i:' + i++ + '">' + label("Leagues") + '<div class="list">' + LGS.map(function (k) {
      var g = D.leagues[k], cc = C[k], t = cc.me, o = cc.opp, ms = cc.live ? cc.myScore : cc.myProj, os = cc.live ? cc.oppScore : cc.oppProj;
      var d = (ms || 0) - (os || 0), st = cc.live ? (Math.abs(d) < 3 ? ["◆ Close", "cau"] : d > 0 ? ["▲ Ahead", "up"] : ["▼ Behind", "dn"]) : (Math.abs(d) < 3 ? ["◆ Even", "cau"] : d > 0 ? ["▲ Favored", "up"] : ["▼ Underdog", "dn"]);
      var n = cc.todo.filter(function (a) { return !S.done[a.key]; }).length;
      return '<div class="row b lgrow" data-nav="leagues" data-lg="' + k + '"><span class="t"><b>' + esc(g.name) + "</b><small>" + rec(t) + (t.standing ? " · " + ord(t.standing) : "") + (o ? " · vs " + esc(o.name.trim()) : "") + (n ? ' · <span class="up">' + n + " move" + (n > 1 ? "s" : "") + "</span>" : "") + '</small></span><span class="mid"><span class="num">' + n1(ms) + "–" + n1(os) + "</span><small>" + (cc.live ? "so far" : "proj") + '</small></span><span class="stx ' + st[1] + '">' + st[0] + "</span>" + chev() + "</div>";
    }).join("") + "</div></div>";
    var rest = all.filter(function (a) { return a !== top; }).slice(0, 6);
    if (rest.length) h += '<div style="--i:' + i++ + '">' + label("Also", rest.length) + '<div class="list">' + rest.map(function (a) { return moveRow(D.leagues[a.lg], a, LGS.length > 1); }).join("") + "</div></div>";
    var news = []; LGS.forEach(function (k) { D.leagues[k].news.forEach(function (n) { if (n.mine) news.push({ n: n, lg: D.leagues[k] }); }); });
    news = news.filter(function (x, idx, arr) { return arr.findIndex(function (m) { return m.n.h === x.n.h; }) === idx; }).slice(0, 3);
    if (news.length) h += '<div style="--i:' + i++ + '">' + label("News") + '<div class="list news">' + news.map(function (x) { var p = x.n.pids && x.n.pids.length ? P(x.lg, x.n.pids[0]) : null; return '<div class="row">' + (p ? pic(p) : "") + '<span class="t"><b>' + (x.n.url ? '<a href="' + esc(x.n.url) + '" target="_blank" rel="noopener">' + esc(x.n.h) + "</a>" : esc(x.n.h)) + "</b><small>" + esc(x.n.who) + (x.n.src ? " · " + esc(x.n.src) : "") + "</small></span></div>"; }).join("") + "</div></div>";
    return h + "</div>";
  }

  function vLeagues() {
    var lg = L(), c = C[S.lg], t = c.me, o = c.opp, i = 0, h = '<div class="v">';
    h += '<div style="--i:' + i++ + ';margin-top:6px">' + hero(lg, c) + "</div>";
    if (o) {
      var mine = slotLines(lg, t).lines, theirs = slotLines(lg, o).lines;
      h += '<div style="--i:' + i++ + '">' + label("Position by position", "▲ you · ▼ them · ◆ close") + '<div class="list h2h">' + mine.map(function (m, idx) {
        var a = m.p, b = theirs[idx] ? theirs[idx].p : null;
        var av = a ? (a.played >= 100 ? a.wpts : wkVal(a)) : 0, bv = b ? (b.played >= 100 ? b.wpts : wkVal(b)) : 0, s = sem(av - bv, 1.5);
        var side = function (p, v, r) { return p ? '<div class="side' + (r ? " r" : "") + '"><b>' + esc(p.n) + "</b><small>" + (p.played >= 100 ? "final " : dead(p) ? (p.bye ? "bye · " : "out · ") : "") + n1(v) + "</small></div>" : '<div class="side' + (r ? " r" : "") + '"><b class="dim">—</b></div>'; };
        return '<div class="row">' + side(a, av, false) + '<div class="mid"><span>' + esc(SLOT_LABEL[m.slot] || m.slot) + '</span><b class="' + s.cls + '">' + s.g + " " + sg(av - bv) + "</b></div>" + side(b, bv, true) + "</div>";
      }).join("") + "</div></div>";
    }
    var st = lg.teams.slice().sort(function (a, b) { return (a.standing || 99) - (b.standing || 99) || (b.pf || 0) - (a.pf || 0); });
    h += '<div style="--i:' + i++ + '">' + label("Standings", "top " + lg.playoffTeams + " in") + '<table class="tbl"><thead><tr><th>#</th><th>Team</th><th class="n">W-L</th><th class="n">PF</th><th class="n">Odds</th></tr></thead><tbody>' +
      st.map(function (x, idx) { return '<tr class="' + (x.me ? "me" : "") + '" data-team="' + x.id + '"' + (lg.playoffTeams && idx === lg.playoffTeams ? ' style="border-top:2px solid var(--hair2)"' : "") + "><td>" + (x.standing || idx + 1) + "</td><td><b>" + esc(x.name.trim()) + "</b>" + (x.streak ? ' <span class="dim">' + esc(x.streak) + "</span>" : "") + '</td><td class="n">' + (rec(x) || "—") + '</td><td class="n">' + n0(x.pf) + '</td><td class="n">' + (x.po != null ? n0(x.po) + "%" : "—") + "</td></tr>"; }).join("") + "</tbody></table></div>";
    h += '<div style="--i:' + i++ + '">' + label("Roster", '<button data-nav="moves" data-sub="lineup" class="up">Optimize ›</button>') + rosterList(lg, t, true) + "</div>";
    var rules = (lg.rules || []).filter(function (r) { return /TD|Yard|reception|Interception|Fumble/i.test(r.label); }).slice(0, 10);
    h += '<div style="--i:' + i++ + '">' + label("League") + '<div class="list">' +
      rx("rules", '<div class="row b" data-xt><span class="t"><b>Rules</b><small>' + lg.teamCount + " teams · " + lg.playoffTeams + " playoff · " + esc(lg.scoring.toUpperCase()) + " · " + (lg.waiverType === "faab" ? "FAAB $" + lg.faabBudget : "waiver #" + (t.waiverRank || "?")) + "</small></span>" + chev() + "</div>",
        '<div class="xin flush"><p class="tiny"><span>' + startSlots(lg).map(function (s) { return SLOT_LABEL[s] || s; }).join(" · ") + "</span><span>BN " + (lg.slots.BE || 0) + " · IR " + (lg.slots.IR || 0) + "</span></p>" + (rules.length ? '<table class="tbl">' + rules.map(function (r) { return "<tr><td>" + esc(r.label) + '</td><td class="n"><b>' + sg(r.points, 1).replace(".0", "") + "</b></td></tr>"; }).join("") + "</table>" : "") + "</div>", false) +
      (lg.activity && lg.activity.length ? rx("act", '<div class="row b" data-xt><span class="t"><b>Activity</b><small>' + esc((lg.activity[0].team || "").trim() + " · " + (lg.activity[0].kind === "DROPPED" ? "dropped " : "added ") + (lg.activity[0].player || "")) + "</small></span>" + chev() + "</div>",
        '<div class="xin flush"><div class="list">' + lg.activity.slice(0, 15).map(function (a) { var d = a.at ? new Date(a.at) : null, when = d && !isNaN(d) ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : ""; var k = a.kind === "FA ADDED" || a.kind === "WAIVER ADDED" ? "▲ Added" : a.kind === "DROPPED" ? "▼ Dropped" : a.kind.indexOf("TRADE") >= 0 ? "⇄ Traded" : esc(a.kind); return '<div class="row" style="min-height:40px"><span class="t"><b>' + k + " " + esc(a.player || "") + (a.bid ? " · $" + a.bid : "") + "</b><small>" + esc((a.team || "").trim()) + (when ? " · " + when : "") + "</small></span></div>"; }).join("") + "</div></div>", false) : "") + "</div></div>";
    return h + "</div>";
  }
  function rosterList(lg, t, mine) {
    var sl = slotLines(lg, t), moveIn = {}, moveOut = {};
    if (mine) C[lg.key].moves.forEach(function (a) { if (a.kind === "lineup" && /^Start /.test(a.title)) { moveIn[a.players[0]] = 1; moveOut[a.players[1]] = 1; } if (a.kind === "injury") moveOut[a.players[0]] = 1; });
    var line = function (p, k, bn) {
      if (!p) return '<div class="row"><span class="k">' + esc(k) + '</span><span class="t"><b class="dim">Empty</b></span><span class="n"><span class="num dn">0</span></span></div>';
      var fin = p.played >= 100 && p.wpts != null, mark = moveOut[p.n] ? '<span class="st dn">▼ Sit</span>' : moveIn[p.n] ? '<span class="st up">▲ Start</span>' : "";
      return prow(lg, p, { k: k, kbn: bn, open: true, pic: "sm", ind: oppHtml(p) + mark, val: fin ? n1(p.wpts) : n1(wkVal(p)), unit: fin ? "final" : dead(p) ? "0 pts" : "proj" });
    };
    var bench = roster(lg, t).filter(function (p) { return !sl.used[p.id] && p.slot !== "IR"; }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
    var ir = roster(lg, t).filter(function (p) { return p.slot === "IR"; });
    return '<div class="list">' + sl.lines.map(function (l) { return line(l.p, SLOT_LABEL[l.slot] || l.slot, false); }).join("") + bench.map(function (p) { return line(p, "BN", true); }).join("") + ir.map(function (p) { return line(p, "IR", true); }).join("") + "</div>";
  }

  function vPlayers() {
    var lg = L(), i = 0, h = '<div class="v">';
    h += '<div class="search" style="--i:' + i++ + ';margin-top:8px"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg><input id="q" type="search" placeholder="Search ' + esc(lg.name) + '" value="' + esc(S.q) + '" autocomplete="off"></div>';
    h += '<div class="scroll" style="--i:' + i++ + ';margin-top:8px">' + ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"].map(function (p) { return '<button class="chipb" data-pill="pos:' + p + '" aria-pressed="' + (S.pos === p) + '">' + (p === "D/ST" ? "DST" : p) + "</button>"; }).join("") + '<button class="chipb" data-pill="fav:1" aria-pressed="' + S.favOnly + '">★</button></div>';
    h += '<div class="ctl" style="--i:' + i++ + ';margin-top:8px"><select class="sel" id="avail" aria-label="Roster">' + [["ALL", "Everyone"], ["FA", "Free agents"], ["ME", "My team"], ["OTH", "Other teams"]].map(function (x) { return '<option value="' + x[0] + '"' + (S.avail === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; }).join("") + "</select>" +
      '<select class="sel" id="sort" aria-label="Sort">' + [["wk", "Wk " + lg.week + " proj"], ["proj", "Season proj"], ["l3", "Last 3 avg"], ["spts", "Season pts"], ["use", "Volume"], ["own", "% rostered"]].map(function (x) { return '<option value="' + x[0] + '"' + (S.sort === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; }).join("") + "</select></div>";
    var list = filteredPlayers(lg), shown = list.slice(0, 40 + S.more * 40);
    h += '<div class="list" id="plist" style="--i:' + i++ + ';margin-top:10px">' + (shown.length ? shown.map(function (p) { return playerRow(lg, p); }).join("") : '<p class="empty">No players match.</p>') + "</div>";
    if (list.length > shown.length) h += '<button class="btn full g" data-more style="margin-top:10px">' + Math.min(40, list.length - shown.length) + " more</button>";
    if (S.cmp.length) h += '<button class="btn c full" data-sheet="compare" style="position:sticky;bottom:calc(92px + env(safe-area-inset-bottom,0px));margin-top:12px">Compare ' + S.cmp.length + "</button>";
    return h + "</div>";
  }
  function metric(p) {
    switch (S.sort) {
      case "proj": return [n0(p.proj), "season"]; case "l3": return [n1(last3(p)), "last 3"]; case "spts": return [n1(p.spts), "pts"];
      case "use": return [n1(usage3(p)), "tch+tgt"]; case "own": return [p.own != null ? n0(p.own) + "%" : "—", "rostered"]; default: return [n1(wkVal(p)), "proj"];
    }
  }
  function sortVal(p) { switch (S.sort) { case "proj": return p.proj || 0; case "l3": return last3(p) || 0; case "spts": return p.spts || 0; case "use": return usage3(p) || 0; case "own": return p.own || 0; default: return wkVal(p); } }
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
  function playerRow(lg, p) {
    var m = metric(p), own = ownerWord(lg, p);
    return prow(lg, p, { open: true, meta: posT(p.pos) + "<span>" + esc(p.team || "") + '</span><span class="dim">' + esc(own.length > 14 ? own.slice(0, 13) + "…" : own) + "</span>" + oppHtml(p) + (S.favs[p.id] ? '<span class="st" style="color:var(--wr)">★</span>' : ""), val: m[0], unit: m[1] });
  }

  function vMoves() {
    var lg = L(), i = 0, h = '<div class="v">';
    h += '<div class="seg" style="--i:' + i++ + ';margin-top:8px">' + [["lineup", "Lineup"], ["waivers", "Waivers"], ["trades", "Trades"]].map(function (x) { return '<button data-sub="' + x[0] + '" aria-pressed="' + (S.sub === x[0]) + '">' + x[1] + "</button>"; }).join("") + "</div>";
    h += S.sub === "lineup" ? vLineup(lg, i) : S.sub === "waivers" ? vWaivers(lg, i) : vTrades(lg, i);
    return h + "</div>";
  }
  function lr(p, v, max, cls, i) {
    return '<div class="lr' + (cls ? " " + cls : "") + (dead(p) ? " dead" : "") + '" data-player="' + esc(p.id) + '">' + pic(p, "sm") + '<span class="t"><b>' + esc(p.n) + "</b>" + bar(v, max, dead(p) ? "dn" : cls === "ghost" ? "neu" : "", i) + '</span><span class="num">' + n1(v) + "</span></div>";
  }
  function vLineup(lg, i) {
    var c = C[lg.key], t = c.me, sl = slotLines(lg, t), my = roster(lg, t), best = c.bestWeek, gap = best.total - c.curWeek, h = "";
    h += '<div class="kv" style="--i:' + i++ + ';margin-top:14px"><div><span class="num">' + n1(c.curWeek) + '</span><small>set · wk ' + lg.week + '</small></div><div><span class="num">' + n1(best.total) + '</span><small>best</small></div><div><span class="num ' + (gap >= 1 ? "up" : "neu") + '">' + (gap >= 1 ? "▲ " : "") + sg(gap) + "</span><small>" + (gap < 1 ? "optimal" : "available") + "</small></div></div>";
    h += '<div style="--i:' + i++ + '">' + label("Decisions") + '<div class="list">';
    sl.lines.forEach(function (l) {
      var s0 = l.slot, starter = l.p;
      var elig = my.filter(function (p) { return p.slot === "BE" && (s0 === FLEX ? FLEXP[p.pos] : p.pos === s0); }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
      var alt = elig[0], sv = starter ? wkVal(starter) : 0, av = alt ? wkVal(alt) : 0, d = av - sv, verdict, vc, swapIt = false;
      if (!starter) { verdict = "Fill"; vc = "dn"; }
      else if (dead(starter) && alt && av > 0) { verdict = "Swap · " + (starter.bye ? "bye" : "out"); vc = "dn"; swapIt = true; }
      else if (!alt) { verdict = "Keep"; vc = "neu"; }
      else if (d >= 3) { verdict = "Swap"; vc = "up"; swapIt = true; }
      else if (d >= 1.5) { verdict = "Lean swap"; vc = "cau"; swapIt = true; }
      else if (d > -1.5) { verdict = "Keep · close"; vc = "cau"; }
      else { verdict = "Keep"; vc = "neu"; }
      var max = Math.max(sv, av, 1), key = "slot:" + lg.key + ":" + s0 + ":" + (starter ? starter.id : "x");
      var head = '<div class="slotrow b" data-xt><span class="k">' + esc(SLOT_LABEL[s0] || s0) + '</span><div><div class="vd"><b class="' + vc + '">' + (vc === "up" ? "▲ " : vc === "dn" ? "▼ " : vc === "cau" ? "◆ " : "") + esc(verdict) + "</b>" + (alt && starter ? '<span class="num ' + (swapIt ? "up" : "neu") + '">' + sg(Math.abs(d)) + "</span>" : "") + "</div>" +
        (starter ? lr(starter, sv, max, swapIt ? "ghost" : "", 0) : '<div class="lr"><span class="ph ini sm">—</span><span class="t"><b class="dim">Empty</b></span><span class="num dn">0</span></div>') + (alt ? lr(alt, av, max, swapIt ? "" : "ghost", 1) : "") + "</div></div>";
      var tbl = alt && starter ? '<table class="tbl"><thead><tr><th></th><th class="n">Proj</th><th class="n">L3</th><th class="n">Avg</th><th>Opp</th></tr></thead><tbody>' + [starter, alt].map(function (p) { var o = opp(p); return "<tr><td><b>" + esc(p.n.split(" ").pop()) + "</b> " + stTag(p) + '</td><td class="n">' + n1(wkVal(p)) + '</td><td class="n">' + n1(last3(p)) + '</td><td class="n">' + n1(seasonAvg(p)) + '</td><td class="' + o.cls + '">' + (o.g ? o.g + " " : "") + esc(o.w.replace(/^vs /, "")) + (o.r ? " (" + ord(o.r) + ")" : "") + "</td></tr>"; }).join("") + "</tbody></table>" : "";
      h += rx(key, head, tbl ? '<div class="xin" style="padding-left:44px">' + tbl + "</div>" : "", false);
    });
    return h + "</div></div>";
  }
  var WCATS = [["ALL", "All"], ["start", "Starts"], ["week", "This wk"], ["hot", "Hot"], ["role", "Role"], ["stash", "Stash"], ["warn", "Beware"]];
  var WWORD = { start: ["▲ Starts for you", "up"], week: ["▲ This week", "up"], hot: ["▲ Hot hand", "up"], role: ["▲ Role growing", "up"], stash: ["● Stash", "neu"], warn: ["◆ Thin volume", "cau"] };
  function vWaivers(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    h += '<div class="scroll" style="--i:' + i++ + ';margin-top:8px">' + WCATS.map(function (x) { return '<button class="chipb" data-pill="wcat:' + x[0] + '" aria-pressed="' + (S.wcat === x[0]) + '">' + x[1] + "</button>"; }).join("") + "</div>";
    var list = c.scout.filter(function (x) { return S.wcat === "ALL" || x.cats.indexOf(S.wcat) >= 0; }).slice(0, 10);
    h += '<div style="--i:' + i++ + '">' + label("Ranked for your roster", lg.waiverType === "faab" ? "FAAB $" + lg.faabBudget : "waiver #" + (t.waiverRank || "?")) + '<div class="list">';
    if (!list.length) h += '<p class="calm"><b>Nothing worth a claim.</b></p>';
    list.forEach(function (x) {
      var p = x.p, ww = WWORD[x.cats[0]], fit = x.gs >= 3 ? [sg(x.gs, 0), "season"] : x.gw >= 2 ? [sg(x.gw), "this wk"] : [n1(wkVal(p)), "proj"], o = opp(p);
      var head = prow(lg, p, { b: true, ind: '<span class="' + ww[1] + '">' + ww[0] + "</span>", val: fit[0], unit: fit[1], deadMark: false });
      var body = '<div class="xin">' + spark(p) + '<div class="kv"><div><span class="num">' + n1(x.l3) + '</span><small>last 3</small></div><div><span class="num">' + (touchPos(p) ? n1(x.u3) : n1(wkVal(p))) + "</span><small>" + (touchPos(p) ? "tch+tgt /g" : "wk proj") + '</small></div><div><span class="num ' + o.cls + '">' + (o.r ? ord(o.r) : "—") + "</span><small>" + (p.bye ? "bye" : "opp vs " + esc(p.pos)) + "</small></div></div>" +
        '<p class="tiny"><span>' + (x.drop ? "Drop <b>" + esc(x.drop.n) + "</b>" : "Open spot") + "</span>" + (p.own != null ? "<span>" + n0(p.own) + "% rostered</span>" : "") + (x.cats.length > 1 ? "<span>" + x.cats.slice(1).map(function (k) { return WWORD[k][0]; }).join(" · ") + "</span>" : "") + "</p>" +
        '<div style="display:flex;gap:8px"><button class="btn g" data-player="' + esc(p.id) + '">View player</button></div></div>';
      h += rx("fa:" + lg.key + ":" + p.id, head, body, false);
    });
    return h + "</div></div>";
  }
  function vTrades(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    var ideas = c.moves.filter(function (a) { return a.kind === "trade" || a.kind === "plan"; });
    h += '<div style="--i:' + i++ + '">' + label("Ideas", ideas.length ? "both sides gain" : "") + '<div class="list">' + (ideas.length ? ideas.map(function (a) { return moveRow(lg, a, false); }).join("") : '<p class="calm"><b>No trade helps both sides right now.</b></p>') +
      (c.alts.length ? rx("alts", '<div class="row b" data-xt><span class="t"><b>' + c.alts.length + " other versions</b></span>" + chev() + "</div>", '<div class="xin flush"><div class="list">' + c.alts.map(function (a) { var tr = a.trade; return '<div class="row" style="min-height:40px"><span class="t"><b>' + esc(names(tr.receive)) + "</b><small>for " + esc(names(tr.send)) + " · " + esc(tr.partner_name.trim()) + " · them " + sg(tr.their_gain, 0) + '</small></span><span class="n"><span class="num up">' + sg(tr.my_gain, 0) + "</span></span></div>"; }).join("") + "</div></div>", false) : "") + "</div></div>";
    var others = lg.teams.filter(function (x) { return !x.me; });
    if (!S.tl.partner || !team(lg, S.tl.partner) || team(lg, S.tl.partner).me) S.tl.partner = others[0].id;
    var them = team(lg, S.tl.partner), my = roster(lg, t), theirs = roster(lg, them);
    h += '<div style="--i:' + i++ + '">' + label("Trade lab") + '<div class="ctl"><select class="sel" id="tl-partner" aria-label="Partner">' + others.map(function (x) { return '<option value="' + x.id + '"' + (x.id === S.tl.partner ? " selected" : "") + ">" + esc(x.name.trim()) + (rec(x) ? " · " + rec(x) : "") + "</option>"; }).join("") + "</select>" +
      '<div class="seg"><button data-set="horizon:season" aria-pressed="' + (S.horizon === "season") + '">Season</button><button data-set="horizon:week" aria-pressed="' + (S.horizon === "week") + '">Wk ' + lg.week + "</button></div></div>" +
      '<div class="cap" style="margin:12px 0 6px">Get</div><div class="pchips">' + chips(lg, theirs, "get") + '</div><div class="cap" style="margin:12px 0 6px">Give</div><div class="pchips">' + chips(lg, my, "send") + "</div>" +
      '<div id="tl-out" style="margin-top:12px">' + tradeVerdict(lg) + "</div></div>";
    return h;
  }
  function chips(lg, ps, side) {
    return ps.filter(function (p) { return p.proj != null; }).sort(function (a, b) { return b.proj - a.proj; }).map(function (p) {
      var on = !!S.tl[side][p.id], lock = side === "send" && S.untouch[p.id];
      return '<button class="pc' + (lock ? " lock" : "") + '" data-tl="' + side + ":" + esc(p.id) + '" aria-pressed="' + on + '">' + pic(p) + posT(p.pos) + esc(shortName(p.n)) + "</button>";
    }).join("");
  }
  function tradeVerdict(lg) {
    var c = C[lg.key], t = c.me, them = team(lg, S.tl.partner), my = roster(lg, t), theirs = roster(lg, them);
    var get = theirs.filter(function (p) { return S.tl.get[p.id]; }), send = my.filter(function (p) { return S.tl.send[p.id]; });
    if (!get.length || !send.length) return '<div class="verdict"><p class="w dim">Pick both sides</p></div>';
    var wk = S.horizon === "week", val = wk ? wkVal : sVal;
    var b0 = bestLineup(my, lg.slots, val), b1 = bestLineup(swap(my, send, get), lg.slots, val), t0 = lv(theirs, lg.slots, val), t1 = lv(swap(theirs, get, send), lg.slots, val);
    var mg = b1.total - b0.total, tg = t1 - t0, rawS = send.reduce(function (s, p) { return s + (p.proj || 0); }, 0), rawG = get.reduce(function (s, p) { return s + (p.proj || 0); }, 0), ratio = rawG ? rawS / rawG : 1;
    var take = wk ? S.takeAt / 4 : S.takeAt, win = wk ? 1 : 3, word, cls;
    if (mg >= take) { word = "▲ Take it"; cls = "up"; } else if (mg >= win) { word = "▲ Small win"; cls = "up"; } else if (mg > -win) { word = "◆ Even"; cls = "cau"; } else { word = "▼ Pass"; cls = "dn"; }
    var hurt = get.filter(HURT), byes = wk ? get.filter(function (p) { return p.bye; }) : [];
    var pos = ["QB", "RB", "WR", "TE"].map(function (ps) { var a = b0.starters.filter(function (s) { return s.p.pos === ps; }).reduce(function (s, x) { return s + x.v; }, 0), b = b1.starters.filter(function (s) { return s.p.pos === ps; }).reduce(function (s, x) { return s + x.v; }, 0); return { pos: ps, d: b - a }; }).filter(function (x) { return Math.abs(x.d) >= 0.5; });
    var ids0 = {}; b0.starters.forEach(function (s) { ids0[s.p.id] = 1; }); var ids1 = {}; b1.starters.forEach(function (s) { ids1[s.p.id] = 1; });
    var depth = ["QB", "RB", "WR", "TE"].filter(function (ps) { return !b1.bench.some(function (p) { return p.pos === ps && p.slot !== "IR"; }); });
    var fair = ratio >= 0.85 ? "▲ Fair on paper" : ratio >= 0.7 ? "◆ Light on paper" : "▼ Lopsided on paper";
    var conf = Math.abs(mg) >= (wk ? 6 : 30) ? "High" : Math.abs(mg) >= (wk ? 2 : 10) ? "Medium" : "Low";
    var counter = "";
    if (mg < win) { var cc = bestCounter(lg, them, my, theirs, val); if (cc) counter = '<p class="line">Try <b>' + esc(names(cc.send)) + "</b> for <b>" + esc(cc.get.n) + "</b> · you " + sg(cc.mg, 0) + " · them " + sg(cc.tg, 0) + "</p>"; }
    var lineup = function (b, ids) { return '<div class="list">' + b.starters.map(function (s) { return '<div class="row' + (!ids[s.p.id] ? " in" : "") + '" style="min-height:34px;padding:4px 0"><span class="k">' + esc(SLOT_LABEL[s.slot] || s.slot) + '</span><span class="t"><b style="font-size:13px">' + esc(shortName(s.p.n)) + '</b></span><span class="n"><span class="num" style="font-size:14px">' + (wk ? n1(s.v) : n0(s.v)) + "</span></span></div>"; }).join("") + "</div>"; };
    return '<div class="verdict ' + cls + ' pop"><p class="w">' + word + "</p>" +
      '<div class="kv"><div><span class="num ' + sem(mg, win).cls + '">' + sg(mg, wk ? 1 : 0) + '</span><small>your lineup</small></div><div><span class="num ' + sem(tg, win).cls + '">' + sg(tg, wk ? 1 : 0) + '</span><small>their lineup</small></div><div><span class="num">' + ratio.toFixed(2) + "×</span><small>out ÷ in</small></div></div>" +
      '<p class="line">' + fair + " · <b>" + conf + "</b> confidence" + (hurt.length ? " · ▼ " + esc(names(hurt)) + " hurt" : "") + (byes.length ? " · ▼ " + esc(names(byes)) + " on bye" : "") + (depth.length ? " · ◆ no bench " + depth.join("/") : "") + "</p>" +
      (pos.length ? '<p class="line">' + pos.map(function (x) { var s = sem(x.d, 0.5); return x.pos + ' <b class="' + s.cls + '">' + s.g + " " + sg(x.d, wk ? 1 : 0) + "</b>"; }).join(" &nbsp; ") + "</p>" : "") + counter +
      rx("tl-x", '<div class="row b" style="min-height:40px" data-xt><span class="t"><b>Before · after</b></span>' + chev() + "</div>", '<div class="xin flush"><div class="two"><div><div class="cap">Now</div>' + lineup(b0, ids1) + '</div><div><div class="cap">After</div>' + lineup(b1, ids0) + "</div></div></div>", false) + "</div>";
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
    var strength = Object.keys(lg.strength || {});
    if (strength.length) h += '<div style="--i:' + i++ + '">' + label("Roster strength", "starters vs league median") + strength.map(function (pos, k) { var s = lg.strength[pos], sm = sem(s.edge, 12); return '<div class="stat"><span style="display:flex;gap:10px;align-items:center;min-width:0">' + posT(pos) + '<span style="flex:1">' + bar(s.mine, Math.max(s.mine, s.median) * 1.15, sm.cls === "cau" ? "neu" : sm.cls, k) + '</span></span><span class="num ' + sm.cls + '">' + sm.g + " " + sg(s.edge, 0) + '</span><span class="tiny" style="grid-column:1/-1"><span>' + n0(s.mine) + " vs " + n0(s.median) + "</span></span></div>"; }).join("") + "</div>";
    var my = roster(lg, t).filter(function (p) { return playedRows(p).length >= 3 && p.pavg; }).map(function (p) { return { p: p, d: seasonAvg(p) - p.pavg }; }).sort(function (a, b) { return b.d - a.d; });
    if (my.length) h += '<div style="--i:' + i++ + '">' + label("Projection accuracy", "actual − projected / game") + '<div class="list">' + my.slice(0, 6).map(function (x) { var s = sem(x.d, 2); return prow(lg, x.p, { open: true, pic: "sm", ind: '<span class="dim">' + n1(seasonAvg(x.p)) + " vs " + n1(x.p.pavg) + '</span>', val: '<span class="' + s.cls + '">' + s.g + " " + sg(x.d) + "</span>", unit: "per game", deadMark: false }); }).join("") + "</div></div>";
    var tr = function (x) { var s = sem(x.d, 2); return prow(lg, x.p, { open: true, pic: "sm", ind: '<span class="dim">' + esc(ownerWord(lg, x.p)) + " · L3 " + n1(x.l3) + '</span>', val: '<span class="' + s.cls + '">' + s.g + " " + sg(x.d) + "</span>", unit: "vs avg", deadMark: false }); };
    h += '<div style="--i:' + i++ + '">' + label("Trending players", "last 3 vs season") + '<div class="cap" style="margin:4px 0">Rising</div><div class="list">' + c.risers.map(tr).join("") + '</div><div class="cap" style="margin:14px 0 4px">Falling</div><div class="list">' + c.fallers.map(tr).join("") + "</div></div>";
    var weeks = []; for (var w = lg.week; w <= (lg.regWeeks || 14); w++) weeks.push(w);
    h += '<div style="--i:' + i++ + '">' + label("Byes ahead", "starters out") + '<div class="wk">' + weeks.map(function (w) { var n = (c.byes[w] || []).length; return '<div class="' + (w === lg.week ? "now" : "") + (n >= 2 ? " bad" : "") + '"><span>' + w + '</span><span class="num">' + (n || "–") + "</span></div>"; }).join("") + "</div>" +
      '<div class="list">' + Object.keys(c.byes).sort(function (a, b) { return a - b; }).map(function (w) { return '<div class="row" style="min-height:36px"><span class="k">W' + w + '</span><span class="t"><b>' + esc(c.byes[w].map(function (p) { return p.n; }).join(", ")) + "</b></span></div>"; }).join("") + "</div></div>";
    if (c.ahead.length) h += '<div style="--i:' + i++ + '">' + label("Next up") + '<div class="list">' + c.ahead.map(function (a) { return '<div class="row" data-team="' + a.t.id + '"><span class="k">W' + a.w + '</span><span class="t"><b>' + esc(a.t.name.trim()) + "</b><small>" + rec(a.t) + " · " + ord(a.rank) + ' roster</small></span><span class="st ' + a.cls + '">' + a.g + " " + a.word + "</span></div>"; }).join("") + "</div></div>";
    h += '<div style="--i:' + i++ + '">' + label("Position depth", "best bench vs weakest starter") + c.depth.map(function (d, k) { return '<div class="stat"><span style="display:flex;gap:10px;align-items:center;min-width:0">' + posT(d.pos) + '<span style="flex:1">' + bar(d.best, Math.max(d.worst, d.best, 1), d.cls === "cau" ? "neu" : d.cls, k) + '</span></span><span class="st ' + d.cls + '">' + d.g + " " + d.word + '</span><span class="tiny" style="grid-column:1/-1"><span>' + d.bench + " on bench · best " + n0(d.best) + " vs " + n0(d.worst) + "</span></span></div>"; }).join("") + "</div>";
    var mySur = (lg.market || []).filter(function (m) { return m.teamId === lg.myId; })[0], fits = (lg.market || []).filter(function (m) { return m.teamId !== lg.myId && m.needs.length; });
    if (fits.length) h += '<div style="--i:' + i++ + '">' + label("Who needs what") + '<p class="tiny" style="margin:-4px 0 6px"><span>' + (mySur && mySur.surplus.length ? "Your spare · <b>" + esc(mySur.surplus.map(function (s) { return s.name; }).join(", ")) + "</b>" : "No spare starters") + "</span></p>" + '<div class="list">' + fits.map(function (m) { var tt = team(lg, m.teamId), fit = mySur ? mySur.surplus.filter(function (s) { return m.needs.indexOf(s.pos) >= 0; }) : []; return '<div class="row" style="min-height:40px" data-team="' + m.teamId + '"><span class="t"><b>' + esc(tt ? tt.name.trim() : "") + "</b><small>needs " + m.needs.join(", ") + (m.surplus.length ? " · spare " + esc(m.surplus.map(function (s) { return s.name; }).join(", ")) : "") + "</small></span>" + (fit.length ? '<span class="st up">▲ Fit</span>' : "") + "</div>"; }).join("") + "</div></div>";
    return h + "</div>";
  }

  /* ---------------- sheets ---------------- */
  function sheet(html) { return '<div class="scrim" data-close></div><div class="sheet" role="dialog" aria-modal="true">' + html + "</div>"; }
  function playerSheet(pid) {
    var lg = L(), p = P(lg, pid); if (!p) return "";
    var o = opp(p), l3 = last3(p), sa = seasonAvg(p), rows = playedRows(p), u3 = usage3(p), last = rows[rows.length - 1], read = [];
    if (l3 != null && sa != null && rows.length >= 3) { var d = l3 - sa; if (d >= 3) read.push("▲ Up " + sg(d) + " vs season avg"); else if (d <= -3) read.push("▼ Down " + sg(d) + " vs season avg"); }
    if (p.pavg && sa != null && rows.length >= 3) { var r = sa - p.pavg; if (r >= 2) read.push("▲ Beats projection by " + n1(r) + "/g"); else if (r <= -2) read.push("▼ Under projection by " + n1(-r) + "/g"); }
    if (last && rows.length > 1) { var pu = avg(rows.slice(0, -1).map(touches)); if (touches(last) >= 1.4 * pu && touches(last) >= 8) read.push("▲ Usage up: " + n0(touches(last)) + " vs " + n1(pu) + " before"); }
    if (o.r && o.cls !== "neu" && !p.bye) read.push(o.g + " " + (o.cls === "up" ? "Soft" : "Tough") + " matchup · " + ord(o.r) + " vs " + p.pos);
    if (p.bye) read.push("▼ Bye this week"); if (HURT(p)) read.push("▼ " + (p.inj || "").replace("_", " ")); if (Q(p)) read.push("◆ Questionable");
    var fut = (p.byes || []).filter(function (w) { return w > lg.week; }); if (fut.length) read.push("● Bye week " + fut.join(", "));
    var fit = "";
    if (p.owner == null) { var c = C[lg.key], my = roster(lg, c.me), gs = lv(my.concat([p]), lg.slots, sVal) - c.bestSeason.total, gw = lv(my.concat([p]), lg.slots, wkVal) - c.bestWeek.total; fit = '<div class="kv"><div><span class="num ' + (gs > 0 ? "up" : "neu") + '">' + sg(gs, 0) + '</span><small>your lineup · season</small></div><div><span class="num ' + (gw > 0 ? "up" : "neu") + '">' + sg(gw) + "</span><small>your lineup · wk</small></div></div>"; }
    var usage = last ? [last.tgt != null ? n0(last.tgt) + " tgt" : "", last.car != null ? n0(last.car) + " car" : "", last.rec != null ? n0(last.rec) + " rec" : "", last.att != null ? n0(last.att) + " att" : ""].filter(Boolean).join(" · ") : "";
    return sheet('<div class="phead">' + pic(p, "xl") + '<span class="t"><b>' + esc(p.n) + "</b><small>" + posT(p.pos) + "<span>" + esc(p.team || "") + '</span><span class="dim">' + esc(ownerWord(lg, p)) + "</span>" + stTag(p) + "</small></span>" + '<button class="btn g star" data-fav="' + esc(p.id) + '" aria-pressed="' + !!S.favs[p.id] + '" style="min-width:36px;padding:0 10px" aria-label="Favorite"><svg viewBox="0 0 24 24"><path d="M12 3l2.8 5.9 6.4.8-4.7 4.5 1.2 6.4L12 17.5 6.3 20.6l1.2-6.4L2.8 9.7l6.4-.8z"/></svg></button></div>' +
      '<div class="kv"><div><span class="num">' + n1(wkVal(p)) + "</span><small>wk " + lg.week + '</small></div><div><span class="num">' + n0(p.proj) + '</span><small>season</small></div><div><span class="num">' + n1(p.spts) + '</span><small>pts</small></div><div><span class="num">' + n1(l3) + "</span><small>last 3</small></div></div>" +
      (o.w ? '<p class="tiny"><span class="' + o.cls + '">' + (o.g ? o.g + " " : "") + esc(o.w) + (o.r ? " (" + ord(o.r) + " vs " + esc(p.pos) + ")" : "") + "</span>" + (p.kick ? "<span>" + esc(fmtKick(p.kick)) + "</span>" : "") + "</p>" : "") +
      spark(p) + (usage ? '<p class="tiny"><span>Last game · ' + esc(usage) + "</span>" + (u3 != null ? "<span>L3 · " + n1(u3) + " tch+tgt/g</span>" : "") + "</p>" : "") +
      (read.length ? '<div class="list">' + read.map(function (w) { return '<div class="row" style="min-height:34px;padding:5px 0"><span class="t"><b style="font-size:14px">' + esc(w) + "</b></span></div>"; }).join("") + "</div>" : "") + fit +
      '<div class="kv"><div><span class="num">' + (p.posRank ? "#" + p.posRank : "—") + "</span><small>" + esc(p.pos) + ' rank</small></div><div><span class="num">' + (p.own != null ? n0(p.own) + "%" : "—") + '</span><small>rostered</small></div><div><span class="num">' + ((p.byes || []).join(", ") || "—") + "</span><small>bye</small></div></div>" +
      '<div style="display:flex;gap:8px"><button class="btn full" data-cmp="' + esc(p.id) + '" aria-pressed="' + (S.cmp.indexOf(p.id) >= 0) + '">' + (S.cmp.indexOf(p.id) >= 0 ? "In compare" : "Compare") + '</button><button class="btn full c" data-close>Close</button></div>');
  }
  function compareSheet() {
    var lg = L(), ps = S.cmp.map(function (id) { return P(lg, id); }).filter(Boolean);
    if (ps.length < 2) return sheet('<p class="calm"><b>Pick two players.</b> Open a player, tap Compare.</p><button class="btn full" data-close>Close</button>');
    var a = ps[0], b = ps[1], rows = [["Wk " + lg.week + " proj", wkVal(a), wkVal(b), 1], ["Season proj", a.proj, b.proj, 0], ["Season pts", a.spts, b.spts, 1], ["Last 3 avg", last3(a), last3(b), 1], ["Season avg", seasonAvg(a), seasonAvg(b), 1], ["Tch+tgt /g", usage3(a), usage3(b), 1]];
    return sheet('<div class="two"><div class="phead">' + pic(a, "lg") + '<span class="t"><b style="font-size:16px">' + esc(a.n) + "</b><small>" + posT(a.pos) + esc(a.team || "") + stTag(a) + '</small></span></div><div class="phead">' + pic(b, "lg") + '<span class="t"><b style="font-size:16px">' + esc(b.n) + "</b><small>" + posT(b.pos) + esc(b.team || "") + stTag(b) + "</small></span></div></div>" +
      rows.map(function (r) { var fa = r[3] ? n1(r[1]) : n0(r[1]), fb = r[3] ? n1(r[2]) : n0(r[2]); return '<div><div class="cap" style="margin-bottom:4px">' + r[0] + "</div>" + cmp(r[1] || 0, r[2] || 0, fa, fb) + "</div>"; }).join("") +
      '<p class="tiny"><span>' + esc(a.n.split(" ").pop()) + " " + esc(opp(a).w || "—") + "</span><span>" + esc(b.n.split(" ").pop()) + " " + esc(opp(b).w || "—") + "</span></p>" +
      '<div style="display:flex;gap:8px"><button class="btn full g" data-cmp-clear>Clear</button><button class="btn full c" data-close>Close</button></div>');
  }
  function teamSheet(id) {
    var lg = L(), t = team(lg, id); if (!t) return "";
    var r = C[lg.key].rankByProj.indexOf(t.id) + 1;
    return sheet('<div class="phead"><span class="t"><b>' + esc(t.name.trim()) + "</b><small>" + esc(t.owner || "") + (rec(t) ? " · " + rec(t) : "") + " · " + ord(r) + " roster · " + n0(t.pf) + " PF</small></span></div>" + rosterList(lg, t, false) + '<button class="btn full" data-close>Close</button>');
  }
  function settingsSheet() {
    var lg = L(), my = roster(lg, C[lg.key].me);
    var opt = function (field, vals, cur) { return '<div class="opts">' + vals.map(function (v) { return '<button data-set="' + field + ":" + v[0] + '" aria-pressed="' + (cur === v[0]) + '">' + v[1] + "</button>"; }).join("") + "</div>"; };
    return sheet('<div class="phead"><span class="t"><b>Settings</b></span></div>' +
      label("Accent") + opt("accent", [["sky", "Cyan"], ["violet", "Violet"], ["mint", "Mint"]], S.accent) +
      label("Motion") + opt("motion", [["auto", "On"], ["off", "Off"]], S.motion) +
      '<div class="setrow"><span class="t"><b>Take-it bar</b><small>season lineup pts a trade must add</small></span><input class="numin" id="takeAt" type="number" min="0" max="60" value="' + esc(S.takeAt) + '"></div>' +
      label("Untouchables") + '<div class="pchips">' + my.filter(function (p) { return p.proj; }).sort(function (a, b) { return b.proj - a.proj; }).map(function (p) { return '<button class="pc" data-untouch="' + esc(p.id) + '" aria-pressed="' + !!S.untouch[p.id] + '">' + pic(p) + posT(p.pos) + esc(shortName(p.n)) + "</button>"; }).join("") + "</div>" +
      label("Data") + '<p class="sub">' + LGS.map(function (k) { return esc(D.leagues[k].name) + " · " + esc(ago(D.leagues[k].pulledAt).txt) + " ago"; }).join(" · ") + ". ESPN is pulled twice a day; this page cannot refresh it.</p>" +
      '<div style="display:flex;gap:8px"><button class="btn full g" data-reset>Clear done & favorites</button><button class="btn full c" data-close>Done</button></div>');
  }

  /* ---------------- shell ---------------- */
  function header() {
    var latest = LGS.map(function (k) { return D.leagues[k].pulledAt; }).filter(Boolean).sort().pop() || D.generated, f = ago(latest);
    var title = S.tab === "home" ? "Command Center" : S.tab.charAt(0).toUpperCase() + S.tab.slice(1);
    return '<div class="logo"><i>TyG</i><span><b>' + title + "</b><small>Week " + esc(D.week) + ' · <span class="fresh"><b class="' + f.cls + '"></b>' + esc(f.txt) + "</span></small></span></div>" +
      (LGS.length > 1 ? '<div class="lgsw">' + LGS.map(function (k) { var n = C[k].todo.filter(function (a) { return !S.done[a.key]; }).length; return '<button data-lg="' + k + '" aria-pressed="' + (S.lg === k) + '">' + esc(D.leagues[k].name.replace(/^League /, "L")) + (n ? "<i>" + n + "</i>" : "") + "</button>"; }).join("") + "</div>" : '<span style="flex:1"></span>') +
      '<button class="gear" data-sheet="settings" aria-label="Settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></button>';
  }
  function nav() {
    var icons = { home: '<path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/>', leagues: '<path d="M8 21h8M12 17v4M6 4h12v4a6 6 0 01-12 0zM6 6H3v2a3 3 0 003 3M18 6h3v2a3 3 0 01-3 3"/>', players: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>', moves: '<path d="M7 7h12l-3-3M17 17H5l3 3"/>', insights: '<path d="M4 19h16M7 15l4-5 3 3 5-7"/>' };
    return TABS.map(function (t) { return '<button data-nav="' + t + '" aria-pressed="' + (S.tab === t) + '"><svg viewBox="0 0 24 24">' + icons[t] + "</svg>" + t + "</button>"; }).join("");
  }
  function render(soft) {
    document.documentElement.dataset.accent = S.accent; document.documentElement.dataset.motion = S.motion;
    $("#top").innerHTML = header();
    var main = $("#main"); main.classList.toggle("soft", !!soft);
    main.innerHTML = S.tab === "home" ? vHome() : S.tab === "leagues" ? vLeagues() : S.tab === "players" ? vPlayers() : S.tab === "moves" ? vMoves() : vInsights();
    $("#nav").innerHTML = nav();
    $("#sheet").innerHTML = S.sheet ? (S.sheet === "settings" ? settingsSheet() : S.sheet === "compare" ? compareSheet() : S.sheet.indexOf("team:") === 0 ? teamSheet(+S.sheet.slice(5)) : S.sheet.indexOf("player:") === 0 ? playerSheet(S.sheet.slice(7)) : "") : "";
    document.body.style.overflow = S.sheet ? "hidden" : "";
    if (!soft) countUp();
  }
  function countUp() {
    if (motionOff()) return;
    $$("[data-count]").forEach(function (el) {
      var n = parseFloat(el.dataset.count); if (isNaN(n) || !n) return; var t0 = null, dec = el.dataset.count.indexOf(".") >= 0 ? 1 : 0;
      function step(t) { if (!t0) t0 = t; var p = Math.min(1, (t - t0) / 700), v = n * (1 - Math.pow(1 - p, 3)); el.textContent = v.toFixed(dec); if (p < 1) requestAnimationFrame(step); }
      el.textContent = (0).toFixed(dec); requestAnimationFrame(step);
    });
  }
  var toastT;
  function toast(h, s) { var t = $("#toast"); t.innerHTML = esc(h) + (s ? "<small>" + esc(s) + "</small>" : ""); t.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove("on"); }, 2400); }
  function go(tab, extra) { S.tab = tab; put("tab", tab); if (extra && extra.sub) { S.sub = extra.sub; put("sub", S.sub); } if (extra && extra.lg) { S.lg = extra.lg; put("lg", S.lg); } S.sheet = null; render(); window.scrollTo(0, 0); }

  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-nav],[data-sub],[data-lg],[data-xt],[data-done],[data-copy],[data-fav],[data-cmp],[data-cmp-clear],[data-player],[data-team],[data-sheet],[data-close],[data-set],[data-pill],[data-more],[data-tl],[data-untouch],[data-reset]");
    if (!el) return;
    var ds = el.dataset;
    if (ds.close != null) { S.sheet = null; render(true); return; }
    if (ds.sheet) { S.sheet = ds.sheet; render(true); return; }
    if (ds.nav) { go(ds.nav, { sub: ds.sub, lg: ds.lg }); return; }
    if (ds.sub) { S.sub = ds.sub; put("sub", S.sub); render(); return; }
    if (ds.lg) { if (S.lg !== ds.lg) { S.lg = ds.lg; put("lg", S.lg); S.cmp = []; S.tl = { partner: null, get: {}, send: {} }; render(); window.scrollTo(0, 0); } return; }
    if (ds.xt != null) { var box = el.closest(".rx"); if (!box) return; box.classList.toggle("open"); var o = box.classList.contains("open"); if (box.dataset.rx) { if (o) S.open[box.dataset.rx] = 1; else delete S.open[box.dataset.rx]; } return; }
    if (ds.done) { if (S.done[ds.done]) delete S.done[ds.done]; else S.done[ds.done] = 1; put("done", S.done); el.setAttribute("aria-pressed", String(!!S.done[ds.done])); var wrap = el.closest(".move, .rx"); if (wrap) { var tgt = wrap.classList.contains("move") ? wrap : wrap.querySelector(".row"); tgt.classList.toggle("done", !!S.done[ds.done]); } if (S.done[ds.done]) toast("Done", "Make the move in the ESPN app."); $("#top").innerHTML = header(); return; }
    if (ds.copy) { var txt = (window.FCC_MSG || {})[ds.copy] || ""; var ok = function () { toast("Copied", txt); }, no = function () { toast("Copy this", txt); }; try { navigator.clipboard.writeText(txt).then(ok, no); } catch (err) { no(); } return; }
    if (ds.fav) { if (S.favs[ds.fav]) delete S.favs[ds.fav]; else S.favs[ds.fav] = 1; put("favs", S.favs); el.setAttribute("aria-pressed", String(!!S.favs[ds.fav])); toast(S.favs[ds.fav] ? "Favorited" : "Removed"); return; }
    if (ds.cmp) { var ix = S.cmp.indexOf(ds.cmp); if (ix >= 0) S.cmp.splice(ix, 1); else { S.cmp.push(ds.cmp); if (S.cmp.length > 2) S.cmp.shift(); } if (S.cmp.length === 2) S.sheet = "compare"; else toast("Pick one more player"); render(true); return; }
    if (ds.cmpClear != null) { S.cmp = []; S.sheet = null; render(true); return; }
    if (ds.player) { S.sheet = "player:" + ds.player; render(true); return; }
    if (ds.team) { S.sheet = "team:" + ds.team; render(true); return; }
    if (ds.set) { var kv = ds.set.split(":"), f = kv[0], v = kv[1]; S[f] = v; if (["lg", "accent", "motion"].indexOf(f) >= 0) put(f, v); if (f === "horizon") { $("#tl-out").innerHTML = tradeVerdict(L()); $$("[data-set^='horizon:']").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.set === "horizon:" + v)); }); return; } render(true); return; }
    if (ds.pill) { var pk = ds.pill.split(":"); if (pk[0] === "fav") S.favOnly = !S.favOnly; else S[pk[0]] = pk[1]; S.more = 0; render(true); return; }
    if (ds.more != null) { S.more++; render(true); return; }
    if (ds.tl) { var sp = ds.tl.split(":"), side = sp[0], id = sp.slice(1).join(":"); if (side === "send" && S.untouch[id]) { toast("Untouchable", "Change it in Settings."); return; } if (S.tl[side][id]) delete S.tl[side][id]; else S.tl[side][id] = 1; el.setAttribute("aria-pressed", String(!!S.tl[side][id])); $("#tl-out").innerHTML = tradeVerdict(L()); return; }
    if (ds.untouch) { if (S.untouch[ds.untouch]) delete S.untouch[ds.untouch]; else S.untouch[ds.untouch] = 1; put("untouch", S.untouch); el.setAttribute("aria-pressed", String(!!S.untouch[ds.untouch])); return; }
    if (ds.reset != null) { S.done = {}; S.favs = {}; S.cmp = []; put("done", S.done); put("favs", S.favs); toast("Cleared"); render(true); return; }
  });
  document.addEventListener("input", function (e) {
    if (e.target.id === "q") { S.q = e.target.value; S.more = 0; var lg = L(), list = filteredPlayers(lg).slice(0, 40), ul = $("#plist"); if (ul) ul.innerHTML = list.length ? list.map(function (p) { return playerRow(lg, p); }).join("") : '<p class="empty">No players match.</p>'; }
    if (e.target.id === "takeAt") { S.takeAt = +e.target.value || 0; put("takeAt", S.takeAt); }
  });
  document.addEventListener("change", function (e) {
    if (e.target.id === "sort") { S.sort = e.target.value; S.more = 0; render(true); }
    if (e.target.id === "avail") { S.avail = e.target.value; S.more = 0; render(true); }
    if (e.target.id === "tl-partner") { S.tl = { partner: +e.target.value, get: {}, send: {} }; render(true); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && S.sheet) { S.sheet = null; render(true); } });

  render();
})();
