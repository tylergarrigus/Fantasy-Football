"""Delivery, deduplication, and the decision about whether to buzz at all.

Channel is a config string, not a code path. Apprise sits behind this, so
switching from ntfy to Pushover to Discord to SMS is one environment variable
and no rewrite. That matters because the honest recommendation for ntfy comes
with a caveat (a known iOS bug can deliver its notifications silently), and the
right response to hitting that is to switch channel in ten seconds, not to
refactor.

Deduplication is the difference between a useful agent and one you mute in week
two. The same injury re-reported by four outlets, or re-polled every 30 minutes
for two days, is one notification -- and only becomes a second one if the
recommendation actually changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ff.db.store import Store, utcnow
from ff.decide.engine import Decision, Priority
from ff.logging_setup import get_logger
from ff.notify.format import Alert, format_cross_league, format_single

log = get_logger(__name__)

# Re-notify about the same subject only if urgency escalated to one of these.
ESCALATION_PRIORITIES = (Priority.CRITICAL,)


class Notifier:
    def __init__(self, store: Store, notify_url: str = "", dry_run: bool = False):
        self.store = store
        self.notify_url = notify_url
        self.dry_run = dry_run
        self._apprise: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.notify_url)

    # -- assembly ----------------------------------------------------------

    def build_alerts(self, decisions: list[Decision]) -> list[Alert]:
        """Group decisions into the fewest messages that stay clear.

        Decisions about the same player in different leagues merge into one
        cross-league alert; everything else stands alone.
        """
        by_subject: dict[str, list[Decision]] = {}
        standalone: list[Decision] = []

        for decision in decisions:
            if decision.subject_player_id:
                by_subject.setdefault(decision.subject_player_id, []).append(decision)
            else:
                standalone.append(decision)

        alerts: list[Alert] = []
        for _player_id, group in by_subject.items():
            leagues = {d.league_key for d in group}
            if len(leagues) > 1:
                alerts.append(format_cross_league(group))
            else:
                alerts.extend(format_single(d) for d in group)
        alerts.extend(format_single(d) for d in standalone)

        for alert in alerts:
            alert.fingerprint = self._fingerprint(alert)
        return alerts

    def _fingerprint(self, alert: Alert) -> str:
        """Identity of the *advice*, not of the message text.

        Keyed on league + action + player + priority, so a rewording does not
        re-notify but an escalation from HIGH to CRITICAL does.
        """
        basis = "|".join(
            sorted(
                f"{d.league_key}:{d.action}:{d.subject_player_id or d.summary[:40]}:{d.priority}"
                for d in alert.decisions
            )
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:24]

    # -- dedup -------------------------------------------------------------

    def should_send(self, alert: Alert) -> tuple[bool, str]:
        existing = self.store.one(
            "SELECT * FROM notifications_sent WHERE fingerprint = ?", (alert.fingerprint,)
        )
        if existing is None:
            return True, "new"

        # Same advice already delivered. Only re-send on genuine escalation.
        old_priority = existing["priority"]
        if alert.priority in ESCALATION_PRIORITIES and old_priority != alert.priority:
            return True, f"escalated {old_priority} -> {alert.priority}"
        return False, f"already sent at {existing['sent_at']}"

    # -- delivery ----------------------------------------------------------

    def _apprise_client(self) -> Any:
        if self._apprise is None:
            try:
                import apprise

                self._apprise = apprise.Apprise()
                if self.notify_url:
                    if not self._apprise.add(self.notify_url):
                        log.error(
                            "notification URL was not accepted by Apprise. Check the "
                            "format -- e.g. ntfy://ntfy.sh/your-topic or pover://user@token"
                        )
                        return None
            except ImportError:
                log.error("apprise not installed; cannot deliver notifications")
                return None
        return self._apprise

    def send(self, alert: Alert) -> bool:
        ok, reason = self.should_send(alert)
        if not ok:
            log.info("suppressed duplicate alert (%s): %s", reason, alert.title)
            return False

        delivered = False
        error: str | None = None

        if self.dry_run:
            log.info("DRY RUN -- would send:\n%s\n%s", alert.title, alert.body)
            delivered = True
        elif not self.configured:
            log.warning("no notification channel configured; alert recorded only")
            print(f"\n{alert.title}\n{'-' * len(alert.title)}\n{alert.body}\n")
            error = "no channel configured"
        else:
            client = self._apprise_client()
            if client is None:
                error = "apprise unavailable"
            else:
                try:
                    delivered = bool(
                        client.notify(title=alert.title, body=alert.body)
                    )
                    if not delivered:
                        error = "apprise reported delivery failure"
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                    log.error("notification delivery failed: %s", exc)

        self._record(alert, delivered, error)
        if delivered:
            log.info("sent [%s] %s", alert.priority, alert.title)
        return delivered

    def send_all(self, alerts: list[Alert]) -> int:
        """Send in priority order so the urgent one lands first."""
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        sent = 0
        for alert in sorted(alerts, key=lambda a: order.get(a.priority, 9)):
            if self.send(alert):
                sent += 1
        return sent

    def _record(self, alert: Alert, delivered: bool, error: str | None) -> None:
        self.store.execute(
            """
            INSERT INTO notifications_sent(fingerprint, sent_at, priority, league_ids,
                                           rec_ids, title, body, channel, delivered, error)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                sent_at = excluded.sent_at, priority = excluded.priority,
                title = excluded.title, body = excluded.body,
                delivered = excluded.delivered, error = excluded.error
            """,
            (
                alert.fingerprint, utcnow(), alert.priority,
                json.dumps(alert.league_keys), json.dumps(alert.rec_ids),
                alert.title, alert.body,
                self._channel_name(), int(delivered), error,
            ),
        )
        self.store.commit()

    def _channel_name(self) -> str:
        """Scheme only. The URL contains credentials and must never be stored."""
        if not self.notify_url:
            return "none"
        return self.notify_url.split("://", 1)[0]

    # -- introspection -----------------------------------------------------

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.store.query(
                "SELECT fingerprint, sent_at, priority, league_ids, title, delivered, error "
                "FROM notifications_sent ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def explain(self, fingerprint_prefix: str) -> dict[str, Any] | None:
        """Answers 'why did you send me that?' -- the full stored body and recs."""
        row = self.store.one(
            "SELECT * FROM notifications_sent WHERE fingerprint LIKE ?",
            (f"{fingerprint_prefix}%",),
        )
        if not row:
            return None
        out = dict(row)
        rec_ids = json.loads(out.get("rec_ids") or "[]")
        out["recommendations"] = [
            dict(r)
            for rid in rec_ids
            for r in self.store.query("SELECT * FROM recommendations WHERE rec_id = ?", (rid,))
        ]
        return out
