"""LeagueContext -- the structural guarantee that League 1 can never see League 2.

The requirement is that the *same* NFL event produces different, independent
recommendations in each league. That only holds if league state never mixes,
and "the developer will remember to add a WHERE clause" is not a guarantee.

So isolation is enforced three ways, at runtime, on every query:

  1. Any SQL touching a league-scoped table MUST bind ``:league_id``. Forgetting
     the filter raises instead of silently returning both leagues' rows.
  2. The caller cannot supply ``league_id`` themselves -- the context injects its
     own. There is no code path that reaches another league's data.
  3. Every returned row carrying a ``league_id`` column is verified to match.
     Belt, braces, and a second pair of braces.

The cost is a few microseconds per query. The benefit is that the headline
failure mode of this product is unreachable rather than merely unlikely.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any, Sequence

from ff.db.store import Store, utcnow, content_hash
from ff.logging_setup import get_logger

log = get_logger(__name__)

# Tables whose rows are meaningless without a league. Every one of these has
# league_id in its PRIMARY KEY (see schema.sql).
LEAGUE_SCOPED_TABLES = frozenset(
    {
        "leagues",
        "league_teams",
        "league_rosters",
        "league_free_agents",
        "league_matchups",
        "league_transactions",
        "league_state",
        "league_projections",
        "opponent_profiles",
        "recommendations",
    }
)

# `preferences` is deliberately excluded: a NULL league_id there means "applies
# to both leagues", which is a legitimate global row rather than a leak.

_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)


class LeagueIsolationError(RuntimeError):
    """A query would have crossed a league boundary. Always a bug, never expected."""


class LeagueContext:
    """Every read and write for exactly one league. Cannot address another."""

    def __init__(self, store: Store, league_id: int, league_key: str, name: str):
        self.store = store
        self.league_id = int(league_id)
        self.league_key = league_key
        self.name = name

    def __repr__(self) -> str:
        return f"<LeagueContext {self.league_key} id={self.league_id} {self.name!r}>"

    # -- guarded query surface --------------------------------------------

    def _guard(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        tables = {m.lower() for m in _TABLE_REF.findall(sql)}
        touches_scoped = bool(tables & LEAGUE_SCOPED_TABLES)

        if touches_scoped and ":league_id" not in sql:
            offenders = sorted(tables & LEAGUE_SCOPED_TABLES)
            raise LeagueIsolationError(
                f"query touches league-scoped table(s) {offenders} without binding "
                f":league_id -- this would read across leagues. SQL: {sql.strip()[:200]}"
            )

        if "league_id" in params:
            raise LeagueIsolationError(
                "callers must not pass league_id; the context supplies its own. "
                f"(context is {self.league_key}/{self.league_id})"
            )

        merged = dict(params)
        merged["league_id"] = self.league_id
        return merged

    def _verify_rows(self, rows: Sequence[sqlite3.Row]) -> None:
        for row in rows:
            if "league_id" in row.keys():
                value = row["league_id"]
                if value is not None and int(value) != self.league_id:
                    raise LeagueIsolationError(
                        f"row from league {value} surfaced in context "
                        f"{self.league_key}/{self.league_id}"
                    )

    def query(self, sql: str, **params: Any) -> list[sqlite3.Row]:
        rows = self.store.conn.execute(sql, self._guard(sql, params)).fetchall()
        self._verify_rows(rows)
        return rows

    def one(self, sql: str, **params: Any) -> sqlite3.Row | None:
        row = self.store.conn.execute(sql, self._guard(sql, params)).fetchone()
        if row is not None:
            self._verify_rows([row])
        return row

    def execute(self, sql: str, **params: Any) -> sqlite3.Cursor:
        return self.store.conn.execute(sql, self._guard(sql, params))

    def commit(self) -> None:
        self.store.commit()

    # -- league identity ---------------------------------------------------

    def settings(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM leagues WHERE league_id = :league_id")

    def scoring(self) -> dict[str, Any]:
        row = self.settings()
        if not row or not row["scoring_json"]:
            return {}
        return json.loads(row["scoring_json"])

    def roster_slots(self) -> dict[str, int]:
        row = self.settings()
        if not row or not row["roster_json"]:
            return {}
        return json.loads(row["roster_json"])

    @property
    def uses_faab(self) -> bool:
        row = self.settings()
        return bool(row and (row["waiver_type"] or "").lower() == "faab")

    def my_team_id(self) -> int | None:
        row = self.settings()
        return row["my_team_id"] if row else None

    def current_week(self) -> int:
        row = self.settings()
        return (row["current_week"] if row else None) or 1

    # -- teams and rosters -------------------------------------------------

    def teams(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM league_teams WHERE league_id = :league_id ORDER BY standing, team_id"
        )

    def my_team(self) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM league_teams WHERE league_id = :league_id AND is_mine = 1"
        )

    def opponents(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM league_teams WHERE league_id = :league_id AND is_mine = 0 "
            "ORDER BY standing, team_id"
        )

    def roster(self, team_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT r.*, p.full_name, p.position, p.nfl_team, p.injury_status "
            "FROM league_rosters r LEFT JOIN nfl_players p ON p.player_id = r.player_id "
            "WHERE r.league_id = :league_id AND r.team_id = :team_id",
            team_id=team_id,
        )

    def my_roster(self) -> list[sqlite3.Row]:
        team_id = self.my_team_id()
        return self.roster(team_id) if team_id is not None else []

    def rosters_by_team(self) -> dict[int, list[sqlite3.Row]]:
        rows = self.query(
            "SELECT r.*, p.full_name, p.position, p.nfl_team, p.injury_status "
            "FROM league_rosters r LEFT JOIN nfl_players p ON p.player_id = r.player_id "
            "WHERE r.league_id = :league_id"
        )
        out: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            out.setdefault(row["team_id"], []).append(row)
        return out

    # -- availability: the crux of per-league divergence -------------------

    def is_rostered(self, player_id: str) -> bool:
        return (
            self.one(
                "SELECT 1 AS hit FROM league_rosters "
                "WHERE league_id = :league_id AND player_id = :pid",
                pid=player_id,
            )
            is not None
        )

    def rostered_by(self, player_id: str) -> int | None:
        row = self.one(
            "SELECT team_id FROM league_rosters "
            "WHERE league_id = :league_id AND player_id = :pid",
            pid=player_id,
        )
        return row["team_id"] if row else None

    def is_available(self, player_id: str) -> bool:
        """Free agent / on waivers in THIS league.

        The single most important query in the system: the same player is
        routinely rostered in one league and sitting on waivers in the other,
        and that is what makes the two recommendations differ.
        """
        return (
            self.one(
                "SELECT 1 AS hit FROM league_free_agents "
                "WHERE league_id = :league_id AND player_id = :pid",
                pid=player_id,
            )
            is not None
        )

    def i_own(self, player_id: str) -> bool:
        team_id = self.my_team_id()
        if team_id is None:
            return False
        return self.rostered_by(player_id) == team_id

    def free_agents(self, limit: int = 300) -> list[sqlite3.Row]:
        return self.query(
            "SELECT f.*, p.full_name, p.position, p.nfl_team, p.injury_status "
            "FROM league_free_agents f LEFT JOIN nfl_players p ON p.player_id = f.player_id "
            "WHERE f.league_id = :league_id "
            "ORDER BY COALESCE(f.pct_owned, 0) DESC LIMIT :lim",
            lim=limit,
        )

    def availability(self, player_id: str) -> str:
        """One of: mine | opponent | available | unknown."""
        owner = self.rostered_by(player_id)
        if owner is not None:
            return "mine" if owner == self.my_team_id() else "opponent"
        if self.is_available(player_id):
            return "available"
        return "unknown"

    # -- projections (league-scoped: scoring settings differ) --------------

    def set_projection(
        self,
        player_id: str,
        week: int,
        projected: float | None,
        season_avg: float | None = None,
        last_actual: float | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO league_projections(league_id, player_id, week, projected, "
            "season_avg, last_actual, updated_at) "
            "VALUES(:league_id, :pid, :week, :proj, :avg, :last, :ts) "
            "ON CONFLICT(league_id, player_id, week) DO UPDATE SET "
            "projected = excluded.projected, season_avg = excluded.season_avg, "
            "last_actual = excluded.last_actual, updated_at = excluded.updated_at",
            pid=player_id, week=week, proj=projected, avg=season_avg,
            last=last_actual, ts=utcnow(),
        )

    def projection(self, player_id: str, week: int) -> float | None:
        row = self.one(
            "SELECT projected, season_avg FROM league_projections "
            "WHERE league_id = :league_id AND player_id = :pid AND week = :week",
            pid=player_id, week=week,
        )
        if not row:
            return None
        # Fall back to season average when a weekly projection is missing --
        # but never invent one when neither exists.
        return row["projected"] if row["projected"] is not None else row["season_avg"]

    def projections(self, week: int) -> dict[str, float]:
        rows = self.query(
            "SELECT player_id, projected, season_avg FROM league_projections "
            "WHERE league_id = :league_id AND week = :week",
            week=week,
        )
        out: dict[str, float] = {}
        for row in rows:
            value = row["projected"] if row["projected"] is not None else row["season_avg"]
            if value is not None:
                out[row["player_id"]] = float(value)
        return out

    def season_average(self, player_id: str) -> float | None:
        row = self.one(
            "SELECT AVG(season_avg) AS a FROM league_projections "
            "WHERE league_id = :league_id AND player_id = :pid AND season_avg IS NOT NULL",
            pid=player_id,
        )
        return row["a"] if row and row["a"] is not None else None

    # -- schedule ----------------------------------------------------------

    def matchup(self, week: int, team_id: int | None = None) -> sqlite3.Row | None:
        team_id = team_id if team_id is not None else self.my_team_id()
        if team_id is None:
            return None
        return self.one(
            "SELECT * FROM league_matchups "
            "WHERE league_id = :league_id AND week = :week AND team_id = :team_id",
            week=week,
            team_id=team_id,
        )

    def remaining_schedule(self, from_week: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM league_matchups "
            "WHERE league_id = :league_id AND week >= :week AND completed = 0 "
            "ORDER BY week, team_id",
            week=from_week,
        )

    def faab_remaining(self, team_id: int | None = None) -> int | None:
        team_id = team_id if team_id is not None else self.my_team_id()
        if team_id is None:
            return None
        row = self.one(
            "SELECT faab_remaining FROM league_teams "
            "WHERE league_id = :league_id AND team_id = :team_id",
            team_id=team_id,
        )
        return row["faab_remaining"] if row else None

    # -- change detection, scoped to this league ---------------------------

    def state_changed(self, key: str, payload: Any) -> bool:
        new = content_hash(payload)
        row = self.one(
            "SELECT hash FROM league_state WHERE league_id = :league_id AND key = :key",
            key=key,
        )
        if row and row["hash"] == new:
            return False
        self.execute(
            "INSERT INTO league_state(league_id, key, hash, updated_at) "
            "VALUES(:league_id, :key, :hash, :ts) "
            "ON CONFLICT(league_id, key) DO UPDATE SET "
            "hash = excluded.hash, updated_at = excluded.updated_at",
            key=key,
            hash=new,
            ts=utcnow(),
        )
        return True

    # -- recommendations ---------------------------------------------------

    def record_recommendation(self, rec: dict[str, Any]) -> str:
        rec_id = rec.get("rec_id") or f"rec_{uuid.uuid4().hex[:12]}"
        self.execute(
            """
            INSERT INTO recommendations(
                rec_id, league_id, created_at, week, action, urgency, priority,
                subject_player_id, related_player_ids, summary, rationale,
                evidence_json, assumptions, faab_low, faab_high,
                championship_before, championship_after, championship_delta,
                confidence, triggering_event_id, model
            ) VALUES(
                :rec_id, :league_id, :created_at, :week, :action, :urgency, :priority,
                :subject_player_id, :related_player_ids, :summary, :rationale,
                :evidence_json, :assumptions, :faab_low, :faab_high,
                :cb, :ca, :cd, :confidence, :triggering_event_id, :model
            )
            """,
            rec_id=rec_id,
            created_at=rec.get("created_at") or utcnow(),
            week=rec.get("week"),
            action=rec["action"],
            urgency=rec.get("urgency", "MONITOR"),
            priority=rec.get("priority", "LOW"),
            subject_player_id=rec.get("subject_player_id"),
            related_player_ids=json.dumps(rec.get("related_player_ids") or []),
            summary=rec["summary"],
            rationale=rec.get("rationale"),
            evidence_json=json.dumps(rec.get("evidence") or {}, default=str),
            assumptions=rec.get("assumptions"),
            faab_low=rec.get("faab_low"),
            faab_high=rec.get("faab_high"),
            cb=rec.get("championship_before"),
            ca=rec.get("championship_after"),
            cd=rec.get("championship_delta"),
            confidence=rec.get("confidence"),
            triggering_event_id=rec.get("triggering_event_id"),
            model=rec.get("model", "deterministic"),
        )
        self.commit()
        return rec_id

    def recent_recommendations(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM recommendations WHERE league_id = :league_id "
            "ORDER BY created_at DESC LIMIT :lim",
            lim=limit,
        )

    # -- preferences -------------------------------------------------------

    def preferences(self) -> dict[str, list[str]]:
        rows = self.store.preferences(self.league_id)
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["key"], []).append(row["value"])
        return out

    def posture(self) -> str:
        """balanced | aggressive | conservative | win_now | playoffs."""
        return (self.preferences().get("posture") or ["balanced"])[-1]

    def untouchable_players(self) -> set[str]:
        return set(self.preferences().get("never_trade", []))

    def avoided_teams(self) -> set[str]:
        return set(self.preferences().get("avoid_team", []))


def open_contexts(store: Store, settings: Any) -> dict[str, LeagueContext]:
    """One independent context per configured league. They share nothing."""
    contexts: dict[str, LeagueContext] = {}
    for lg in settings.leagues:
        contexts[lg.key] = LeagueContext(store, lg.league_id, lg.key, lg.name)
    return contexts
