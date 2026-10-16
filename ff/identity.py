"""Cross-source player identity.

Unglamorous and completely load-bearing. ESPN calls him "Marquise Brown",
Sleeper has "Hollywood Brown", nflverse keys off a GSIS id, and the news wire
just says "Brown". If those don't collapse to one player, the injury never
reaches the roster check and the agent silently does nothing.

The crosswalk is close to free: Sleeper's player universe already carries
`espn_id` and `gsis_id` for most players, so ingesting it once gives us the
mapping rather than making us infer it. Name matching is the fallback, and
anything matched by name is stamped with confidence < 1.0 so downstream code
can refuse to act on a shaky identification.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from ff.db.store import Store, utcnow
from ff.logging_setup import get_logger

log = get_logger(__name__)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Players whose common name differs from their listed name badly enough that
# normalization alone won't bridge it.
_ALIASES = {
    "hollywood brown": "marquise brown",
    "dj moore": "d j moore",
    "aj brown": "a j brown",
    "cj stroud": "c j stroud",
    "kenneth walker": "kenneth walker iii",
    "michael pittman": "michael pittman jr",
    "brian robinson": "brian robinson jr",
    "travis etienne": "travis etienne jr",
    "marvin harrison": "marvin harrison jr",
    "chig okonkwo": "chigoziem okonkwo",
    "gabe davis": "gabriel davis",
    "josh palmer": "joshua palmer",
    "cam ward": "cameron ward",
}

# Team abbreviation drift between sources.
TEAM_ALIASES = {
    "JAX": "JAC", "WSH": "WAS", "LAR": "LA", "STL": "LA", "SD": "LAC",
    "OAK": "LV", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
}


def normalize_team(team: str | None) -> str | None:
    if not team:
        return None
    t = team.strip().upper()
    return TEAM_ALIASES.get(t, t)


def normalize_name(name: str) -> str:
    """Fold a display name to a comparable key.

    Strips accents, punctuation, and generational suffixes, and expands the
    known alias list. "D.J. Moore" and "DJ Moore" must land in the same place.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    text = _WS.sub(" ", text).strip()
    text = _ALIASES.get(text, text)
    parts = [p for p in text.split(" ") if p]
    while len(parts) > 2 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


@dataclass(frozen=True)
class ResolvedPlayer:
    player_id: str
    full_name: str
    position: str | None
    nfl_team: str | None
    confidence: float

    @property
    def certain(self) -> bool:
        return self.confidence >= 1.0


