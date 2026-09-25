"""nflverse usage data -- snap share, target share, carry share, red-zone work.

This is the difference between "he scored 12 last week" and "he played 78% of
snaps on a 24% target share". The first is noise; the second predicts. Role
changes show up here days before they show up in fantasy points, which is
exactly the kind of thing worth interrupting someone about.

Optional dependency (`pip install '.[usage]'` -- it pulls polars/pyarrow). If
nflreadpy is absent or the fetch fails, every method returns empty and the
engines mark those inputs DATA UNAVAILABLE rather than inventing them.
"""

from __future__ import annotations

from typing import Any

from ff.db.store import Store, utcnow
from ff.identity import PlayerRegistry, normalize_team
from ff.logging_setup import get_logger

log = get_logger(__name__)


class NFLVerseSource:
    def __init__(self, store: Store, registry: PlayerRegistry):
        self.store = store
        self.registry = registry
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                import nflreadpy  # noqa: F401

                self._available = True
            except ImportError:
                log.info(
                    "nflreadpy not installed -- usage/snap-share signals will be "
                    "reported as DATA UNAVAILABLE. Install with: pip install '.[usage]'"
                )
                self._available = False
        return self._available

    def sync_weekly_usage(self, season: int, week: int | None = None) -> int:
        """Pull weekly player stats and store per-player usage rates."""
        if not self.available:
            return 0
        try:
            import nflreadpy as nfl

            frame = nfl.load_player_stats(seasons=[season])
        except Exception as exc:  # noqa: BLE001 - upstream data can be absent early season
            log.warning("nflverse weekly stats unavailable: %s", exc)
            return 0

        try:
            rows = frame.to_dicts()  # polars
        except AttributeError:
            rows = frame.to_dict("records")  # pandas fallback

        now = utcnow()
        stored = 0
        for row in rows:
            if week is not None and row.get("week") != week:
                continue
            gsis = row.get("player_id") or row.get("gsis_id")
            if not gsis:
                continue
            hit = self.registry.by_source_id("nflverse", gsis)
            if not hit:
                hit = self.registry.resolve(
                    source="nflverse",
                    source_id=gsis,
                    name=row.get("player_display_name") or row.get("player_name"),
                    position=row.get("position"),
                    team=normalize_team(row.get("recent_team") or row.get("team")),
                )
            if not hit:
                continue

            targets = _num(row.get("targets"))
            team_targets = _num(row.get("team_targets")) or 0
            carries = _num(row.get("carries"))
            team_carries = _num(row.get("team_carries")) or 0

            self.store.execute(
                """
                INSERT INTO player_usage(player_id, season, week, snap_pct, route_pct,
                                         target_share, carry_share, rz_touches,
                                         gl_carries, fantasy_pts, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(player_id, season, week) DO UPDATE SET
                    snap_pct = COALESCE(excluded.snap_pct, player_usage.snap_pct),
                    target_share = COALESCE(excluded.target_share, player_usage.target_share),
                    carry_share = COALESCE(excluded.carry_share, player_usage.carry_share),
                    fantasy_pts = COALESCE(excluded.fantasy_pts, player_usage.fantasy_pts),
                    updated_at = excluded.updated_at
                """,
                (
                    hit.player_id, season, int(row.get("week") or 0),
                    _num(row.get("snap_pct")),
                    _num(row.get("route_pct")),
                    (targets / team_targets) if targets and team_targets else _num(row.get("target_share")),
                    (carries / team_carries) if carries and team_carries else _num(row.get("carry_share")),
                    _int(row.get("rz_touches")),
                    _int(row.get("carries_gl")),
                    _num(row.get("fantasy_points_ppr")),
                    now,
                ),
            )
            stored += 1

        self.store.commit()
        log.info("nflverse: stored %d weekly usage rows for %s", stored, season)
        return stored

    def recent_usage(self, player_id: str, weeks: int = 4) -> list[dict[str, Any]]:
        rows = self.store.query(
            "SELECT * FROM player_usage WHERE player_id = ? ORDER BY season DESC, week DESC LIMIT ?",
            (player_id, weeks),
        )
        return [dict(r) for r in rows]

    def usage_trend(self, player_id: str) -> dict[str, Any]:
        """Is his role growing or shrinking? Returns DATA UNAVAILABLE honestly."""
        rows = self.recent_usage(player_id, weeks=4)
        if len(rows) < 2:
            return {"status": "DATA UNAVAILABLE", "reason": "fewer than 2 weeks of usage data"}

        def avg(key: str, subset: list[dict]) -> float | None:
            vals = [r[key] for r in subset if r.get(key) is not None]
            return sum(vals) / len(vals) if vals else None

        recent, prior = rows[:2], rows[2:]
        out: dict[str, Any] = {"status": "ok", "weeks_available": len(rows)}
        for key in ("snap_pct", "target_share", "carry_share"):
            now_v, then_v = avg(key, recent), avg(key, prior or recent)
            if now_v is None:
                out[key] = None
                continue
            out[key] = round(now_v, 3)
            if then_v:
                out[f"{key}_delta"] = round(now_v - then_v, 3)
        return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
