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
    tab: get("tab", "home"), lg: get("lg", LGS[0]), sub: get("sub", "waivers"),
    done: get("done", {}), favs: get("favs", {}), untouch: get("untouch", {}),
    accent: get("accent", "sky"), motion: get("motion", "auto"), takeAt: get("takeAt", 10),
    q: "", pos: "ALL", avail: "ALL", sort: "wk", more: 0, favOnly: false,
    wcat: "ALL", horizon: "season", tl: { partner: null, get: {}, send: {} }, sheet: null, cmp: [], open: {}
  };
  if (LGS.indexOf(S.lg) < 0) S.lg = LGS[0];
  var TABS = ["home", "matchup", "lineup", "players", "moves"];
  if (TABS.indexOf(S.tab) < 0) S.tab = "home";
  if (["waivers", "trades"].indexOf(S.sub) < 0) S.sub = "waivers";
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
  function sVal(p) { return p.ros != null ? p.ros : (p.proj || 0); }
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
  function ownerWord(lg, p) { if (p.owner == null) return "Free agent"; var t = team(lg, p.owner); return t ? (t.me ? "Yours" : t.name.trim()) : ""; }
  function names(ps) { return ps.map(function (p) { return p.name || p.n; }).join(" + "); }
  function shortName(n) { return /D\/ST$/.test(n) ? n : n.replace(/^(\w)\S* /, "$1. "); }
  function byName(lg, n) { if (!lg._byName) { lg._byName = {}; Object.keys(lg.players).forEach(function (pid) { lg._byName[lg.players[pid].n] = lg.players[pid]; }); } return lg._byName[n]; }
  function lgShort(lg) { return lg.name.length > 14 ? lg.name.split(" ").slice(0, 2).join(" ") : lg.name; }

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
      var drop = null;
      if (gs > 0 || gw > 0) {
        var cand = my.filter(function (q) { return q.slot !== "IR" && !needed[q.id] && !(q.pos === p.pos && q.proj > p.proj); }).sort(function (a, b) { return sVal(a) - sVal(b); });
        drop = cand[0] || null;
      }
      return { p: p, gs: gs, gw: gw, l3: l3, sa: sa, u3: u3, lastU: lastU, prevU: prevU, cats: cats, drop: drop,
        score: gs * 2 + gw * 3 + (l3 || 0) * 0.5 + (cats.indexOf("role") >= 0 ? 4 : 0) - (dead(p) ? 6 : 0) };
    }).filter(function (x) { return x.cats.length; }).sort(function (a, b) { return b.score - a.score; });
    c.byes = {}; c.starters.forEach(function (p) { (p.byes || []).forEach(function (w) { if (w >= lg.week) (c.byes[w] = c.byes[w] || []).push(p); }); });
  });

  /* ---------------- components ---------------- */
  function label(t, right) { return '<div class="label"><span>' + t + "</span>" + (right ? "<b>" + right + "</b>" : "") + "</div>"; }
  function chev() { return '<svg class="chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>'; }
  function rx(id, head, body, open) { return '<div class="rx' + (open ? " open" : "") + '" data-rx="' + esc(id) + '">' + head + '<div class="x"><div>' + body + "</div></div></div>"; }
  function prow(lg, p, o) {
    o = o || {};
    var meta = o.meta != null ? o.meta : posT(p.pos) + "<span>" + esc(p.team || "") + "</span>" + (o.ind != null ? o.ind : "");
    var cls = "row" + (o.b ? " b" : "") + (o.cls ? " " + o.cls : "") + (dead(p) && o.deadMark !== false ? " dead" : "");
    var val = o.val != null ? o.val : n1(wkVal(p)), unit = o.unit != null ? o.unit : "proj";
    var attrs = (o.b ? " data-xt" : (o.open ? ' data-player="' + esc(p.id) + '"' : ""));
    return '<div class="' + cls + '"' + attrs + ">" + (o.sign ? '<span class="sg ' + o.sign + '">' + (o.sign === "in" ? "+" : "−") + "</span>" : "") + (o.k != null ? '<span class="k' + (o.kbn ? " bn" : "") + '">' + esc(o.k) + "</span>" : "") +
      pic(p, o.pic || "") + '<span class="t"><b>' + esc(p.n) + "</b><small>" + meta + stTag(p) + "</small></span>" +
      (o.right != null ? o.right : '<span class="n"><span class="num">' + val + "</span><small>" + unit + "</small></span>") + (o.b ? chev() : "") + "</div>";
  }
  function spark(p) {
    var rows = (p.hist || []).filter(function (r) { return r.w < p.lgWeek; });
    if (rows.length < 2) return '<p class="empty">Not enough games.</p>';
    var W = 280, H = 56, pad = 12, n = rows.length;
    var max = Math.max.apply(null, rows.map(function (r) { return Math.max(r.pts || 0, r.proj || 0); }).concat([1]));
    var x = function (i) { return pad + i * (W - 2 * pad) / Math.max(1, n - 1); }, y = function (v) { return H - 14 - (v / max) * (H - 22); };
    var d = "", started = false, lastPt = null;
    rows.forEach(function (r, i) { if (!r.played) { started = false; return; } var pt = [x(i), y(r.pts || 0)]; d += (started ? " L" : " M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1); started = true; lastPt = pt; });
    var proj = rows.map(function (r, i) { return r.proj != null ? (i ? "L" : "M") + x(i).toFixed(1) + " " + y(r.proj).toFixed(1) : ""; }).join(" ");
    var lab = rows.map(function (r, i) { return '<text x="' + x(i).toFixed(1) + '" y="' + (H - 2) + '" text-anchor="middle"' + (r.played ? "" : ' class="bye"') + ">" + (r.played ? n0(r.pts) : "B") + "</text>"; }).join("");
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="Points by week">' + (proj ? '<path class="p" d="' + proj + '"/>' : "") + '<path class="l" d="' + d.trim() + '"/>' + (lastPt ? '<circle cx="' + lastPt[0].toFixed(1) + '" cy="' + lastPt[1].toFixed(1) + '" r="3"/>' : "") + lab + "</svg>" +
      '<p class="tiny"><span>Points, weeks ' + rows[0].w + "–" + rows[rows.length - 1].w + '</span><span class="dim">- - projected</span></p>';
  }
  function bar(v, max, cls, i) { var w = max ? Math.max(0, Math.min(1, v / max)) : 0; return '<div class="bar"><i class="' + (cls || "") + '" style="--w:' + w.toFixed(3) + ";--i:" + (i || 0) + '"></i></div>'; }
  function cmp(a, b, la, lb) {
    var max = Math.max(a || 0, b || 0, 1), s = sem((a || 0) - (b || 0), 0.5);
    return '<div class="cmp"><div class="bar l"><i class="' + (s.cls === "up" ? "" : "neu") + '" style="--w:' + ((a || 0) / max).toFixed(3) + '"></i></div><b>' + (la || n1(a)) + " · " + (lb || n1(b)) + '</b><div class="bar"><i class="' + (s.cls === "dn" ? "dn" : "neu") + '" style="--w:' + ((b || 0) / max).toFixed(3) + '"></i></div></div>';
  }
  function check(key) { return '<button class="check" data-done="' + esc(key) + '" aria-pressed="' + (S.done[key] ? "true" : "false") + '" aria-label="Done"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></button>'; }
  var KIND = { lineup: "Start", injury: "Bench", waiver: "Add", trade: "Trade for", plan: "Trade for", info: "" };
  var TAGW = { package: "Package deal", buy_low: "Buy low", sell_high: "Sell high", plan: "Multi-step" };
  function verb(a) { if (a.kind === "lineup" && /^Pick up/.test(a.title)) return "Pick up"; if (a.tag && TAGW[a.tag]) return TAGW[a.tag]; return KIND[a.kind] || "Move"; }
  function subject(lg, a) {
    var ps = a.players || [];
    if (a.kind === "trade" && a.trade) return a.trade.receive.map(function (p) { return byName(lg, p.name); }).filter(Boolean);
    if (a.kind === "plan" && a.trade) { var st = a.trade.steps || [a.trade.step1, a.trade.step2]; return st[st.length - 1].receive.map(function (p) { return byName(lg, p.name); }).filter(Boolean); }
    return ps.slice(0, 1).map(function (n) { return byName(lg, n); }).filter(Boolean);
  }
  function gainTxt(a) { return a.kind === "lineup" ? [sg(a.gain), "pts this week"] : [sg(a.gain, 0), "pts rest of season"]; }
  function line2(lg, a) {
    var ps = a.players || [], t = a.trade;
    if (a.kind === "trade" && t) return "Give " + names(t.send) + (a.note ? " · " + a.note : "");
    if (a.kind === "plan" && t) return (t.steps ? t.steps.length : 2) + " trades, in order · " + names(t.step1.receive) + " first";
    if (a.kind === "waiver" || /^Pick up/.test(a.title)) return ps[1] ? "Drop " + ps[1] : "Open roster spot";
    if (a.kind === "lineup") { var q = byName(lg, ps[1]); return "Bench " + ps[1] + (q && (q.bye || HURT(q)) ? " · " + (q.bye ? "bye" : "out") : ""); }
    if (a.kind === "injury") return "Out this week";
    return "";
  }
  function planSteps(lg, a) {
    if (a.kind !== "plan" || !a.trade) return "";
    var st = a.trade.steps || [a.trade.step1, a.trade.step2];
    return '<div class="plan">' + st.map(function (s, i) { var g = s.receive.map(function (p) { return byName(lg, p.name); }).filter(Boolean)[0]; return '<div class="pstep">' + (g ? pic(g, "sm") : '<span class="ph ini sm">' + (i + 1) + "</span>") + '<span class="t"><b>' + (i + 1) + ". Get " + esc(names(s.receive)) + "</b><small>give " + esc(names(s.send)) + " · " + esc(s.partner_name.trim()) + '</small></span><span class="num up">' + sg(s.their_gain, 0) + "</span></div>"; }).join("") + "</div>";
  }
  function moveCard(lg, a, showLg) {
    var subj = subject(lg, a)[0], g = gainTxt(a), done = !!S.done[a.key];
    var who = subj ? pic(subj, "lg") + '<span class="who"><b>' + esc(subj.n) + "</b><small>" + posT(subj.pos) + "<span>" + esc(subj.team || "") + "</span>" + stTag(subj) + "</small></span>" : '<span class="who"><b>' + esc(a.title) + "</b></span>";
    var acts = (subj ? '<button class="btn g" data-player="' + esc(subj.id) + '">View player</button>' : "") +
      (a.kind === "lineup" ? '<button class="btn g" data-nav="lineup" data-lg="' + lg.key + '">Lineup</button>' : a.msg ? '<button class="btn g" data-copy="' + esc(a.key) + '">Copy text</button>' : "") +
      '<span style="flex:1"></span>' + check(a.key);
    return '<div class="move' + (done ? " done" : "") + '"><div class="row" style="min-height:0;padding:0;border:0;gap:10px"><span class="tag ' + (a.urgency >= 2 ? "o" : "c") + '">' + esc(verb(a)) + "</span>" + (showLg ? '<span class="st" style="color:var(--fg3)">' + esc(lgShort(lg)) + "</span>" : "") + "</div>" +
      '<div class="head">' + who + "</div>" +
      '<div class="mid"><span class="gain"><span class="num">' + g[0] + "</span><small>" + g[1] + '</small></span><p class="sub">' + esc(line2(lg, a)) + "</p>" + planSteps(lg, a) + "</div>" +
      '<div class="acts">' + acts + "</div></div>";
  }
  function hero(lg, c) {
    var t = c.me, o = c.opp; if (!o) return '<div class="hero"><p class="empty">No matchup this week.</p></div>';
    var ms = c.live ? c.myScore : c.myProj, os = c.live ? c.oppScore : c.oppProj, share = (ms + os) > 0 ? ms / (ms + os) : 0.5;
    return '<div class="hero"><div class="names"><div><b>' + esc(t.name) + "</b><small>" + rec(t) + (t.standing ? " · " + ord(t.standing) + " place" : "") + '</small></div><div class="r"><b>' + esc(o.name.trim()) + "</b><small>" + rec(o) + (o.standing ? " · " + ord(o.standing) + " place" : "") + "</small></div></div>" +
      '<div class="scores"><span class="num" data-count="' + n1(ms) + '">' + n1(ms) + '</span><span class="mid">' + (c.live ? "SO FAR" : "PROJ") + '</span><span class="num r" data-count="' + n1(os) + '">' + n1(os) + "</span></div>" +
      (c.live ? '<div class="proj"><span>Proj <b>' + n1(c.myProj) + '</b></span><span class="r">Proj <b>' + n1(c.oppProj) + "</b></span></div>" : "") +
      '<div class="hbar"><i style="--w:' + share.toFixed(3) + '"></i></div>' +
      '<div class="foot"><span>' + c.left + ' still to play</span><span class="r">' + c.oppLeft + " still to play</span></div></div>";
  }

  /* ---------------- views ---------------- */
  function vHome() {
    var lg = L(), c = C[S.lg], i = 0, h = '<div class="v">';
    h += '<div style="--i:' + i++ + ';margin-top:4px">' + hero(lg, c) + "</div>";
    var all = []; LGS.forEach(function (k) { C[k].todo.forEach(function (a) { all.push(a); }); });
    all.sort(function (a, b) { return (b.urgency - a.urgency) || (b.gain - a.gain); });
    var todo = all.filter(function (a) { return !S.done[a.key]; }).slice(0, 3);
    h += '<div style="--i:' + i++ + '">' + label("Do this") + "</div>";
    if (!todo.length) h += '<p class="calm" style="--i:' + i++ + '"><b>Nothing to do.</b> Lineups are set.</p>';
    todo.forEach(function (a) { h += '<div style="--i:' + i++ + ';margin-bottom:10px">' + moveCard(D.leagues[a.lg], a, LGS.length > 1) + "</div>"; });
    h += '<div style="--i:' + i++ + '">' + label("Leagues") + '<div class="list">' + LGS.map(function (k) {
      var g = D.leagues[k], cc = C[k], t = cc.me, ms = cc.live ? cc.myScore : cc.myProj, os = cc.live ? cc.oppScore : cc.oppProj;
      var d = (ms || 0) - (os || 0), st = cc.live ? (Math.abs(d) < 3 ? ["◆ Close", "cau"] : d > 0 ? ["▲ Ahead", "up"] : ["▼ Behind", "dn"]) : (Math.abs(d) < 3 ? ["◆ Even", "cau"] : d > 0 ? ["▲ Favored", "up"] : ["▼ Underdog", "dn"]);
      return '<div class="row b lgrow" data-nav="matchup" data-lg="' + k + '"><span class="nm">' + esc(g.name) + '</span><span class="stx ' + st[1] + '">' + st[0] + "</span>" + chev() + '<span class="rc">' + rec(t) + (t.standing ? " · " + ord(t.standing) + " of " + g.teamCount : "") + ' · <b class="num">' + n1(ms) + "–" + n1(os) + "</b> " + (cc.live ? "so far" : "proj") + "</span></div>";
    }).join("") + "</div></div>";
    return h + "</div>";
  }

  function vMatchup() {
    var lg = L(), c = C[S.lg], t = c.me, o = c.opp, i = 0, h = '<div class="v">';
    h += '<div style="--i:' + i++ + ';margin-top:4px">' + hero(lg, c) + "</div>";
    if (o) {
      var mine = slotLines(lg, t).lines, theirs = slotLines(lg, o).lines;
      h += '<div style="--i:' + i++ + '">' + label("Position by position") + '<div class="list h2h">' + mine.map(function (m, idx) {
        var a = m.p, b = theirs[idx] ? theirs[idx].p : null;
        var av = a ? (a.played >= 100 ? a.wpts : wkVal(a)) : 0, bv = b ? (b.played >= 100 ? b.wpts : wkVal(b)) : 0, s = sem(av - bv, 1.5);
        var side = function (p, v, r) { return p ? '<div class="side' + (r ? " r" : "") + '" data-player="' + esc(p.id) + '"><b>' + esc(p.n) + "</b><small>" + (p.played >= 100 ? "final " : dead(p) ? (p.bye ? "bye · " : "out · ") : "") + n1(v) + "</small></div>" : '<div class="side' + (r ? " r" : "") + '"><b class="dim">—</b></div>'; };
        return '<div class="row">' + side(a, av, false) + '<div class="mid"><span>' + esc(SLOT_LABEL[m.slot] || m.slot) + '</span><b class="' + s.cls + '">' + s.g + " " + sg(av - bv) + "</b></div>" + side(b, bv, true) + "</div>";
      }).join("") + "</div></div>";
    }
    var st = lg.teams.slice().sort(function (a, b) { return (a.standing || 99) - (b.standing || 99) || (b.pf || 0) - (a.pf || 0); });
    h += '<div style="--i:' + i++ + '">' + label("Standings", "top " + lg.playoffTeams + " make playoffs") + '<table class="tbl"><thead><tr><th>#</th><th>Team</th><th class="n">W-L</th><th class="n">Odds</th></tr></thead><tbody>' +
      st.map(function (x, idx) { return '<tr class="' + (x.me ? "me" : "") + '" data-team="' + x.id + '"' + (lg.playoffTeams && idx === lg.playoffTeams ? ' style="border-top:2px solid var(--hair2)"' : "") + "><td>" + (x.standing || idx + 1) + "</td><td><b>" + esc(x.name.trim()) + '</b></td><td class="n">' + (rec(x) || "—") + '</td><td class="n">' + (x.po != null ? n0(x.po) + "%" : "—") + "</td></tr>"; }).join("") + "</tbody></table></div>";
    return h + "</div>";
  }

  function vLineup() {
    var lg = L(), c = C[S.lg], t = c.me, sl = slotLines(lg, t), my = roster(lg, t), best = c.bestWeek, gap = best.total - c.curWeek, i = 0, h = '<div class="v">';
    h += '<div class="kv" style="--i:' + i++ + ';margin-top:8px"><div><span class="num">' + n1(c.curWeek) + '</span><small>proj · week ' + lg.week + '</small></div><div><span class="num ' + (gap >= 1 ? "up" : "") + '">' + (gap >= 1 ? "▲ " + sg(gap) : "▲ Set") + "</span><small>" + (gap >= 1 ? "available" : "best lineup") + "</small></div></div>";
    h += '<div style="--i:' + i++ + '">' + label("Starters") + '<div class="list">';
    sl.lines.forEach(function (l) {
      var s0 = l.slot, p = l.p, k = SLOT_LABEL[s0] || s0;
      if (!p) { h += '<div class="lu dead"><span class="k">' + esc(k) + '</span><div class="main"><span class="ph ini">—</span><span class="t"><b class="dim">Empty</b></span><span class="n"><span class="num">0</span></span></div></div>'; return; }
      var elig = my.filter(function (q) { return q.slot === "BE" && (s0 === FLEX ? FLEXP[q.pos] : q.pos === s0); }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
      var alt = elig[0], sv = wkVal(p), av = alt ? wkVal(alt) : 0, d = av - sv, fin = p.played >= 100 && p.wpts != null;
      var fix = "";
      if (alt && dead(p) && av > 0) fix = '<div class="fix" data-cmp2="' + esc(p.id) + "," + esc(alt.id) + '">' + pic(alt) + "<span>Start <b>" + esc(alt.n) + '</b> instead</span><span class="g dn">▼ ' + (p.bye ? "bye" : "out") + "</span></div>";
      else if (alt && d >= 1.5) fix = '<div class="fix up" data-cmp2="' + esc(p.id) + "," + esc(alt.id) + '">' + pic(alt) + "<span>Start <b>" + esc(alt.n) + '</b> instead</span><span class="g up">▲ ' + sg(d) + "</span></div>";
      h += '<div class="lu' + (dead(p) ? " dead" : "") + '"><span class="k">' + esc(k) + '</span><div class="main" data-player="' + esc(p.id) + '">' + pic(p) + '<span class="t"><b>' + esc(p.n) + "</b><small>" + posT(p.pos) + "<span>" + esc(p.team || "") + "</span>" + stTag(p) + '</small></span><span class="n"><span class="num">' + (fin ? n1(p.wpts) : n1(sv)) + "</span><small>" + (fin ? "final" : "proj") + "</small></span></div>" + fix + "</div>";
    });
    h += "</div></div>";
    var bench = my.filter(function (p) { return !sl.used[p.id] && p.slot !== "IR"; }).sort(function (a, b) { return wkVal(b) - wkVal(a); }), ir = my.filter(function (p) { return p.slot === "IR"; });
    h += '<div style="--i:' + i++ + '">' + label("Bench") + '<div class="list">' + bench.map(function (p) { return prow(lg, p, { k: "BN", kbn: true, open: true, pic: "sm" }); }).join("") + ir.map(function (p) { return prow(lg, p, { k: "IR", kbn: true, open: true, pic: "sm" }); }).join("") + "</div></div>";
    var strength = Object.keys(lg.strength || {});
    if (strength.length) h += '<div style="--i:' + i++ + '">' + label("Strength", "vs the league") + '<div class="strength">' + strength.map(function (pos, k) { var s = lg.strength[pos], sm = sem(s.edge, 12); return "<div>" + posT(pos) + bar(s.mine, Math.max(s.mine, s.median) * 1.15, sm.cls === "cau" ? "neu" : sm.cls, k) + '<span class="num ' + sm.cls + '">' + sm.g + " " + sg(s.edge, 0) + "</span></div>"; }).join("") + "</div></div>";
    var weeks = []; for (var w = lg.week; w <= (lg.regWeeks || 14); w++) weeks.push(w);
    h += '<div style="--i:' + i++ + '">' + label("Byes ahead", "starters out") + '<div class="wk">' + weeks.map(function (w) { var n = (c.byes[w] || []).length; return '<div class="' + (w === lg.week ? "now" : "") + (n >= 2 ? " bad" : "") + '"><span>' + w + '</span><span class="num">' + (n || "–") + "</span></div>"; }).join("") + "</div></div>";
    return h + "</div>";
  }

  function vPlayers() {
    var lg = L(), i = 0, h = '<div class="v">';
    h += '<div class="search" style="--i:' + i++ + ';margin-top:6px"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg><input id="q" type="search" placeholder="Search players" value="' + esc(S.q) + '" autocomplete="off"></div>';
    h += '<div class="scroll" style="--i:' + i++ + ';margin-top:8px">' + ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"].map(function (p) { return '<button class="chipb" data-pill="pos:' + p + '" aria-pressed="' + (S.pos === p) + '">' + (p === "D/ST" ? "DST" : p === "ALL" ? "All" : p) + "</button>"; }).join("") + '<button class="chipb" data-pill="fav:1" aria-pressed="' + S.favOnly + '">★</button></div>';
    h += '<div class="ctl" style="--i:' + i++ + ';margin-top:8px"><select class="sel" id="avail" aria-label="Who">' + [["ALL", "Everyone"], ["FA", "Free agents"], ["ME", "My team"], ["OTH", "Other teams"]].map(function (x) { return '<option value="' + x[0] + '"' + (S.avail === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; }).join("") + "</select>" +
      '<select class="sel" id="sort" aria-label="Sort">' + [["wk", "This week"], ["proj", "Rest of season"], ["l3", "Last 3 games"], ["spts", "Points so far"], ["use", "Volume"]].map(function (x) { return '<option value="' + x[0] + '"' + (S.sort === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; }).join("") + "</select></div>";
    var list = filteredPlayers(lg), shown = list.slice(0, 30 + S.more * 30);
    h += '<div class="list" id="plist" style="--i:' + i++ + ';margin-top:10px">' + (shown.length ? shown.map(function (p) { return playerRow(lg, p); }).join("") : '<p class="empty">No players match.</p>') + "</div>";
    if (list.length > shown.length) h += '<button class="btn full g" data-more style="margin-top:12px">More</button>';
    if (S.cmp.length) h += '<button class="btn c full" data-sheet="compare" style="position:sticky;bottom:calc(96px + env(safe-area-inset-bottom,0px));margin-top:12px">Compare ' + S.cmp.length + "</button>";
    return h + "</div>";
  }
  function metric(p) {
    switch (S.sort) {
      case "proj": return [n0(sVal(p)), "rest of szn"]; case "l3": return [n1(last3(p)), "last 3"]; case "spts": return [n1(p.spts), "pts"];
      case "use": return [n1(usage3(p)), "touches"]; default: return [n1(wkVal(p)), "proj"];
    }
  }
  function sortVal(p) { switch (S.sort) { case "proj": return sVal(p); case "l3": return last3(p) || 0; case "spts": return p.spts || 0; case "use": return usage3(p) || 0; default: return wkVal(p); } }
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
    return prow(lg, p, { open: true, meta: posT(p.pos) + "<span>" + esc(p.team || "") + '</span><span class="dim">' + esc(own.length > 14 ? own.slice(0, 13) + "…" : own) + "</span>" + (S.favs[p.id] ? '<span class="st" style="color:var(--wr)">★</span>' : ""), val: m[0], unit: m[1] });
  }

  function vMoves() {
    var lg = L(), i = 0, h = '<div class="v">';
    h += '<div class="seg" style="--i:' + i++ + ';margin-top:6px">' + [["waivers", "Waivers"], ["trades", "Trades"]].map(function (x) { return '<button data-sub="' + x[0] + '" aria-pressed="' + (S.sub === x[0]) + '">' + x[1] + "</button>"; }).join("") + "</div>";
    h += S.sub === "waivers" ? vWaivers(lg, i) : vTrades(lg, i);
    return h + "</div>";
  }
  var WCATS = [["ALL", "All"], ["start", "Starts for you"], ["hot", "Hot hand"], ["role", "Role growing"]];
  var WWORD = { start: ["▲ Starter", "up"], week: ["▲ This week", "up"], hot: ["▲ Hot", "up"], role: ["▲ Role up", "up"], stash: ["● Depth", "neu"] };
  function vWaivers(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    var list = c.scout.slice(0, 8);
    h += '<div style="--i:' + i++ + '">' + label("Best available") + '<div class="list">';
    if (!list.length) h += '<p class="calm"><b>Nothing worth a claim.</b></p>';
    list.forEach(function (x) {
      var p = x.p, ww = WWORD[x.cats[0]], fit = x.gs >= 3 ? [sg(x.gs, 0), "rest of szn"] : x.gw >= 2 ? [sg(x.gw), "this week"] : [n1(wkVal(p)), "proj"], o = opp(p);
      var head = prow(lg, p, { b: true, ind: '<span class="' + ww[1] + '">' + ww[0] + "</span>", val: fit[0], unit: fit[1], deadMark: false });
      var body = '<div class="xin">' + spark(p) + '<div class="kv"><div><span class="num">' + n1(x.l3) + '</span><small>last 3</small></div><div><span class="num">' + (touchPos(p) ? n1(x.u3) : n1(wkVal(p))) + "</span><small>" + (touchPos(p) ? "touches" : "this week") + '</small></div><div><span class="num ' + o.cls + '">' + (o.r ? ord(o.r) : "—") + "</span><small>" + (p.bye ? "bye" : "opp defense") + "</small></div></div>" +
        '<p class="tiny"><span>' + (x.drop ? "Drop <b>" + esc(x.drop.n) + "</b>" : "Open roster spot") + "</span></p>" +
        '<div style="display:flex;gap:10px"><button class="btn g" data-player="' + esc(p.id) + '">View player</button></div></div>';
      h += rx("fa:" + lg.key + ":" + p.id, head, body, false);
    });
    return h + "</div></div>";
  }
  function vTrades(lg, i) {
    var c = C[lg.key], t = c.me, h = "";
    var ideas = c.moves.filter(function (a) { return a.kind === "trade" || a.kind === "plan"; });
    h += '<div style="--i:' + i++ + '">' + label("Ideas", ideas.length ? "both teams get better" : "") + "</div>";
    if (!ideas.length) h += '<p class="calm" style="--i:' + i++ + '"><b>No trade helps both sides right now.</b></p>';
    ideas.forEach(function (a) { h += '<div style="--i:' + i++ + ';margin-bottom:10px">' + moveCard(lg, a, false) + "</div>"; });
    if (c.alts.length) h += '<div class="list" style="--i:' + i++ + '">' + rx("alts", '<div class="row b" data-xt><span class="t"><b>' + c.alts.length + " more ideas</b></span>" + chev() + "</div>", '<div class="xin flush"><div class="list">' + c.alts.map(function (a) { var tr = a.trade; return '<div class="row"><span class="t"><b>Get ' + esc(names(tr.receive)) + "</b><small>for " + esc(names(tr.send)) + " · " + esc(tr.partner_name.trim()) + '</small></span><span class="n"><span class="num up">' + sg(tr.my_gain, 0) + "</span><small>rest of szn</small></span></div>"; }).join("") + "</div></div>", false) + "</div>";
    var others = lg.teams.filter(function (x) { return !x.me; });
    if (!S.tl.partner || !team(lg, S.tl.partner) || team(lg, S.tl.partner).me) S.tl.partner = others[0].id;
    var them = team(lg, S.tl.partner), my = roster(lg, t), theirs = roster(lg, them);
    h += '<div style="--i:' + i++ + '">' + label("Try a trade") + '<div class="ctl"><select class="sel" id="tl-partner" aria-label="Partner">' + others.map(function (x) { return '<option value="' + x.id + '"' + (x.id === S.tl.partner ? " selected" : "") + ">" + esc(x.name.trim()) + "</option>"; }).join("") + "</select>" +
      '<div class="seg" style="padding:3px"><button data-set="horizon:season" aria-pressed="' + (S.horizon === "season") + '" style="min-height:36px">Season</button><button data-set="horizon:week" aria-pressed="' + (S.horizon === "week") + '" style="min-height:36px">Week</button></div></div>' +
      label("You get") + '<div class="pchips">' + chips(lg, theirs, "get") + "</div>" + label("You give") + '<div class="pchips">' + chips(lg, my, "send") + "</div>" +
      '<div id="tl-out" style="margin-top:16px">' + tradeVerdict(lg) + "</div></div>";
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
    if (!get.length || !send.length) return '<div class="verdict"><p class="w dim">Pick players on both sides</p></div>';
    var wk = S.horizon === "week", val = wk ? wkVal : sVal;
    var b0 = bestLineup(my, lg.slots, val), b1 = bestLineup(swap(my, send, get), lg.slots, val), t0 = lv(theirs, lg.slots, val), t1 = lv(swap(theirs, get, send), lg.slots, val);
    var mg = b1.total - b0.total, tg = t1 - t0, rawS = send.reduce(function (s, p) { return s + sVal(p); }, 0), rawG = get.reduce(function (s, p) { return s + sVal(p); }, 0), ratio = rawG ? rawS / rawG : 1;
    var take = wk ? S.takeAt / 4 : S.takeAt, win = wk ? 1 : 3, word, cls;
    if (mg >= take) { word = "▲ Take it"; cls = "up"; } else if (mg >= win) { word = "▲ Small win"; cls = "up"; } else if (mg > -win) { word = "◆ Even"; cls = "cau"; } else { word = "▼ Pass"; cls = "dn"; }
    var hurt = get.filter(HURT), byes = wk ? get.filter(function (p) { return p.bye; }) : [];
    var ids0 = {}; b0.starters.forEach(function (s) { ids0[s.p.id] = 1; }); var ids1 = {}; b1.starters.forEach(function (s) { ids1[s.p.id] = 1; });
    var fair = ratio >= 0.85 ? "▲ Looks fair" : ratio >= 0.7 ? "◆ Looks a little light" : "▼ Looks lopsided · likely declined";
    var counter = "";
    if (mg < win) { var cc = bestCounter(lg, them, my, theirs, val); if (cc) counter = '<p class="line">Try <b>' + esc(names(cc.send)) + "</b> for <b>" + esc(cc.get.n) + "</b> · you " + sg(cc.mg, 0) + " · them " + sg(cc.tg, 0) + "</p>"; }
    var lineup = function (b, ids) { return '<div class="list">' + b.starters.map(function (s) { return '<div class="row' + (!ids[s.p.id] ? " in" : "") + '"><span class="k">' + esc(SLOT_LABEL[s.slot] || s.slot) + '</span><span class="t"><b>' + esc(shortName(s.p.n)) + '</b></span><span class="n"><span class="num">' + (wk ? n1(s.v) : n0(s.v)) + "</span></span></div>"; }).join("") + "</div>"; };
    return '<div class="verdict ' + cls + ' pop"><p class="w">' + word + "</p>" +
      '<div class="kv"><div><span class="num ' + sem(mg, win).cls + '">' + sg(mg, wk ? 1 : 0) + '</span><small>you</small></div><div><span class="num ' + sem(tg, win).cls + '">' + sg(tg, wk ? 1 : 0) + "</span><small>them</small></div></div>" +
      '<p class="line">' + fair + (hurt.length ? " · ▼ " + esc(names(hurt)) + " hurt" : "") + (byes.length ? " · ▼ " + esc(names(byes)) + " on bye" : "") + "</p>" + counter +
      rx("tl-x", '<div class="row b" style="min-height:44px" data-xt><span class="t"><b>Your lineup before · after</b></span>' + chev() + "</div>", '<div class="xin flush"><div class="two"><div>' + lineup(b0, ids1) + "</div><div>" + lineup(b1, ids0) + "</div></div></div>", false) + "</div>";
  }
  function bestCounter(lg, them, my, theirs, val) {
    var mine0 = lv(my, lg.slots, val), th0 = lv(theirs, lg.slots, val), best = null;
    var mine = my.filter(function (p) { return p.proj != null && !S.untouch[p.id]; }), packs = [];
    mine.forEach(function (p) { packs.push([p]); });
    for (var i = 0; i < mine.length; i++) for (var j = i + 1; j < mine.length; j++) packs.push([mine[i], mine[j]]);
    theirs.forEach(function (g) {
      if (g.proj == null || HURT(g)) return;
      packs.forEach(function (pk) {
        var raw = pk.reduce(function (s, p) { return s + sVal(p); }, 0); if (raw < 0.7 * sVal(g)) return;
        var mg = lv(swap(my, pk, [g]), lg.slots, val) - mine0, tg = lv(swap(theirs, [g], pk), lg.slots, val) - th0;
        if (mg >= 5 && tg >= 12 && (!best || mg + tg > best.mg + best.tg)) best = { send: pk, get: g, mg: mg, tg: tg };
      });
    });
    return best;
  }

  /* ---------------- sheets ---------------- */
  function sheet(html) { return '<div class="scrim" data-close></div><div class="sheet" role="dialog" aria-modal="true">' + html + "</div>"; }
  function playerSheet(pid) {
    var lg = L(), p = P(lg, pid); if (!p) return "";
    var o = opp(p), l3 = last3(p), sa = seasonAvg(p), rows = playedRows(p), last = rows[rows.length - 1], read = [];
    if (l3 != null && sa != null && rows.length >= 3) { var d = l3 - sa; if (d >= 3) read.push("▲ Heating up · " + sg(d) + " vs his average"); else if (d <= -3) read.push("▼ Cooling off · " + sg(d) + " vs his average"); }
    if (last && rows.length > 1) { var pu = avg(rows.slice(0, -1).map(touches)); if (touches(last) >= 1.4 * pu && touches(last) >= 8) read.push("▲ Getting the ball more · " + n0(touches(last)) + " touches last game"); }
    if (o.r && o.cls !== "neu" && !p.bye) read.push(o.g + " " + (o.cls === "up" ? "Easy" : "Tough") + " matchup this week");
    if (p.bye) read.push("▼ Bye this week"); if (HURT(p)) read.push("▼ " + (p.inj || "").replace("_", " ")); if (Q(p)) read.push("◆ Questionable");
    var fut = (p.byes || []).filter(function (w) { return w > lg.week; }); if (fut.length) read.push("● Bye week " + fut.join(", "));
    var fit = "";
    if (p.owner == null) { var c = C[lg.key], my = roster(lg, c.me), gs = lv(my.concat([p]), lg.slots, sVal) - c.bestSeason.total, gw = lv(my.concat([p]), lg.slots, wkVal) - c.bestWeek.total; fit = '<div class="kv"><div><span class="num ' + (gs > 0 ? "up" : "neu") + '">' + sg(gs, 0) + '</span><small>for you · rest of season</small></div><div><span class="num ' + (gw > 0 ? "up" : "neu") + '">' + sg(gw) + "</span><small>for you · this week</small></div></div>"; }
    return sheet('<div class="phead">' + pic(p, "xl") + '<span class="t"><b>' + esc(p.n) + "</b><small>" + posT(p.pos) + "<span>" + esc(p.team || "") + '</span><span class="dim">' + esc(ownerWord(lg, p)) + "</span>" + stTag(p) + "</small></span>" + '<button class="btn g star" data-fav="' + esc(p.id) + '" aria-pressed="' + !!S.favs[p.id] + '" style="min-width:46px;padding:0 12px" aria-label="Favorite"><svg viewBox="0 0 24 24"><path d="M12 3l2.8 5.9 6.4.8-4.7 4.5 1.2 6.4L12 17.5 6.3 20.6l1.2-6.4L2.8 9.7l6.4-.8z"/></svg></button></div>' +
      '<div class="kv"><div><span class="num">' + n1(wkVal(p)) + '</span><small>this week</small></div><div><span class="num">' + n1(l3) + '</span><small>last 3</small></div><div><span class="num">' + n0(sVal(p)) + "</span><small>rest of season</small></div></div>" +
      (o.w ? '<p class="tiny"><span class="' + o.cls + '">' + (o.g ? o.g + " " : "") + esc(o.w) + (o.r ? " (" + ord(o.r) + " vs " + esc(p.pos) + ")" : "") + "</span></p>" : "") +
      spark(p) +
      (read.length ? '<div class="list">' + read.map(function (w) { return '<div class="row" style="min-height:40px;padding:6px 0"><span class="t"><b style="font-size:16px">' + esc(w) + "</b></span></div>"; }).join("") + "</div>" : "") + fit +
      '<div style="display:flex;gap:10px"><button class="btn full" data-cmp="' + esc(p.id) + '" aria-pressed="' + (S.cmp.indexOf(p.id) >= 0) + '">' + (S.cmp.indexOf(p.id) >= 0 ? "In compare" : "Compare") + '</button><button class="btn full c" data-close>Close</button></div>');
  }
  function compareSheet() {
    var lg = L(), ps = S.cmp.map(function (id) { return P(lg, id); }).filter(Boolean);
    if (ps.length < 2) return sheet('<p class="calm"><b>Pick two players.</b> Open a player, tap Compare.</p><button class="btn full" data-close>Close</button>');
    var a = ps[0], b = ps[1], rows = [["This week", wkVal(a), wkVal(b), 1], ["Last 3 games", last3(a), last3(b), 1], ["Rest of season", sVal(a), sVal(b), 0], ["Touches / game", usage3(a), usage3(b), 1]];
    return sheet('<div class="two"><div class="phead">' + pic(a, "lg") + '<span class="t"><b style="font-size:18px">' + esc(a.n) + "</b><small>" + posT(a.pos) + esc(a.team || "") + stTag(a) + '</small></span></div><div class="phead">' + pic(b, "lg") + '<span class="t"><b style="font-size:18px">' + esc(b.n) + "</b><small>" + posT(b.pos) + esc(b.team || "") + stTag(b) + "</small></span></div></div>" +
      rows.map(function (r) { var fa = r[3] ? n1(r[1]) : n0(r[1]), fb = r[3] ? n1(r[2]) : n0(r[2]); return '<div><div class="label" style="margin:4px 0 8px">' + r[0] + "</div>" + cmp(r[1] || 0, r[2] || 0, fa, fb) + "</div>"; }).join("") +
      '<p class="tiny"><span>' + esc(shortName(a.n)) + " " + esc(opp(a).w || "") + "</span><span>" + esc(shortName(b.n)) + " " + esc(opp(b).w || "") + "</span></p>" +
      '<div style="display:flex;gap:10px"><button class="btn full g" data-cmp-clear>Clear</button><button class="btn full c" data-close>Close</button></div>');
  }
  function teamSheet(id) {
    var lg = L(), t = team(lg, id); if (!t) return "";
    var sl = slotLines(lg, t), bench = roster(lg, t).filter(function (p) { return !sl.used[p.id]; }).sort(function (a, b) { return wkVal(b) - wkVal(a); });
    return sheet('<div class="phead"><span class="t"><b>' + esc(t.name.trim()) + "</b><small>" + (rec(t) ? rec(t) + " · " : "") + ord(C[lg.key].rankByProj.indexOf(t.id) + 1) + " strongest roster</small></span></div>" +
      '<div class="list">' + sl.lines.map(function (l) { return l.p ? prow(lg, l.p, { k: SLOT_LABEL[l.slot] || l.slot, open: true, pic: "sm" }) : ""; }).join("") + bench.map(function (p) { return prow(lg, p, { k: p.slot === "IR" ? "IR" : "BN", kbn: true, open: true, pic: "sm" }); }).join("") + "</div>" +
      '<button class="btn full" data-close>Close</button>');
  }
  function settingsSheet() {
    var lg = L(), my = roster(lg, C[lg.key].me);
    var opt = function (field, vals, cur) { return '<div class="opts">' + vals.map(function (v) { return '<button data-set="' + field + ":" + v[0] + '" aria-pressed="' + (cur === v[0]) + '">' + v[1] + "</button>"; }).join("") + "</div>"; };
    return sheet('<div class="phead"><span class="t"><b>Settings</b></span></div>' +
      label("Color") + opt("accent", [["sky", "Cyan"], ["violet", "Violet"], ["mint", "Mint"]], S.accent) +
      label("Motion") + opt("motion", [["auto", "On"], ["off", "Off"]], S.motion) +
      label("Never trade") + '<div class="pchips">' + my.filter(function (p) { return p.proj; }).sort(function (a, b) { return b.proj - a.proj; }).map(function (p) { return '<button class="pc" data-untouch="' + esc(p.id) + '" aria-pressed="' + !!S.untouch[p.id] + '">' + pic(p) + posT(p.pos) + esc(shortName(p.n)) + "</button>"; }).join("") + "</div>" +
      label("Data") + '<p class="sub">' + LGS.map(function (k) { return esc(D.leagues[k].name) + " · updated " + esc(ago(D.leagues[k].pulledAt).txt) + " ago"; }).join("<br>") + "<br>ESPN is pulled twice a day.</p>" +
      '<div style="display:flex;gap:10px"><button class="btn full g" data-reset>Reset checks</button><button class="btn full c" data-close>Done</button></div>');
  }

  /* ---------------- shell ---------------- */
  var TITLES = { home: "Command Center", matchup: "Matchup", lineup: "Lineup", players: "Players", moves: "Moves" };
  function header() {
    var latest = LGS.map(function (k) { return D.leagues[k].pulledAt; }).filter(Boolean).sort().pop() || D.generated, f = ago(latest);
    return '<div class="brand"><div class="logo"><i>TyG</i><span><b>' + TITLES[S.tab] + "</b><small>Week " + esc(D.week) + " · updated " + esc(f.txt) + " ago</small></span></div>" +
      '<button class="gear" data-sheet="settings" aria-label="Settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></button></div>' +
      (LGS.length > 1 ? '<div class="lgsw">' + LGS.map(function (k) { var n = C[k].todo.filter(function (a) { return !S.done[a.key]; }).length; return '<button data-lg="' + k + '" aria-pressed="' + (S.lg === k) + '"><span>' + esc(D.leagues[k].name) + "</span>" + (n ? "<i>" + n + "</i>" : "") + "</button>"; }).join("") + "</div>" : "");
  }
  function nav() {
    var icons = { home: '<path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/>', matchup: '<path d="M8 21h8M12 17v4M6 4h12v4a6 6 0 01-12 0zM6 6H3v2a3 3 0 003 3M18 6h3v2a3 3 0 01-3 3"/>', lineup: '<path d="M4 7h16M4 12h16M4 17h10"/>', players: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>', moves: '<path d="M7 7h12l-3-3M17 17H5l3 3"/>' };
    return TABS.map(function (t) { return '<button data-nav="' + t + '" aria-pressed="' + (S.tab === t) + '"><svg viewBox="0 0 24 24">' + icons[t] + "</svg>" + t + "</button>"; }).join("");
  }
  function render(soft) {
    document.documentElement.dataset.accent = S.accent; document.documentElement.dataset.motion = S.motion;
    $("#top").innerHTML = header();
    var main = $("#main"); main.classList.toggle("soft", !!soft);
    main.innerHTML = S.tab === "home" ? vHome() : S.tab === "matchup" ? vMatchup() : S.tab === "lineup" ? vLineup() : S.tab === "players" ? vPlayers() : vMoves();
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
    var el = e.target.closest("[data-nav],[data-sub],[data-lg],[data-xt],[data-done],[data-copy],[data-fav],[data-cmp],[data-cmp2],[data-cmp-clear],[data-player],[data-team],[data-sheet],[data-close],[data-set],[data-pill],[data-more],[data-tl],[data-untouch],[data-reset]");
    if (!el) return;
    var ds = el.dataset;
    if (ds.close != null) { S.sheet = null; render(true); return; }
    if (ds.sheet) { S.sheet = ds.sheet; render(true); return; }
    if (ds.nav) { go(ds.nav, { sub: ds.sub, lg: ds.lg }); return; }
    if (ds.sub) { S.sub = ds.sub; put("sub", S.sub); render(); return; }
    if (ds.lg) { if (S.lg !== ds.lg) { S.lg = ds.lg; put("lg", S.lg); S.cmp = []; S.tl = { partner: null, get: {}, send: {} }; render(); window.scrollTo(0, 0); } return; }
    if (ds.xt != null) { var box = el.closest(".rx"); if (!box) return; box.classList.toggle("open"); var o = box.classList.contains("open"); if (box.dataset.rx) { if (o) S.open[box.dataset.rx] = 1; else delete S.open[box.dataset.rx]; } return; }
    if (ds.done) { if (S.done[ds.done]) delete S.done[ds.done]; else S.done[ds.done] = 1; put("done", S.done); el.setAttribute("aria-pressed", String(!!S.done[ds.done])); var wrap = el.closest(".move"); if (wrap) wrap.classList.toggle("done", !!S.done[ds.done]); if (S.done[ds.done]) toast("Done", "Make the move in the ESPN app."); $("#top").innerHTML = header(); return; }
    if (ds.copy) { var txt = (window.FCC_MSG || {})[ds.copy] || ""; var ok = function () { toast("Copied", txt); }, no = function () { toast("Copy this", txt); }; try { navigator.clipboard.writeText(txt).then(ok, no); } catch (err) { no(); } return; }
    if (ds.fav) { if (S.favs[ds.fav]) delete S.favs[ds.fav]; else S.favs[ds.fav] = 1; put("favs", S.favs); el.setAttribute("aria-pressed", String(!!S.favs[ds.fav])); toast(S.favs[ds.fav] ? "Favorited" : "Removed"); return; }
    if (ds.cmp2) { S.cmp = ds.cmp2.split(","); S.sheet = "compare"; render(true); return; }
    if (ds.cmp) { var ix = S.cmp.indexOf(ds.cmp); if (ix >= 0) S.cmp.splice(ix, 1); else { S.cmp.push(ds.cmp); if (S.cmp.length > 2) S.cmp.shift(); } if (S.cmp.length === 2) S.sheet = "compare"; else toast("Pick one more player"); render(true); return; }
    if (ds.cmpClear != null) { S.cmp = []; S.sheet = null; render(true); return; }
    if (ds.player) { S.sheet = "player:" + ds.player; render(true); return; }
    if (ds.team) { S.sheet = "team:" + ds.team; render(true); return; }
    if (ds.set) { var kv = ds.set.split(":"), f = kv[0], v = kv[1]; S[f] = v; if (["lg", "accent", "motion"].indexOf(f) >= 0) put(f, v); if (f === "horizon") { $("#tl-out").innerHTML = tradeVerdict(L()); $$("[data-set^='horizon:']").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.set === "horizon:" + v)); }); return; } render(true); return; }
    if (ds.pill) { var pk = ds.pill.split(":"); if (pk[0] === "fav") S.favOnly = !S.favOnly; else S[pk[0]] = pk[1]; S.more = 0; render(true); return; }
    if (ds.more != null) { S.more++; render(true); return; }
    if (ds.tl) { var sp = ds.tl.split(":"), side = sp[0], id = sp.slice(1).join(":"); if (side === "send" && S.untouch[id]) { toast("Marked never trade", "Change it in Settings."); return; } if (S.tl[side][id]) delete S.tl[side][id]; else S.tl[side][id] = 1; el.setAttribute("aria-pressed", String(!!S.tl[side][id])); $("#tl-out").innerHTML = tradeVerdict(L()); return; }
    if (ds.untouch) { if (S.untouch[ds.untouch]) delete S.untouch[ds.untouch]; else S.untouch[ds.untouch] = 1; put("untouch", S.untouch); el.setAttribute("aria-pressed", String(!!S.untouch[ds.untouch])); return; }
    if (ds.reset != null) { S.done = {}; S.favs = {}; S.cmp = []; put("done", S.done); put("favs", S.favs); toast("Reset"); render(true); return; }
  });
  document.addEventListener("input", function (e) {
    if (e.target.id === "q") { S.q = e.target.value; S.more = 0; var lg = L(), list = filteredPlayers(lg).slice(0, 30), ul = $("#plist"); if (ul) ul.innerHTML = list.length ? list.map(function (p) { return playerRow(lg, p); }).join("") : '<p class="empty">No players match.</p>'; }
  });
  document.addEventListener("change", function (e) {
    if (e.target.id === "sort") { S.sort = e.target.value; S.more = 0; render(true); }
    if (e.target.id === "avail") { S.avail = e.target.value; S.more = 0; render(true); }
    if (e.target.id === "tl-partner") { S.tl = { partner: +e.target.value, get: {}, send: {} }; render(true); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && S.sheet) { S.sheet = null; render(true); } });

  render();
})();
