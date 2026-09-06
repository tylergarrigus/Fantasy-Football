"""Configuration and secret handling.

Secrets are read from the environment only -- never from a committed file, never
hardcoded. Every secret value that passes through here is registered with the
log redactor, so a stray ``log.info(cookies)`` prints ``***REDACTED***`` instead
of your ESPN session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "state.db"

# Populated by `_secret()`. `ff.logging_setup` reads this to build its filter.
_REGISTERED_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mark a string as sensitive so logging scrubs it wherever it appears."""
    if value and len(value) >= 6:
        _REGISTERED_SECRETS.add(value)


def registered_secrets() -> frozenset[str]:
    return frozenset(_REGISTERED_SECRETS)


def _secret(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    register_secret(value)
    return value


def _plain(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _plain(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _plain(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _plain(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class ESPNCredentials:
    """ESPN's only supported auth for private leagues: two browser cookies.

    There is no official API, no OAuth, and no API key. Username/password login
    is blocked by reCAPTCHA and cannot be automated. These cookies authenticate
    your entire ESPN account, so they are treated as top-tier secrets.

    Both empty is a valid, expected state: public leagues need no auth at all.
    """

    espn_s2: str = ""
    swid: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    def as_cookies(self) -> dict[str, str] | None:
        if not self.configured:
            return None
        return {"espn_s2": self.espn_s2, "SWID": self.swid}

    def __repr__(self) -> str:  # never leak into a traceback
        return f"ESPNCredentials(configured={self.configured})"


@dataclass(frozen=True)
class LeagueConfig:
    """Identity of one league. Deliberately minimal -- everything else is fetched."""

    key: str  # stable internal handle: "L1" / "L2"
    league_id: int
    season: int
    name: str
    team_id: int | None = None  # None => auto-detect from SWID

    def __post_init__(self) -> None:
        if self.league_id <= 0:
            raise ConfigError(f"{self.key}: league_id must be a positive integer")


@dataclass(frozen=True)
class Settings:
    leagues: tuple[LeagueConfig, ...]
    espn: ESPNCredentials
    anthropic_api_key: str = ""
    notify_url: str = ""
    fantasypros_api_key: str = ""
    db_path: Path = DEFAULT_DB_PATH
    cache_dir: Path = REPO_ROOT / ".cache"
    min_championship_delta: float = 1.0
    dry_run: bool = False
    # Deterministic seed for the Monte Carlo simulator. Fixing this makes runs
    # reproducible, which matters when we later audit why an alert was sent.
    sim_seed: int = 20260817
    sim_iterations: int = 10000
    _warnings: tuple[str, ...] = field(default_factory=tuple)

    def league(self, key: str) -> LeagueConfig:
        for lg in self.leagues:
            if lg.key == key:
                return lg
        raise ConfigError(f"no league configured with key {key!r}")

    @property
    def can_reason(self) -> bool:
        """Stage-2 (Claude) analysis is available."""
        return bool(self.anthropic_api_key)

    @property
    def can_notify(self) -> bool:
        return bool(self.notify_url)

    def warnings(self) -> tuple[str, ...]:
        return self._warnings


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from the environment.

    Missing optional pieces degrade to a warning rather than an exception: the
    agent should still be able to run analysis and print to a terminal when it
    has no notification channel or no API key configured.
    """
    if env is not None:
        os.environ.update(env)

    season = _int("FF_SEASON", 2026)
    warnings: list[str] = []
    leagues: list[LeagueConfig] = []

    for idx, key in ((1, "L1"), (2, "L2")):
        raw_id = _plain(f"FF_LEAGUE_{idx}_ID")
        if not raw_id:
            continue
        try:
            league_id = int(raw_id)
        except ValueError as exc:
            raise ConfigError(
                f"FF_LEAGUE_{idx}_ID must be the numeric leagueId from your "
                f"ESPN URL, got {raw_id!r}"
            ) from exc
        raw_team = _plain(f"FF_LEAGUE_{idx}_TEAM_ID")
        leagues.append(
            LeagueConfig(
                key=key,
                league_id=league_id,
                season=season,
                name=_plain(f"FF_LEAGUE_{idx}_NAME") or f"League {idx}",
                team_id=int(raw_team) if raw_team else None,
            )
        )

    if not leagues:
        warnings.append(
            "No leagues configured (FF_LEAGUE_1_ID / FF_LEAGUE_2_ID unset). "
            "Analysis commands will have nothing to analyze."
        )

    league_ids = [lg.league_id for lg in leagues]
    if len(set(league_ids)) != len(league_ids):
        raise ConfigError(
            "FF_LEAGUE_1_ID and FF_LEAGUE_2_ID are the same league. The whole "
            "point of this agent is that the two leagues are evaluated "
            "independently -- point them at different leagues."
        )

    espn = ESPNCredentials(espn_s2=_secret("ESPN_S2"), swid=_secret("ESPN_SWID"))
    anthropic_key = _secret("ANTHROPIC_API_KEY")
    notify_url = _secret("FF_NOTIFY_URL")
    fp_key = _secret("FANTASYPROS_API_KEY")

    if not anthropic_key:
        warnings.append(
            "ANTHROPIC_API_KEY unset -- stage-2 reasoning disabled. Deterministic "
            "engines still run; recommendations will lack written rationale."
        )
    if not notify_url:
        warnings.append(
            "FF_NOTIFY_URL unset -- nothing will reach your phone. Alerts will "
            "be recorded to the DB and printed only."
        )

    db_path = Path(_plain("FF_DB_PATH") or DEFAULT_DB_PATH)

    return Settings(
        leagues=tuple(leagues),
        espn=espn,
        anthropic_api_key=anthropic_key,
        notify_url=notify_url,
        fantasypros_api_key=fp_key,
        db_path=db_path,
        cache_dir=Path(_plain("FF_CACHE_DIR") or (REPO_ROOT / ".cache")),
        min_championship_delta=_float("FF_MIN_CHAMPIONSHIP_DELTA", 1.0),
        dry_run=_bool("FF_DRY_RUN", False),
        sim_seed=_int("FF_SIM_SEED", 20260817),
        sim_iterations=_int("FF_SIM_ITERATIONS", 10000),
        _warnings=tuple(warnings),
    )
