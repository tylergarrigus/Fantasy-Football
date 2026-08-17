# Fantasy Football GM

A background agent that watches two ESPN fantasy leagues and the NFL, and contacts you
only when something should change what you do.

Not an app. Not a dashboard. There is nothing to open. It runs on a schedule, decides
whether anything material happened, and if so sends one notification telling you exactly
what to do in each league. Most of the time it correctly does nothing.

---

## The one thing it has to get right

The same NFL event routinely means different things in different leagues:

```
Christian McCaffrey ruled OUT
        │
        ├── League 1: you already roster Jordan Mason  →  START MASON
        └── League 2: Mason is sitting on waivers      →  CLAIM MASON, $21-$32
```

One event, two independent evaluations, one notification. If the system ever gave the
same advice in both leagues just because the news was the same, it would be worthless.

That requirement is enforced structurally, not by discipline. Every league-scoped table
carries `league_id` in its primary key, and all access goes through a `LeagueContext`
that refuses any query which doesn't bind its own league, rejects a caller trying to pass
a different one, and re-verifies every returned row. See `ff/db/context.py`, and
`tests/test_league_isolation.py` / `tests/test_critical_divergence.py` for the tests that
would fail loudly if it ever broke.

See it for yourself, with no credentials and no network:

```bash
python scripts/demo_cycle.py
```

---

## How it works

```
STAGE 1 — cheap detection            pure Python, $0, runs every cycle
  Poll ESPN, Sleeper, news, weather → hash → diff against last run
  Nothing changed?  exit.  ← this is what happens almost every time
        │ something changed
STAGE 1.5 — relevance filter         pure Python, $0
  Does it touch a player rostered or available in either league?
  No?  exit.  An injury to a player neither league can use is not our problem.
        │ relevant
STAGE 2 — analysis                   deterministic engines, then Claude Opus 5
  Valuation · roster · opponent · waiver/FAAB · trade · lineup
  Monte Carlo season simulation → playoff % and championship %
  Claude weighs the evidence, decides if it's worth interrupting you, writes it
        │
  dedupe → priority gate → Apprise → your phone
```

**The arithmetic is code, not the model.** Championship probability comes from a seeded
Monte Carlo simulator over the actual remaining schedule (`ff/engines/simulate.py`).
Asking a language model to produce "18% → 23%" would be inventing precision it has no way
to compute. The model's job is judgment and prose; it is explicitly forbidden from
introducing a number that wasn't given to it.

**Cost.** Stage 2 only runs when something material *and* relevant changed — realistically
a handful of times a week. Stage 1 runs ~50×/day and costs nothing. The stable league
context is prompt-cached, so repeat calls within the hour pay ~10% on that prefix.

---

## Setup

### 1. Configure

```bash
cp .env.example .env
# fill in FF_LEAGUE_1_ID, FF_LEAGUE_2_ID, FF_NOTIFY_URL, ANTHROPIC_API_KEY
ff doctor          # checks config without touching the network
```

Your league ID is the `leagueId=` number in your ESPN URL.

### 2. Notifications

Channel is one config line, because switching should be trivial:

```bash
FF_NOTIFY_URL=ntfy://ntfy.sh/pick-a-hard-to-guess-topic   # free, no account
FF_NOTIFY_URL=pover://USER_KEY@APP_TOKEN                  # $4.99 once, more reliable on iOS
FF_NOTIFY_URL=discord://webhook_id/webhook_token
```

Start with ntfy — it's free and needs no signup. One honest caveat: there's a known open
issue where ntfy notifications can arrive **silently on iOS 26.2+**, which is bad for the
"your RB is out, kickoff is in 40 minutes" case. If that bites, switch to Pushover; it's
an environment variable, not a rewrite.

### 3. ESPN authentication — only if your leagues are private

There is no official ESPN fantasy API, no OAuth, and no API key. Private leagues
authenticate with two browser cookies, and password login is blocked by reCAPTCHA and
cannot be automated.

The agent **tries public access first** and only uses cookies if ESPN refuses. If it does
need them:

1. Log into ESPN in your browser
2. DevTools → Application (Chrome) or Storage (Firefox) → Cookies → `espn.com`
3. Copy `espn_s2` and `SWID`

These are **account-wide bearer credentials** — treat them like a password. They go in
GitHub Secrets, are read from the environment only, and every value is registered with a
log redactor so they cannot leak into a public Actions log
(`tests/test_notify_and_resilience.py` covers this).

### 4. Deploy

Add these as **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Required | What |
|---|---|---|
| `FF_LEAGUE_1_ID`, `FF_LEAGUE_2_ID` | yes | ESPN league IDs |
| `FF_NOTIFY_URL` | yes | where alerts go |
| `ANTHROPIC_API_KEY` | yes | stage-2 reasoning |
| `ESPN_S2`, `ESPN_SWID` | only if private | ESPN cookies |
| `FANTASYPROS_API_KEY` | no | supplemental consensus rankings |

