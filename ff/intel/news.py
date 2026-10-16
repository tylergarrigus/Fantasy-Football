"""Global NFL intelligence: what changed, who says so, and can we believe them.

Collected once, shared by both leagues. The league-specific question ("so what
should Tyler do?") is answered downstream -- this layer only establishes what is
actually true about the NFL right now.

Three rules it enforces:

  * Nothing is invented. A field we could not establish is absent or explicitly
    DATA UNAVAILABLE; it is never filled with a plausible guess.
  * Every claim carries its source and a reliability tier, and corroboration
    from two independent sources is tracked explicitly rather than assumed.
  * Everything is timestamped and ages out. A practice report from Wednesday is
    not evidence about Sunday, and stale data says so.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ff.db.store import Store, utcnow
from ff.identity import PlayerRegistry, normalize_team
from ff.logging_setup import get_logger

log = get_logger(__name__)


# Source reliability. Lower is better. This ordering is the spec's, and it
# matters: a beat reporter saying "expected to play" outranks an aggregator,
# and social chatter is an early signal to go verify, never a fact.
class Tier:
    OFFICIAL_TEAM = 1        # team/NFL announcement, official transaction
    OFFICIAL_REPORT = 2      # official injury report, inactives list
    BEAT_REPORTER = 3        # credentialed reporter for that team
    ESTABLISHED_NEWS = 4     # ESPN, NFL Network, AP
    STAT_PROVIDER = 5        # nflverse, Sleeper metadata
    EXPERT_CONSENSUS = 6     # FantasyPros ECR
    SOCIAL_SIGNAL = 7        # trending adds, unattributed chatter


SOURCE_TIERS = {
    "espn_injury_report": Tier.OFFICIAL_REPORT,
    "espn_inactives": Tier.OFFICIAL_REPORT,
    "sleeper_status": Tier.OFFICIAL_REPORT,
    "sleeper_depth_chart": Tier.STAT_PROVIDER,
    "espn_news": Tier.ESTABLISHED_NEWS,
    "espn_transaction": Tier.OFFICIAL_TEAM,
    "nflverse_usage": Tier.STAT_PROVIDER,
    "fantasypros": Tier.EXPERT_CONSENSUS,
    "sleeper_trending": Tier.SOCIAL_SIGNAL,
    "weather": Tier.STAT_PROVIDER,
}

# Injury designations, most to least severe. Ordering lets us say whether a
# status got *worse*, which is the thing that actually triggers action.
STATUS_SEVERITY = {
    "out": 5, "ir": 6, "injured reserve": 6, "pup": 6, "suspended": 6,
    "doubtful": 4, "questionable": 3, "probable": 2, "active": 1, "healthy": 0,
}

# How long a given kind of news stays decision-relevant.
FRESHNESS_HOURS = {
    "injury_status": 96, "practice": 96, "inactive": 8, "role_change": 336,
    "depth_chart": 336, "trade": 720, "coaching": 720, "weather": 12,
    "usage": 336, "news": 72,
}

# Fantasy content-farm headlines. These are advice *about* fantasy football, not
# news about the NFL, and they are pure volume -- letting them through means
# every cycle "detects a change" and nothing ever settles.
_TRIVIAL = re.compile(
    r"(start\s*'?\s*em|sit\s*'?\s*em|waiver wire|fantasy football|fantasy advice|"
    r"\bdfs\b|betting|odds|preview|power rankings|who to start|best bets|"
    r"survivor pool|sleepers and busts|draft guide|rankings)",
    re.I,
)


@dataclass
class NFLEvent:
    """One normalized, attributed, timestamped change in the NFL."""

    kind: str
    headline: str
    source: str
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    player_id: str | None = None
    nfl_team: str | None = None
    body: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    severity: str = "info"           # info | notable | major
    source_url: str | None = None
    published_at: str | None = None
    first_seen_at: str = field(default_factory=utcnow)
    verified: bool = False
    stale: bool = False

    @property
    def source_tier(self) -> int:
        return SOURCE_TIERS.get(self.source, Tier.SOCIAL_SIGNAL)

    @property
    def fingerprint(self) -> str:
        """Identity of the *change*, not of the message.

        Two sources reporting the same status change collapse to one event;
        the same player moving Questionable -> Out later is a new one.
        """
        basis = "|".join(
            [self.kind, self.player_id or self.nfl_team or "", str(self.new_value or ""),
             self.headline.lower()[:80] if not self.player_id else ""]
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:24]

    @property
    def is_actionable_kind(self) -> bool:
        return self.kind in {
            "injury_status", "inactive", "role_change", "depth_chart",
            "practice", "trade", "coaching",
        }

    def to_row(self) -> tuple:
        return (
            self.event_id, self.fingerprint, self.kind, self.player_id, self.nfl_team,
            self.severity, self.headline, self.body, self.old_value, self.new_value,
            self.source, self.source_tier, self.source_url, self.published_at,
            self.first_seen_at, int(self.verified), int(self.stale),
        )


class NewsEngine:
    """Ingest, dedupe, corroborate, age out."""

    def __init__(self, store: Store, registry: PlayerRegistry):
        self.store = store
        self.registry = registry

    # -- ingestion ---------------------------------------------------------

    def record(self, events: Iterable[NFLEvent]) -> list[NFLEvent]:
        """Persist events, dropping ones we have already seen.

        Returns only the genuinely new ones -- that list is what stage 1.5
        filters and what may eventually justify waking the expensive model.
        """
        fresh: list[NFLEvent] = []
        for event in events:
            # News routinely arrives for players our universe has not ingested
            # yet -- a practice-squad call-up, or a name we could not resolve.
            # Keep the headline, drop the dangling reference. Losing the event
            # entirely (or crashing the cycle on a foreign-key error) is worse.
            if event.player_id and not self._player_exists(event.player_id):
                log.debug("event references unknown player %s; storing unlinked",
                          event.player_id)
                event.player_id = None

            existing = self.store.one(
                "SELECT event_id, source, source_tier FROM nfl_events WHERE fingerprint = ?",
                (event.fingerprint,),
            )
            if existing:
                # Same change from a second, independent source => corroborated.
                if existing["source"] != event.source:
                    self.store.execute(
                        "UPDATE nfl_events SET verified = 1, source_tier = MIN(source_tier, ?) "
                        "WHERE fingerprint = ?",
                        (event.source_tier, event.fingerprint),
                    )
                    log.debug("corroborated %s via %s", event.headline[:60], event.source)
                continue

            self.store.execute(
                """
                INSERT INTO nfl_events(event_id, fingerprint, kind, player_id, nfl_team,
                                       severity, headline, body, old_value, new_value,
                                       source, source_tier, source_url, published_at,
                                       first_seen_at, verified, stale)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                event.to_row(),
            )
            fresh.append(event)

        self.store.commit()
        if fresh:
            log.info("news: %d new event(s)", len(fresh))
        return fresh

    def _player_exists(self, player_id: str) -> bool:
        return (
            self.store.one(
                "SELECT 1 AS hit FROM nfl_players WHERE player_id = ?", (player_id,)
            )
            is not None
        )

    # -- builders from each source ----------------------------------------

    def from_injury_diff(
        self, previous: dict[str, dict], current: dict[str, dict]
    ) -> list[NFLEvent]:
        """Diff two Sleeper injury snapshots into status-change events.

        Only *changes* become events. A player who has been Out for three weeks
        is not news and must not generate an alert every cycle.
        """
        events: list[NFLEvent] = []
        for sleeper_id, now in current.items():
            before = previous.get(sleeper_id, {})
            old_status = (before.get("status") or "").strip()
            new_status = (now.get("status") or "").strip()
            if old_status == new_status:
                continue

            hit = self.registry.resolve(
                source="sleeper", source_id=sleeper_id,
                name=now.get("name"), position=now.get("position"), team=now.get("team"),
            )
            got_worse = severity_of(new_status) > severity_of(old_status)
            events.append(
                NFLEvent(
                    kind="injury_status",
                    player_id=hit.player_id if hit else None,
                    nfl_team=normalize_team(now.get("team")),
                    headline=f"{now.get('name', 'Player')} is now {new_status or 'Active'}"
                    + (f" (was {old_status})" if old_status else ""),
                    body=now.get("notes") or None,
                    old_value=old_status or None,
                    new_value=new_status or "Active",
                    severity=self._injury_severity(new_status, got_worse),
                    source="sleeper_status",
                )
            )

        # Players who dropped out of the snapshot entirely have been cleared.
        for sleeper_id, before in previous.items():
            if sleeper_id in current:
                continue
            hit = self.registry.by_source_id("sleeper", sleeper_id)
            events.append(
                NFLEvent(
                    kind="injury_status",
                    player_id=hit.player_id if hit else None,
                    nfl_team=normalize_team(before.get("team")),
                    headline=f"{before.get('name', 'Player')} no longer carries an injury designation",
                    old_value=before.get("status"),
                    new_value="Active",
                    severity="notable",
                    source="sleeper_status",
                )
            )
        return events

    def from_depth_chart_diff(
        self, previous: dict[str, str], current: dict[str, str]
    ) -> list[NFLEvent]:
        """Depth-chart movement -- often the earliest hard signal of a role change."""
        events: list[NFLEvent] = []
        for sleeper_id, now in current.items():
            before = previous.get(sleeper_id)
            if before is None or before == now:
                continue
            try:
                _, _, old_order = before.split(":")
                team, pos, new_order = now.split(":")
            except ValueError:
                continue

            promoted = int(new_order) < int(old_order)
            # Only movement into or out of the top two matters for fantasy.
            if int(new_order) > 3 and int(old_order) > 3:
                continue

            hit = self.registry.by_source_id("sleeper", sleeper_id)
            name = self.registry.name_of(hit.player_id) if hit else "A player"
            events.append(
                NFLEvent(
                    kind="depth_chart",
                    player_id=hit.player_id if hit else None,
                    nfl_team=normalize_team(team),
                    headline=(
                        f"{name} {'moved up to' if promoted else 'moved down to'} "
                        f"{pos}{new_order} on the {team} depth chart"
                    ),
                    old_value=f"{pos}{old_order}",
                    new_value=f"{pos}{new_order}",
                    severity="major" if (promoted and int(new_order) == 1) else "notable",
                    source="sleeper_depth_chart",
                )
            )
        return events

    def from_espn_articles(self, articles: list[dict[str, Any]]) -> list[NFLEvent]:
        """Headlines, minus the fantasy-advice content farm."""
        events: list[NFLEvent] = []
        for art in articles:
            headline = (art.get("headline") or "").strip()
            if not headline or _TRIVIAL.search(headline):
                continue

            player_id = None
            for athlete in art.get("athletes") or []:
                hit = self.registry.resolve(
                    source="espn", source_id=athlete.get("id"), name=athlete.get("name")
                )
                if hit:
                    player_id = hit.player_id
                    break

            events.append(
                NFLEvent(
                    kind=self._classify(headline),
                    player_id=player_id,
                    headline=headline,
                    body=(art.get("body") or "")[:600] or None,
                    source="espn_news",
                    source_url=art.get("url"),
                    published_at=art.get("published"),
                    severity=self._headline_severity(headline),
                )
            )
        return events

    def from_trending(self, trending: list[dict], threshold: int = 5000) -> list[NFLEvent]:
        """Sudden league-wide add spikes -- an early *signal*, explicitly not a fact."""
        events: list[NFLEvent] = []
        for item in trending:
            count = item.get("count") or 0
            if count < threshold:
                continue
            hit = self.registry.by_source_id("sleeper", item.get("player_id"))
            if not hit:
                continue
            events.append(
                NFLEvent(
                    kind="news",
                    player_id=hit.player_id,
                    headline=f"{hit.full_name} added in {count:,} leagues in the last 24h",
                    body="Market signal only -- unconfirmed by any reporting source.",
                    new_value=str(count),
                    severity="notable",
                    source="sleeper_trending",
                )
            )
        return events

    # -- classification ----------------------------------------------------

    def _classify(self, headline: str) -> str:
        text = headline.lower()
        if any(w in text for w in ("injur", "questionable", "doubtful", "ruled out", "hamstring", "ankle", "concussion")):
            return "injury_status"
        if any(w in text for w in ("inactive", "will not play", "won't play")):
            return "inactive"
        if any(w in text for w in ("practice", "limited", "dnp", "did not participate")):
            return "practice"
        if any(w in text for w in ("traded", "trade", "acquires", "acquired")):
            return "trade"
        if any(w in text for w in ("fired", "hired", "coordinator", "head coach", "benched", "starting job", "named starter")):
            return "coaching"
        if any(w in text for w in ("workload", "snap", "touches", "carries", "role")):
            return "role_change"
        return "news"

    def _injury_severity(self, status: str, got_worse: bool) -> str:
        level = severity_of(status)
        if level >= 5:
            return "major"
        if level >= 3 and got_worse:
            return "notable"
        return "info"

    def _headline_severity(self, headline: str) -> str:
        text = headline.lower()
        if any(w in text for w in ("ruled out", "torn", "acl", "season-ending", "carted", "surgery", "ir")):
            return "major"
        if any(w in text for w in ("questionable", "doubtful", "limited", "benched", "named starter")):
            return "notable"
        return "info"

    # -- retrieval and hygiene --------------------------------------------

    def recent(self, hours: int = 48, kinds: Iterable[str] | None = None) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        sql = "SELECT * FROM nfl_events WHERE first_seen_at >= ?"
        params: list[Any] = [cutoff]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY source_tier ASC, first_seen_at DESC"
        return [dict(r) for r in self.store.query(sql, params)]

    def for_player(self, player_id: str, hours: int = 168) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return [
            dict(r)
            for r in self.store.query(
                "SELECT * FROM nfl_events WHERE player_id = ? AND first_seen_at >= ? "
                "ORDER BY first_seen_at DESC",
                (player_id, cutoff),
            )
        ]

    def mark_stale(self) -> int:
        """Age out events past their decision-relevant window.

        A Wednesday practice report is not evidence about Sunday. Marking rather
        than deleting keeps the audit trail intact for calibration later.
        """
        now = datetime.now(timezone.utc)
        marked = 0
        for kind, hours in FRESHNESS_HOURS.items():
            cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
            cur = self.store.execute(
                "UPDATE nfl_events SET stale = 1 WHERE kind = ? AND stale = 0 AND first_seen_at < ?",
                (kind, cutoff),
            )
            marked += cur.rowcount
        self.store.commit()
        return marked

    def conflicts(self, player_id: str, window_hours: int = 24) -> list[dict]:
        """Sources disagreeing about the same player's current status.

        When this returns anything, the honest output is
        DATA CONFLICT -- NO ACTION RECOMMENDED UNTIL VERIFIED, not a coin flip.
        """
        events = [
            e for e in self.for_player(player_id, hours=window_hours)
            if e["kind"] == "injury_status" and not e["stale"]
        ]
        by_value: dict[str, list[dict]] = {}
        for event in events:
            if event["new_value"]:
                by_value.setdefault(event["new_value"].lower(), []).append(event)
        if len(by_value) <= 1:
            return []
        return [
            {
                "claim": value,
                "sources": [e["source"] for e in group],
                "best_tier": min(e["source_tier"] for e in group),
                "latest": max(e["first_seen_at"] for e in group),
            }
            for value, group in by_value.items()
        ]

    def best_current_status(self, player_id: str) -> dict[str, Any]:
        """Most trustworthy current injury status, with its provenance.

        Resolves disagreement by source tier first, recency second -- an
        official report beats a fresher tweet.
        """
        conflicting = self.conflicts(player_id)
        if conflicting:
            ranked = sorted(conflicting, key=lambda c: (c["best_tier"], c["latest"]))
            return {
                "status": "DATA CONFLICT",
                "claims": conflicting,
                "leading": ranked[0]["claim"],
                "advice": "NO ACTION RECOMMENDED UNTIL VERIFIED",
            }

        events = [
            e for e in self.for_player(player_id, hours=96)
            if e["kind"] == "injury_status" and not e["stale"]
        ]
        if not events:
            return {"status": "DATA UNAVAILABLE",
                    "reason": "no injury reporting in the last 96 hours"}

        best = sorted(events, key=lambda e: (e["source_tier"], _neg_time(e["first_seen_at"])))[0]
        return {
            "status": best["new_value"],
            "source": best["source"],
            "tier": best["source_tier"],
            "verified": bool(best["verified"]),
            "as_of": best["first_seen_at"],
            "headline": best["headline"],
        }


def severity_of(status: str | None) -> int:
    if not status:
        return 0
    return STATUS_SEVERITY.get(status.strip().lower(), 0)


def _neg_time(stamp: str) -> str:
    """Sort key so that, at equal tier, newer wins."""
    return "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in stamp)
