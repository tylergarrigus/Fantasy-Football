"""Game-environment weather via Open-Meteo (free, no key).

Weather only matters at the margins, and only outdoors -- so we skip domes
entirely rather than burn calls on them. The signal that actually moves a
lineup decision is wind: sustained wind above ~15mph measurably suppresses
passing and kicking. Cold alone rarely changes a start/sit; a 25mph crosswind
in Buffalo does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ff.db.store import Store, utcnow
from ff.identity import normalize_team
from ff.logging_setup import get_logger
from ff.sources.base import HttpClient, SourceError

log = get_logger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Stadium coordinates by team. Domes/retractables are flagged so we can skip
# the call entirely rather than pretend the forecast is meaningful.
STADIUMS: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.5277, -112.2626, True),  "ATL": (33.7554, -84.4008, True),
    "BAL": (39.2780, -76.6227, False),  "BUF": (42.7738, -78.7870, False),
    "CAR": (35.2258, -80.8528, False),  "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0954, -84.5160, False),  "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True),   "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True),   "GB":  (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True),   "IND": (39.7601, -86.1639, True),
    "JAC": (30.3239, -81.6373, False),  "KC":  (39.0489, -94.4839, False),
    "LA":  (33.9535, -118.3392, True),  "LAC": (33.9535, -118.3392, True),
    "LV":  (36.0909, -115.1833, True),  "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9736, -93.2578, True),   "NE":  (42.0909, -71.2643, False),
    "NO":  (29.9511, -90.0812, True),   "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False),  "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False),  "SEA": (47.5952, -122.3316, False),
    "SF":  (37.4030, -121.9698, False), "TB":  (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False),  "WAS": (38.9076, -76.8645, False),
}

# Above this, passing and kicking degrade enough to matter for a start/sit.
WIND_CONCERN_MPH = 15.0
WIND_SEVERE_MPH = 22.0


class WeatherSource:
    def __init__(self, http: HttpClient, store: Store):
        self.http = http
        self.store = store

    def for_game(
        self, home_team: str, kickoff_iso: str | None, *, indoor: bool | None = None
    ) -> dict[str, Any]:
        team = normalize_team(home_team) or ""
        coords = STADIUMS.get(team)
        if coords is None:
            return {"status": "DATA UNAVAILABLE", "reason": f"no stadium coords for {home_team!r}"}

        lat, lon, is_dome = coords
        if indoor or is_dome:
            return {"status": "ok", "indoor": True, "impact": "none",
                    "note": "Indoor venue -- weather is not a factor."}

        try:
            resp = self.http.get_json(
                FORECAST_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
                    "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                    "forecast_days": 7,
                },
                ttl=3600,
            )
        except SourceError as exc:
            return {"status": "DATA UNAVAILABLE", "reason": f"forecast fetch failed: {exc}"}

        hourly = (resp.data or {}).get("hourly") or {}
        idx = self._hour_index(hourly.get("time") or [], kickoff_iso)
        if idx is None:
            return {"status": "DATA UNAVAILABLE",
                    "reason": "kickoff time outside the forecast horizon"}

        temp = _at(hourly.get("temperature_2m"), idx)
        wind = _at(hourly.get("wind_speed_10m"), idx)
        precip = _at(hourly.get("precipitation_probability"), idx)

        return {
            "status": "ok",
            "indoor": False,
            "temp_f": temp,
            "wind_mph": wind,
            "precip_pct": precip,
            "impact": self._impact(wind, precip, temp),
            "note": self._describe(temp, wind, precip),
            "stale": resp.from_cache and resp.is_stale(7200),
        }

    def _hour_index(self, times: list[str], kickoff_iso: str | None) -> int | None:
        if not times or not kickoff_iso:
            return None
        try:
            kickoff = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        best, best_delta = None, None
        for i, stamp in enumerate(times):
            try:
                when = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            delta = abs((when - kickoff).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = i, delta
        # More than 3h from any forecast hour means we're guessing.
        return best if (best_delta is not None and best_delta <= 10800) else None

    def _impact(self, wind: float | None, precip: float | None, temp: float | None) -> str:
        if wind is not None and wind >= WIND_SEVERE_MPH:
            return "high"
        if wind is not None and wind >= WIND_CONCERN_MPH:
            return "moderate"
        if precip is not None and precip >= 70:
            return "moderate"
        if temp is not None and temp <= 15:
            return "low"
        return "none"

    def _describe(self, temp: float | None, wind: float | None, precip: float | None) -> str:
        bits = []
        if temp is not None:
            bits.append(f"{temp:.0f}F")
        if wind is not None:
            bits.append(f"{wind:.0f}mph wind")
        if precip is not None and precip >= 30:
            bits.append(f"{precip:.0f}% precip")
        if not bits:
            return "No meaningful weather signal."
        text = ", ".join(bits)
        if wind is not None and wind >= WIND_SEVERE_MPH:
            return f"{text} -- enough wind to suppress passing and kicking."
        if wind is not None and wind >= WIND_CONCERN_MPH:
            return f"{text} -- wind worth noting for passing games and kickers."
        return text

    def store_game(self, game: dict[str, Any], forecast: dict[str, Any], season: int, week: int) -> None:
        if forecast.get("status") != "ok":
            return
        key = f"{season}-W{week:02d}-{game.get('away')}@{game.get('home')}"
        self.store.execute(
            """
            INSERT INTO game_environment(game_key, season, week, home_team, away_team,
                                         kickoff_utc, indoor, temp_f, wind_mph,
                                         precip_pct, implied_total, spread, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(game_key) DO UPDATE SET
                temp_f = excluded.temp_f, wind_mph = excluded.wind_mph,
                precip_pct = excluded.precip_pct, implied_total = excluded.implied_total,
                spread = excluded.spread, updated_at = excluded.updated_at
            """,
            (
                key, season, week, game.get("home"), game.get("away"),
                game.get("kickoff"), 1 if forecast.get("indoor") else 0,
                forecast.get("temp_f"), forecast.get("wind_mph"), forecast.get("precip_pct"),
                game.get("over_under"), game.get("spread"), utcnow(),
            ),
        )
        self.store.commit()


def _at(seq: list[Any] | None, idx: int) -> float | None:
    if not seq or idx >= len(seq):
        return None
    try:
        return float(seq[idx])
    except (TypeError, ValueError):
        return None