Then run **Actions → Monitor → Run workflow** manually once to verify before trusting the
schedule. New repos sometimes take 12–48h to register cron.

---

## Talking to it

The notification is the product; the CLI is for interrogating it.

```bash
ff status                 # what should I do right now?
ff odds                   # championship probability, both leagues
ff lineup                 # start/sit, scored by win probability
ff waivers                # ranked targets with FAAB ranges
ff trades                 # deals worth proposing, and who'd accept
ff changed --hours 24     # what moved in the NFL
ff watching               # everything under observation
ff why                    # recent alerts; `ff why <id>` for full reasoning
ff run --deep             # force a cycle now
```

Standing preferences are remembered and shape every future recommendation:

```bash
ff prefer posture win_now
ff prefer posture conservative --league L2
ff prefer never_trade p_bijan
ff prefer avoid_team "Dave's Team"
```

---

## Design decisions worth knowing about

**Start/sit is scored by win probability, not projected points.** If you're a heavy
favourite the right move is the higher floor; if you're a heavy underdog the safe lineup
loses slowly and you need the tail. Optimising expected points gets both cases wrong.

**Waiver advice compares against *your* roster, not against the player.** "Is he good" is
the wrong question; "is he better than the worst guy you'd actually have to start" is the
right one. Most waiver adds fail that test and are reported as `IGNORE`.

**FAAB is always a range**, calibrated to how your league actually bids — not a generic
rule. A league where winning bids are routinely $40 gets different advice from one where
$8 takes anything.

**Trades are filtered by whether a human would accept them.** A deal that's fair on a
value chart but obviously unacceptable to the person receiving it is noise.

**Nothing is invented.** Unverifiable is `DATA UNVERIFIED`. Missing is `DATA UNAVAILABLE`.
Sources disagreeing is `DATA CONFLICT — NO ACTION RECOMMENDED UNTIL VERIFIED`, and that
suppresses the recommendation rather than picking a side.

**No fake precision.** `~70%`, not `73.42%`. Confidence is rounded to 5% because the model
isn't calibrated finer than that.

**Every recommendation is stored** with its evidence, assumptions, and confidence, so
confidence can be scored against outcomes later instead of being decorative.

---

## Data sources

| Source | Auth | Used for | If it fails |
|---|---|---|---|
| ESPN Fantasy v3 (unofficial) | cookies, private only | leagues, rosters, waivers, projections | cycle reports the error; other league continues |
| Sleeper | none | player universe, ESPN↔nflverse ID crosswalk, injuries, trending | stale cache, marked stale |
| ESPN site API (unofficial) | none | news, schedule, venue | news degrades, rest continues |
| nflverse | none, optional dep | snap share, target share, usage trends | `DATA UNAVAILABLE` |
| Open-Meteo | none | wind and precipitation for outdoor games | `DATA UNAVAILABLE` |
| FantasyPros | API key, optional | consensus rankings cross-check | silently skipped |

ESPN's endpoints are undocumented and unsupported — reverse-engineered from their own web
client. They work reliably in practice, but everything here is written to fail soft.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q                                  # 60 tests, fully offline
pytest tests/test_critical_divergence.py -v  # the test that matters most
```

The entire suite runs without network access or credentials. If a test needs a secret to
pass, the test is wrong.

```
ff/
  config.py          secrets, settings, log redaction registry
  db/context.py      LeagueContext — the isolation guarantee
  identity.py        cross-source player resolution
  sources/           ESPN, Sleeper, news, nflverse, weather
  intel/news.py      dedup, corroboration, staleness, conflict detection
  engines/           valuation, roster, opponent, waiver, trade, lineup, simulate
  decide/            decision engine + Claude adjudication
  notify/            formatting, dedup, delivery
  cycle.py           the three-stage monitoring cycle
```

---

## Status

Everything above is built and tested against fixtures. **No live ESPN call has been made
yet** — the development environment's network policy blocks all sports-data hosts, so the
first real data pull happens on the GitHub Actions runner. Expect to iterate once on the
ESPN response shape when it does.

Known gaps, stated plainly rather than hidden:

- **Bye weeks** are not yet ingested; `ff status` reports that gap as `DATA UNAVAILABLE`
  rather than pretending byes don't exist.
- **Player scoring is modelled as independent**, which is wrong for a QB stacked with his
  own receivers. It slightly overstates confidence on stacked lineups.
- **Playoff seeding** assumes ESPN's default tiebreak (record, then points-for). Leagues
  with custom tiebreaks will be marginally off.
- **Position volatility** uses heuristic priors rather than season-fitted variance until
  enough games exist to fit it.
