-- Fantasy GM state.
--
-- Two kinds of table, and the split is the core safety property of this system:
--
--   GLOBAL tables hold NFL facts that are true regardless of league -- a player
--   is injured, a coordinator was fired, it is going to snow in Buffalo. These
--   are collected once and shared. They have NO league_id.
--
--   LEAGUE-SCOPED tables hold anything whose meaning depends on which league
--   you are in. Every one of them carries league_id as part of its PRIMARY KEY,
--   so a row physically cannot exist without declaring which league it belongs
--   to, and no unique constraint can collide across leagues.
--
-- The same NFL event must be able to produce "start him" in one league and
-- "claim him" in the other. That only works if league state never mixes.

PRAGMA foreign_keys = ON;

-- ===========================================================================
-- GLOBAL: shared NFL intelligence
-- ===========================================================================

CREATE TABLE IF NOT EXISTS nfl_players (
    player_id     TEXT PRIMARY KEY,          -- our canonical id (see ff/identity.py)
    full_name     TEXT NOT NULL,
    normalized    TEXT NOT NULL,             -- lowercased, punctuation-stripped
    position      TEXT,
    nfl_team      TEXT,
    status        TEXT,                      -- Active / Injured Reserve / ...
    injury_status TEXT,                      -- Questionable / Doubtful / Out / ...
    age           INTEGER,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_norm ON nfl_players(normalized);
CREATE INDEX IF NOT EXISTS idx_players_team_pos ON nfl_players(nfl_team, position);

-- Maps our canonical player_id to each source's own id. This is what lets an
-- ESPN roster, a Sleeper injury note, and an nflverse snap count refer to the
-- same human being.
CREATE TABLE IF NOT EXISTS player_ids (
    source     TEXT NOT NULL,                -- espn | sleeper | nflverse | fantasypros
    source_id  TEXT NOT NULL,
    player_id  TEXT NOT NULL REFERENCES nfl_players(player_id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,    -- <1.0 means fuzzy-matched, treat with care
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_player_ids_player ON player_ids(player_id);

-- Normalized NFL events. fingerprint dedupes the same news arriving from
-- several sources or being re-polled.
CREATE TABLE IF NOT EXISTS nfl_events (
    event_id     TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- injury_status | practice | inactive |
                                             -- role_change | depth_chart | trade |
                                             -- coaching | weather | news | usage
    player_id    TEXT REFERENCES nfl_players(player_id) ON DELETE SET NULL,
    nfl_team     TEXT,
    severity     TEXT NOT NULL DEFAULT 'info',   -- info | notable | major
    headline     TEXT NOT NULL,
    body         TEXT,
    old_value    TEXT,                       -- e.g. "Questionable"
    new_value    TEXT,                       -- e.g. "Out"  (the actual change)
    source       TEXT NOT NULL,
    source_tier  INTEGER NOT NULL DEFAULT 5, -- 1 = official, 7 = social rumor
    source_url   TEXT,
    published_at TEXT,                       -- source's own timestamp, if any
    first_seen_at TEXT NOT NULL,             -- when WE first saw it
    verified     INTEGER NOT NULL DEFAULT 0, -- corroborated by >=2 independent sources
    stale        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_fingerprint ON nfl_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_events_player ON nfl_events(player_id, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_seen ON nfl_events(first_seen_at DESC);

-- Weekly usage / role signal (nflverse). The difference between "he scored 12"
-- and "he played 78% of snaps with a 24% target share" -- the second predicts.
CREATE TABLE IF NOT EXISTS player_usage (
    player_id    TEXT NOT NULL REFERENCES nfl_players(player_id) ON DELETE CASCADE,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    snap_pct     REAL,
    route_pct    REAL,
    target_share REAL,
    carry_share  REAL,
    rz_touches   INTEGER,
    gl_carries   INTEGER,
    fantasy_pts  REAL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS game_environment (
    game_key      TEXT PRIMARY KEY,          -- e.g. 2026-W03-KC@BUF
    season        INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    kickoff_utc   TEXT,
    indoor        INTEGER NOT NULL DEFAULT 0,
    temp_f        REAL,
    wind_mph      REAL,
    precip_pct    REAL,
    implied_total REAL,
    spread        REAL,
    updated_at    TEXT NOT NULL
);

-- ===========================================================================
-- LEAGUE-SCOPED: every PK starts with league_id
-- ===========================================================================

CREATE TABLE IF NOT EXISTS leagues (
    league_id     INTEGER PRIMARY KEY,
    league_key    TEXT NOT NULL UNIQUE,      -- L1 / L2
    name          TEXT NOT NULL,
    season        INTEGER NOT NULL,
    my_team_id    INTEGER,
    scoring_json  TEXT,                      -- raw scoring settings
    roster_json   TEXT,                      -- starting slots + counts
    waiver_type   TEXT,                      -- faab | rolling | reverse
    faab_budget   INTEGER,
    playoff_teams INTEGER,
    reg_season_weeks INTEGER,
    current_week  INTEGER,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league_teams (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    team_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    owner       TEXT,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    ties        INTEGER NOT NULL DEFAULT 0,
    points_for  REAL NOT NULL DEFAULT 0,
    points_against REAL NOT NULL DEFAULT 0,
    standing    INTEGER,
    faab_remaining INTEGER,
    waiver_priority INTEGER,
    is_mine     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (league_id, team_id)
);

CREATE TABLE IF NOT EXISTS league_rosters (
    league_id  INTEGER NOT NULL,
    team_id    INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    slot       TEXT,                         -- QB/RB/WR/TE/FLEX/BE/IR
    acquired   TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (league_id, team_id, player_id),
    FOREIGN KEY (league_id, team_id) REFERENCES league_teams(league_id, team_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rosters_player ON league_rosters(league_id, player_id);

-- A player is available in one league and rostered in the other. This table is
-- exactly why league_id cannot be optional anywhere.
CREATE TABLE IF NOT EXISTS league_free_agents (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    player_id   TEXT NOT NULL,
    pct_owned   REAL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (league_id, player_id)
);

-- Projections are league-scoped on purpose: the same player is worth different
-- points in a full-PPR league than in a half-PPR one, so a projection without a
-- league attached is meaningless.
CREATE TABLE IF NOT EXISTS league_projections (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    player_id   TEXT NOT NULL,
    week        INTEGER NOT NULL,
    projected   REAL,
    season_avg  REAL,
    last_actual REAL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (league_id, player_id, week)
);

-- Draft rankings and ADP, league-scoped because ESPN computes draft ranks
-- against the league's own scoring. A PPR league and a standard league produce
-- genuinely different boards, and using one for the other is how people reach
-- two rounds early on a possession receiver.
CREATE TABLE IF NOT EXISTS draft_rankings (
    league_id     INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    player_id     TEXT NOT NULL,
    adp           REAL,      -- average draft position across ESPN leagues
    adp_change    REAL,      -- recent movement; a fast riser is news
    draft_rank    INTEGER,   -- ESPN's own rank under this scoring
    auction_value REAL,
    pct_drafted   REAL,
    projected     REAL,      -- season projection under this league's scoring
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (league_id, player_id)
);

-- Picks as they happen. Polled during a live draft.
CREATE TABLE IF NOT EXISTS draft_picks (
    league_id    INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    overall_pick INTEGER NOT NULL,
    round_num    INTEGER,
    round_pick   INTEGER,
    team_id      INTEGER,
    player_id    TEXT,
    keeper       INTEGER NOT NULL DEFAULT 0,
    bid_amount   INTEGER,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (league_id, overall_pick)
);

CREATE TABLE IF NOT EXISTS league_matchups (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    week        INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    opponent_id INTEGER,
    team_score  REAL,
    opp_score   REAL,
    is_playoff  INTEGER NOT NULL DEFAULT 0,
    completed   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (league_id, week, team_id)
);

CREATE TABLE IF NOT EXISTS league_transactions (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    txn_id      TEXT NOT NULL,
    week        INTEGER,
    type        TEXT,                        -- ADD / DROP / TRADE / WAIVER
    team_id     INTEGER,
    player_id   TEXT,
    bid_amount  INTEGER,
    occurred_at TEXT,
    raw_json    TEXT,
    PRIMARY KEY (league_id, txn_id)
);

-- Learned manager behaviour: FAAB aggression, trade willingness, positional
-- bias. Built up from observed transactions over the season.
CREATE TABLE IF NOT EXISTS opponent_profiles (
    league_id       INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL,
    trades_proposed INTEGER NOT NULL DEFAULT 0,
    trades_accepted INTEGER NOT NULL DEFAULT 0,
    waiver_claims   INTEGER NOT NULL DEFAULT 0,
    avg_faab_bid    REAL,
    max_faab_bid    INTEGER,
    faab_spent      INTEGER NOT NULL DEFAULT 0,
    position_bias   TEXT,                    -- JSON: {"RB": 0.4, "WR": 0.3, ...}
    notes           TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (league_id, team_id)
);

-- Content hashes for cheap stage-1 change detection. If nothing here changed,
-- we exit before spending a token.
CREATE TABLE IF NOT EXISTS league_state (
    league_id  INTEGER NOT NULL,
    key        TEXT NOT NULL,
    hash       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (league_id, key)
);

CREATE TABLE IF NOT EXISTS global_state (
    key        TEXT PRIMARY KEY,
    hash       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ===========================================================================
-- Decisions, delivery, and the audit trail that makes calibration possible
-- ===========================================================================

CREATE TABLE IF NOT EXISTS recommendations (
    rec_id            TEXT PRIMARY KEY,
    league_id         INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    created_at        TEXT NOT NULL,
    week              INTEGER,
    action            TEXT NOT NULL,         -- START/BENCH/ADD/DROP/CLAIM/TRADE/HOLD/...
    urgency           TEXT NOT NULL,         -- NOW / TODAY / THIS_WEEK / MONITOR
    priority          TEXT NOT NULL,         -- CRITICAL / HIGH / MEDIUM / LOW
    subject_player_id TEXT,
    related_player_ids TEXT,                 -- JSON array
    summary           TEXT NOT NULL,
    rationale         TEXT,
    evidence_json     TEXT,                  -- facts + sources it was based on
    assumptions       TEXT,
    faab_low          INTEGER,
    faab_high         INTEGER,
    championship_before REAL,
    championship_after  REAL,
    championship_delta  REAL,
    confidence        REAL,                  -- 0..1, calibration-tracked
    triggering_event_id TEXT,
    model             TEXT,                  -- which model wrote it, or 'deterministic'
    -- Filled in later, once reality happened. This is what makes the
    -- confidence numbers meaningful instead of decorative.
    outcome           TEXT,                  -- followed / ignored / n_a
    outcome_note      TEXT,
    outcome_recorded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recs_league ON recommendations(league_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications_sent (
    fingerprint  TEXT PRIMARY KEY,           -- dedupe key: event + league + action + urgency
    sent_at      TEXT NOT NULL,
    priority     TEXT NOT NULL,
    league_ids   TEXT NOT NULL,              -- JSON array; cross-league alerts list both
    rec_ids      TEXT NOT NULL,              -- JSON array
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    channel      TEXT,
    delivered    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

-- User overrides: "never trade Bijan", "be aggressive", "I need to win now".
-- league_id NULL means the preference applies to both leagues.
CREATE TABLE IF NOT EXISTS preferences (
    pref_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id  INTEGER,
    key        TEXT NOT NULL,                -- posture | never_trade | avoid_team |
                                             -- faab_appetite | min_priority
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prefs ON preferences(league_id, key);

CREATE TABLE IF NOT EXISTS run_log (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT,                       -- schedule | manual | game_window
    changes_found INTEGER NOT NULL DEFAULT 0,
    stage2_invoked INTEGER NOT NULL DEFAULT 0,
    notifications INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
