"""ESPN Fantasy Football.

There is no official ESPN fantasy API. What exists is the undocumented v3
endpoint ESPN's own web app calls, now served from `lm-api-reads.fantasy.espn.com`.
Private leagues authenticate with two browser cookies (`espn_s2`, `SWID`);
username/password login is blocked by reCAPTCHA and cannot be automated.

We use cwendt94/espn-api (MIT, ~940 stars, actively maintained -- last fixes May
2026) rather than reimplementing the endpoint, because it already handles the
401 endpoint-format retry and the response shape churn. What it does *not* have,
verified by reading its request layer, is any rate-limit or 429 handling. So we
own throttling and retry here.

Auth policy: always try public access first. Cookies are account-wide bearer
credentials, and there is no reason to put them on the wire for a public league.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ff.config import ESPNCredentials, LeagueConfig
from ff.db.context import LeagueContext
from ff.db.store import Store, utcnow
from ff.identity import PlayerRegistry, normalize_team
from ff.logging_setup import get_logger
from ff.sources.base import SourceAuthError, SourceNotFound, SourceUnavailable

log = get_logger(__name__)

# ESPN slot codes -> our labels. Anything not listed is bench-equivalent.
SLOT_NAMES = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K",
    23: "FLEX", 20: "BE", 21: "IR",
}
STARTING_SLOTS = {"QB", "RB", "WR", "TE", "FLEX", "DST", "K", "OP", "SUPERFLEX"}

_MIN_INTERVAL = 1.0  # seconds between ESPN calls; the library won't do this for us
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


@dataclass
class LeagueSyncResult:
    league_id: int
    league_key: str
    teams: int
    rostered: int
    free_agents: int
    used_auth: bool
    changed_keys: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.changed_keys)


class ESPNFantasySource:
    def __init__(self, store: Store, registry: PlayerRegistry, creds: ESPNCredentials):
        self.store = store
        self.registry = registry
        self.creds = creds
        self._cache: dict[int, Any] = {}

    # -- connection --------------------------------------------------------

    def connect(self, cfg: LeagueConfig, *, force_auth: bool = False) -> tuple[Any, bool]:
        """Return (espn_api.football.League, used_auth).

        Tries public access first, falls back to cookies only if ESPN refuses.
        """
        if cfg.league_id in self._cache:
            return self._cache[cfg.league_id]

        try:
            from espn_api.football import League
            from espn_api.requests.espn_requests import (
                ESPNAccessDenied,
                ESPNInvalidLeague,
                ESPNUnknownError,
            )
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SourceUnavailable(f"espn-api not installed: {exc}") from exc

        attempts: list[tuple[bool, dict[str, str] | None]] = []
        if not force_auth:
            attempts.append((False, None))
        if self.creds.configured:
            attempts.append((True, self.creds.as_cookies()))

        last: Exception | None = None
        for used_auth, cookies in attempts:
            _throttle()
            try:
                kwargs: dict[str, Any] = {"league_id": cfg.league_id, "year": cfg.season}
                if cookies:
                    kwargs["espn_s2"] = cookies["espn_s2"]
                    kwargs["swid"] = cookies["SWID"]
                league = League(**kwargs)
                log.info(
                    "espn: connected to %s (league %s) %s",
                    cfg.name, cfg.league_id,
                    "with cookies" if used_auth else "as public league",
                )
                self._cache[cfg.league_id] = (league, used_auth)
                return league, used_auth
            except ESPNAccessDenied as exc:
                last = exc
                continue
            except ESPNInvalidLeague as exc:
                raise SourceNotFound(
                    f"ESPN does not recognise league {cfg.league_id} for {cfg.season}. "
                    "Check the leagueId in your ESPN URL and the season year."
                ) from exc
            except ESPNUnknownError as exc:
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001 - library raises bare errors too
                last = exc
                continue

        if self.creds.configured:
            raise SourceAuthError(
                f"ESPN refused league {cfg.league_id} even with cookies. They have "
                "most likely expired -- re-extract espn_s2 and SWID from your "
                f"browser and update the secrets. ({last})"
            )
        raise SourceAuthError(
            f"League {cfg.league_id} is private and no ESPN cookies are configured. "
            "Set ESPN_S2 and ESPN_SWID (see .env.example for how to extract them)."
        )

    # -- sync --------------------------------------------------------------

    def sync(self, ctx: LeagueContext, cfg: LeagueConfig) -> LeagueSyncResult:
        """Pull the full league state into the DB, scoped to this league only.

        Returns which state keys changed -- that list is stage 1's entire output
        for this league, and an empty list means we can stop without spending
        anything on analysis.
        """
        league, used_auth = self.connect(cfg)
        now = utcnow()
        changed: list[str] = []

        my_team_id = cfg.team_id or self._detect_my_team(league)
        settings = getattr(league, "settings", None)

        scoring = self._extract_scoring(settings)
        roster_slots = self._extract_roster_slots(settings)
        waiver_type, faab_budget = self._extract_waiver(settings)

        ctx.execute(
            """
            INSERT INTO leagues(league_id, league_key, name, season, my_team_id,
                                scoring_json, roster_json, waiver_type, faab_budget,
                                playoff_teams, reg_season_weeks, current_week, updated_at)
            VALUES(:league_id, :key, :name, :season, :my_team, :scoring, :roster,
                   :waiver, :faab, :playoff, :regweeks, :week, :ts)
            ON CONFLICT(league_id) DO UPDATE SET
                name = excluded.name, season = excluded.season,
                my_team_id = COALESCE(excluded.my_team_id, leagues.my_team_id),
                scoring_json = excluded.scoring_json, roster_json = excluded.roster_json,
                waiver_type = excluded.waiver_type, faab_budget = excluded.faab_budget,
                playoff_teams = excluded.playoff_teams,
                reg_season_weeks = excluded.reg_season_weeks,
                current_week = excluded.current_week, updated_at = excluded.updated_at
            """,
            key=cfg.key,
            name=cfg.name,
            season=cfg.season,
            my_team=my_team_id,
            scoring=json.dumps(scoring),
            roster=json.dumps(roster_slots),
            waiver=waiver_type,
            faab=faab_budget,
            playoff=getattr(settings, "playoff_team_count", None),
            regweeks=getattr(settings, "reg_season_count", None),
            week=getattr(league, "current_week", None),
            ts=now,
        )

        teams = list(getattr(league, "teams", []) or [])
        rostered = self._sync_teams_and_rosters(ctx, league, teams, my_team_id, now)
        if ctx.state_changed("rosters", rostered["fingerprint"]):
            changed.append("rosters")
        if ctx.state_changed("standings", rostered["standings"]):
            changed.append("standings")

        fa_count, fa_fingerprint = self._sync_free_agents(ctx, league, now)
        if ctx.state_changed("free_agents", fa_fingerprint):
            changed.append("free_agents")

        matchup_fp = self._sync_matchups(ctx, league, teams, now)
        if ctx.state_changed("matchups", matchup_fp):
            changed.append("matchups")

        txn_fp = self._sync_transactions(ctx, league, now)
        if txn_fp and ctx.state_changed("transactions", txn_fp):
            changed.append("transactions")

        ctx.commit()
        return LeagueSyncResult(
            league_id=cfg.league_id,
            league_key=cfg.key,
            teams=len(teams),
            rostered=rostered["count"],
            free_agents=fa_count,
            used_auth=used_auth,
            changed_keys=changed,
        )

    # -- sync internals ----------------------------------------------------

    def _detect_my_team(self, league: Any) -> int | None:
        """Find the user's team from the SWID, when we have one."""
        if not self.creds.configured:
            return None
        swid = self.creds.swid.strip("{}").upper()
        for team in getattr(league, "teams", []) or []:
            owners = getattr(team, "owners", None) or []
            for owner in owners:
                oid = owner.get("id") if isinstance(owner, dict) else str(owner)
                if oid and str(oid).strip("{}").upper() == swid:
                    return team.team_id
        return None

    def _sync_teams_and_rosters(
        self, ctx: LeagueContext, league: Any, teams: list[Any], my_team_id: int | None, now: str
    ) -> dict[str, Any]:
        fingerprint: dict[str, list[str]] = {}
        standings: dict[str, Any] = {}
        total = 0

        # Resolve every rostered player's identity in one pass.
        entries = []
        for team in teams:
            for p in getattr(team, "roster", []) or []:
                entries.append(
                    (getattr(p, "playerId", None), getattr(p, "name", ""),
                     getattr(p, "position", None), normalize_team(getattr(p, "proTeam", None)))
                )
        id_map = self.registry.bulk_resolve_espn([e for e in entries if e[0] is not None])

        for team in teams:
            team_id = team.team_id
            ctx.execute(
                """
                INSERT INTO league_teams(league_id, team_id, name, owner, wins, losses,
                                         ties, points_for, points_against, standing,
                                         faab_remaining, waiver_priority, is_mine, updated_at)
                VALUES(:league_id, :tid, :name, :owner, :w, :l, :t, :pf, :pa, :standing,
                       :faab, :prio, :mine, :ts)
                ON CONFLICT(league_id, team_id) DO UPDATE SET
                    name = excluded.name, owner = excluded.owner,
                    wins = excluded.wins, losses = excluded.losses, ties = excluded.ties,
                    points_for = excluded.points_for, points_against = excluded.points_against,
                    standing = excluded.standing, faab_remaining = excluded.faab_remaining,
                    waiver_priority = excluded.waiver_priority, is_mine = excluded.is_mine,
                    updated_at = excluded.updated_at
                """,
                tid=team_id,
                name=getattr(team, "team_name", f"Team {team_id}"),
                owner=self._owner_name(team),
                w=getattr(team, "wins", 0) or 0,
                l=getattr(team, "losses", 0) or 0,
                t=getattr(team, "ties", 0) or 0,
                pf=float(getattr(team, "points_for", 0) or 0),
                pa=float(getattr(team, "points_against", 0) or 0),
                standing=getattr(team, "standing", None),
                faab=getattr(team, "acquisition_budget_spent", None) is not None
                and self._faab_left(league, team)
                or getattr(team, "faab_remaining", None),
                prio=getattr(team, "waiver_position", None),
                mine=1 if (my_team_id is not None and team_id == my_team_id) else 0,
                ts=now,
            )

            ctx.execute(
                "DELETE FROM league_rosters WHERE league_id = :league_id AND team_id = :tid",
                tid=team_id,
            )
            names: list[str] = []
            for p in getattr(team, "roster", []) or []:
                espn_id = getattr(p, "playerId", None)
                player_id = id_map.get(espn_id)
                if not player_id:
                    continue
                slot = SLOT_NAMES.get(getattr(p, "lineupSlot", None), getattr(p, "lineupSlot", "BE"))
                if isinstance(slot, str) is False:
                    slot = str(slot)
                ctx.execute(
                    "INSERT INTO league_rosters(league_id, team_id, player_id, slot, updated_at) "
                    "VALUES(:league_id, :tid, :pid, :slot, :ts) "
                    "ON CONFLICT(league_id, team_id, player_id) DO UPDATE SET "
                    "slot = excluded.slot, updated_at = excluded.updated_at",
                    tid=team_id, pid=player_id, slot=slot, ts=now,
                )
                self._store_projection(ctx, p, player_id, league)
                names.append(f"{player_id}:{slot}")
                total += 1

            fingerprint[str(team_id)] = sorted(names)
            standings[str(team_id)] = [
                getattr(team, "wins", 0), getattr(team, "losses", 0),
                round(float(getattr(team, "points_for", 0) or 0), 2),
            ]

        return {"count": total, "fingerprint": fingerprint, "standings": standings}

    def _faab_left(self, league: Any, team: Any) -> int | None:
        budget = getattr(getattr(league, "settings", None), "faab_budget", None)
        spent = getattr(team, "acquisition_budget_spent", None)
        if budget is None or spent is None:
            return None
        return int(budget) - int(spent)

    def _owner_name(self, team: Any) -> str | None:
        owners = getattr(team, "owners", None) or []
        if not owners:
            return getattr(team, "owner", None)
        first = owners[0]
        if isinstance(first, dict):
            name = " ".join(
                filter(None, [first.get("firstName"), first.get("lastName")])
            ).strip()
            return name or first.get("displayName")
        return str(first)

    def _sync_free_agents(self, ctx: LeagueContext, league: Any, now: str) -> tuple[int, list[str]]:
        """Who is actually claimable in THIS league.

        This is the table that makes the same NFL event produce a waiver claim
        in one league and a lineup change in the other.
        """
        try:
            _throttle()
            free_agents = league.free_agents(size=250)
        except Exception as exc:  # noqa: BLE001
            log.warning("free agent fetch failed: %s", exc)
            return 0, []

        entries = [
            (getattr(p, "playerId", None), getattr(p, "name", ""),
             getattr(p, "position", None), normalize_team(getattr(p, "proTeam", None)))
            for p in free_agents
            if getattr(p, "playerId", None) is not None
        ]
        id_map = self.registry.bulk_resolve_espn(entries)

        ctx.execute("DELETE FROM league_free_agents WHERE league_id = :league_id")
        fingerprint: list[str] = []
        count = 0
        for p in free_agents:
            player_id = id_map.get(getattr(p, "playerId", None))
            if not player_id:
                continue
            ctx.execute(
                "INSERT INTO league_free_agents(league_id, player_id, pct_owned, updated_at) "
                "VALUES(:league_id, :pid, :owned, :ts) "
                "ON CONFLICT(league_id, player_id) DO UPDATE SET "
                "pct_owned = excluded.pct_owned, updated_at = excluded.updated_at",
                pid=player_id,
                owned=float(getattr(p, "percent_owned", 0) or 0),
                ts=now,
            )
            self._store_projection(ctx, p, player_id, league)
            fingerprint.append(player_id)
            count += 1
        return count, sorted(fingerprint)

    def _store_projection(
        self, ctx: LeagueContext, espn_player: Any, player_id: str, league: Any
    ) -> None:
        """Capture ESPN's projection under THIS league's scoring settings.

        ESPN computes projected points against the league's own scoring rules,
        so the same player legitimately carries different numbers in each league
        -- which is why this table is league-scoped.
        """
        week = getattr(league, "current_week", None)
        if not week:
            return
        projected = getattr(espn_player, "projected_points", None)
        season_avg = getattr(espn_player, "projected_avg_points", None)
        if projected is None and season_avg is None:
            return  # no projection is better than a fabricated one
        try:
            ctx.set_projection(
                player_id, int(week),
                float(projected) if projected is not None else None,
                float(season_avg) if season_avg is not None else None,
                _safe_float(getattr(espn_player, "points", None)),
            )
        except (TypeError, ValueError):
            return

    def _sync_matchups(
        self, ctx: LeagueContext, league: Any, teams: list[Any], now: str
    ) -> dict[str, Any]:
        fingerprint: dict[str, Any] = {}
        settings = getattr(league, "settings", None)
        total_weeks = getattr(settings, "reg_season_count", 14) or 14
        current = getattr(league, "current_week", 1) or 1

        for team in teams:
            schedule = getattr(team, "schedule", []) or []
            scores = getattr(team, "scores", []) or []
            for idx, opponent in enumerate(schedule, start=1):
                if idx > total_weeks + 4:
                    break
                opp_id = getattr(opponent, "team_id", None)
                score = scores[idx - 1] if idx - 1 < len(scores) else None
                completed = 1 if (idx < current and score) else 0
                ctx.execute(
                    """
                    INSERT INTO league_matchups(league_id, week, team_id, opponent_id,
                                                team_score, opp_score, is_playoff,
                                                completed, updated_at)
                    VALUES(:league_id, :week, :tid, :oid, :ts_, NULL, :playoff, :done, :ts)
                    ON CONFLICT(league_id, week, team_id) DO UPDATE SET
                        opponent_id = excluded.opponent_id,
                        team_score = excluded.team_score,
                        is_playoff = excluded.is_playoff,
                        completed = excluded.completed,
                        updated_at = excluded.updated_at
                    """,
                    week=idx, tid=team.team_id, oid=opp_id,
                    ts_=float(score) if score else None,
                    playoff=1 if idx > total_weeks else 0,
                    done=completed, ts=now,
                )
                fingerprint[f"{team.team_id}:{idx}"] = [opp_id, completed]
        return fingerprint

    def _sync_transactions(self, ctx: LeagueContext, league: Any, now: str) -> list[str] | None:
        """Recent adds/drops/trades -- the raw material for opponent profiling."""
        try:
            _throttle()
            activity = league.recent_activity(size=50)
        except Exception as exc:  # noqa: BLE001 - not all league types support this
            log.debug("recent_activity unavailable: %s", exc)
            return None

        fingerprint: list[str] = []
        for item in activity or []:
            date = getattr(item, "date", None)
            for action in getattr(item, "actions", []) or []:
                team, kind, player = (list(action) + [None, None, None])[:3]
                bid = action[3] if len(action) > 3 else None
                espn_id = getattr(player, "playerId", None)
                player_id = None
                if espn_id is not None:
                    hit = self.registry.by_source_id("espn", espn_id)
                    player_id = hit.player_id if hit else None
                txn_id = f"{date}_{getattr(team, 'team_id', 0)}_{espn_id}_{kind}"
                ctx.execute(
                    """
                    INSERT INTO league_transactions(league_id, txn_id, week, type,
                                                    team_id, player_id, bid_amount,
                                                    occurred_at, raw_json)
                    VALUES(:league_id, :txn, :week, :type, :tid, :pid, :bid, :at, :raw)
                    ON CONFLICT(league_id, txn_id) DO NOTHING
                    """,
                    txn=txn_id,
                    week=getattr(league, "current_week", None),
                    type=str(kind),
                    tid=getattr(team, "team_id", None),
                    pid=player_id,
                    bid=int(bid) if isinstance(bid, (int, float)) and bid else None,
                    at=str(date),
                    raw=json.dumps({"kind": str(kind), "player": getattr(player, "name", None)}),
                )
                fingerprint.append(txn_id)
        return sorted(fingerprint)

    # -- settings extraction ----------------------------------------------

    def _extract_scoring(self, settings: Any) -> dict[str, Any]:
        if settings is None:
            return {}
        raw = getattr(settings, "scoring_format", None)
        if isinstance(raw, list):
            return {
                str(item.get("abbr") or item.get("label")): item.get("points")
                for item in raw
                if isinstance(item, dict)
            }
        return {
            "type": getattr(settings, "scoring_type", None),
            "ppr": getattr(settings, "ppr", None),
        }

    def _extract_roster_slots(self, settings: Any) -> dict[str, int]:
        if settings is None:
            return {}
        positions = getattr(settings, "position_slot_counts", None)
        if isinstance(positions, dict):
            return {str(k): int(v) for k, v in positions.items() if v}
        return {}

    def _extract_waiver(self, settings: Any) -> tuple[str | None, int | None]:
        if settings is None:
            return None, None
        budget = getattr(settings, "faab_budget", None)
        if budget:
            return "faab", int(budget)
        return getattr(settings, "waiver_type", None), None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