class PlayerRegistry:
    """Canonical player identities and the source-id crosswalk."""

    def __init__(self, store: Store):
        self.store = store

    # -- ingestion ---------------------------------------------------------

    def upsert_player(
        self,
        player_id: str,
        full_name: str,
        *,
        position: str | None = None,
        nfl_team: str | None = None,
        status: str | None = None,
        injury_status: str | None = None,
        age: int | None = None,
    ) -> str:
        self.store.execute(
            """
            INSERT INTO nfl_players(player_id, full_name, normalized, position,
                                    nfl_team, status, injury_status, age, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(player_id) DO UPDATE SET
                full_name     = excluded.full_name,
                normalized    = excluded.normalized,
                position      = COALESCE(excluded.position, nfl_players.position),
                nfl_team      = COALESCE(excluded.nfl_team, nfl_players.nfl_team),
                status        = COALESCE(excluded.status, nfl_players.status),
                injury_status = excluded.injury_status,
                age           = COALESCE(excluded.age, nfl_players.age),
                updated_at    = excluded.updated_at
            """,
            (
                player_id,
                full_name,
                normalize_name(full_name),
                position,
                normalize_team(nfl_team),
                status,
                injury_status,
                age,
                utcnow(),
            ),
        )
        return player_id

    def link(self, source: str, source_id: str | int, player_id: str, confidence: float = 1.0) -> None:
        if source_id in (None, "", 0):
            return
        self.store.execute(
            "INSERT INTO player_ids(source, source_id, player_id, confidence) VALUES(?,?,?,?) "
            "ON CONFLICT(source, source_id) DO UPDATE SET "
            "player_id = excluded.player_id, confidence = excluded.confidence",
            (source, str(source_id), player_id, confidence),
        )

    def ingest_sleeper_universe(self, players: dict[str, dict[str, Any]]) -> int:
        """Load Sleeper's player dump -- our source of truth for identity.

        Sleeper carries espn_id and gsis_id inline, which is precisely the
        crosswalk we'd otherwise have to guess at with fuzzy name matching.
        """
        count = 0
        for sleeper_id, rec in players.items():
            position = (rec.get("position") or "").upper() or None
            # Only fantasy-relevant positions; the dump includes every OL and LB.
            if position not in {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}:
                continue
            name = rec.get("full_name") or " ".join(
                filter(None, [rec.get("first_name"), rec.get("last_name")])
            )
            if not name:
                continue
            player_id = f"p_{sleeper_id}"
            self.upsert_player(
                player_id,
                name,
                position="DST" if position == "DEF" else position,
                nfl_team=rec.get("team"),
                status=rec.get("status"),
                injury_status=rec.get("injury_status"),
                age=rec.get("age"),
            )
            self.link("sleeper", sleeper_id, player_id)
            if rec.get("espn_id"):
                self.link("espn", rec["espn_id"], player_id)
            if rec.get("gsis_id"):
                self.link("nflverse", rec["gsis_id"], player_id)
            count += 1
        self.store.commit()
        log.info("identity: ingested %d fantasy-relevant players from Sleeper", count)
        return count

    # -- resolution --------------------------------------------------------

    def by_source_id(self, source: str, source_id: str | int) -> ResolvedPlayer | None:
        row = self.store.one(
            "SELECT p.*, i.confidence FROM player_ids i "
            "JOIN nfl_players p ON p.player_id = i.player_id "
            "WHERE i.source = ? AND i.source_id = ?",
            (source, str(source_id)),
        )
        if not row:
            return None
        return ResolvedPlayer(
            row["player_id"], row["full_name"], row["position"],
            row["nfl_team"], row["confidence"],
        )

    def by_name(
        self, name: str, position: str | None = None, team: str | None = None
    ) -> ResolvedPlayer | None:
        """Name-based fallback. Deliberately conservative.

        An ambiguous name resolves to nothing rather than to a guess -- acting
        on the wrong player is far worse than not acting.
        """
        norm = normalize_name(name)
        if not norm:
            return None
        rows = self.store.query(
            "SELECT * FROM nfl_players WHERE normalized = ?", (norm,)
        )
        if not rows:
            return None

        candidates = rows
        if position:
            pos = position.upper()
            narrowed = [r for r in candidates if (r["position"] or "").upper() == pos]
            if narrowed:
                candidates = narrowed
        if team:
            tm = normalize_team(team)
            narrowed = [r for r in candidates if (r["nfl_team"] or "") == tm]
            if narrowed:
                candidates = narrowed

        if len(candidates) != 1:
            log.debug("identity: %r ambiguous (%d candidates), refusing to guess",
                      name, len(candidates))
            return None

        row = candidates[0]
        # Exact name plus a corroborating attribute is near-certain; name alone
        # is good but not something we'd bet a waiver claim on unexamined.
        confidence = 0.95 if (position or team) else 0.80
        return ResolvedPlayer(
            row["player_id"], row["full_name"], row["position"], row["nfl_team"], confidence
        )

    def resolve(
        self,
        *,
        source: str | None = None,
        source_id: str | int | None = None,
        name: str | None = None,
        position: str | None = None,
        team: str | None = None,
    ) -> ResolvedPlayer | None:
        """Best available identification, id first then name."""
        if source and source_id:
            hit = self.by_source_id(source, source_id)
            if hit:
                return hit
        if name:
            hit = self.by_name(name, position, team)
            if hit and source and source_id:
                # Learn the mapping so the next lookup is exact and free.
                self.link(source, source_id, hit.player_id, confidence=hit.confidence)
                self.store.commit()
            return hit
        return None

    def get(self, player_id: str) -> ResolvedPlayer | None:
        row = self.store.one("SELECT * FROM nfl_players WHERE player_id = ?", (player_id,))
        if not row:
            return None
        return ResolvedPlayer(
            row["player_id"], row["full_name"], row["position"], row["nfl_team"], 1.0
        )

    def name_of(self, player_id: str | None) -> str:
        if not player_id:
            return "Unknown player"
        row = self.store.one(
            "SELECT full_name FROM nfl_players WHERE player_id = ?", (player_id,)
        )
        return row["full_name"] if row else player_id

    def search(self, text: str, limit: int = 10) -> list[ResolvedPlayer]:
        rows = self.store.query(
            "SELECT * FROM nfl_players WHERE normalized LIKE ? ORDER BY full_name LIMIT ?",
            (f"%{normalize_name(text)}%", limit),
        )
        return [
            ResolvedPlayer(r["player_id"], r["full_name"], r["position"], r["nfl_team"], 1.0)
            for r in rows
        ]

    def bulk_resolve_espn(self, entries: Iterable[tuple[Any, str, str | None, str | None]]) -> dict[Any, str]:
        """Resolve many ESPN players at once -> {espn_id: player_id}.

        Unresolvable players are registered under a synthetic id so a roster is
        never silently short a slot; they simply carry no cross-source data.
        """
        out: dict[Any, str] = {}
        for espn_id, name, position, team in entries:
            hit = self.resolve(
                source="espn", source_id=espn_id, name=name, position=position, team=team
            )
            if hit:
                out[espn_id] = hit.player_id
                continue
            synthetic = f"espn_{espn_id}"
            self.upsert_player(synthetic, name, position=position, nfl_team=team)
            self.link("espn", espn_id, synthetic, confidence=1.0)
            out[espn_id] = synthetic
        self.store.commit()
        return out
