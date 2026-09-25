"""Notification formatting.

The format is the product. A notification that requires opening an app to act on
has failed. Every alert answers, in this order: which league, what to do, why,
how confident, how urgent.

Cross-league events are merged into one message with a section per league --
one NFL event, two independent decisions, one buzz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ff.decide.engine import Decision, Priority

PRIORITY_ICON = {
    Priority.CRITICAL: "\U0001f6a8",  # rotating light
    Priority.HIGH: "\U0001f525",      # fire
    Priority.MEDIUM: "\U0001f440",    # eyes
    Priority.LOW: "ℹ️",     # info
}

# Apprise priority levels, so CRITICAL actually breaks through on the phone.
APPRISE_PRIORITY = {
    Priority.CRITICAL: "emergency",
    Priority.HIGH: "high",
    Priority.MEDIUM: "normal",
    Priority.LOW: "low",
}


@dataclass
class Alert:
    """One outbound notification, possibly covering both leagues."""

    priority: str
    title: str
    body: str
    decisions: list[Decision] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def league_keys(self) -> list[str]:
        return sorted({d.league_key for d in self.decisions})

    @property
    def rec_ids(self) -> list[str]:
        return [d.evidence.get("rec_id") for d in self.decisions if d.evidence.get("rec_id")]

    @property
    def apprise_priority(self) -> str:
        return APPRISE_PRIORITY.get(self.priority, "normal")


def _urgency_line(decision: Decision) -> str:
    return {
        "NOW": "URGENCY: act now",
        "TODAY": "URGENCY: today",
        "THIS_WEEK": "URGENCY: this week",
        "MONITOR": "URGENCY: monitor only",
    }.get(decision.urgency, "")


def _impact_line(decision: Decision) -> str | None:
    delta = decision.championship_delta
    if delta is None:
        return None
    if abs(delta) < 0.05:
        return "CHAMPIONSHIP IMPACT: negligible"
    return (
        f"CHAMPIONSHIP IMPACT: {decision.championship_before:.1f}% -> "
        f"{decision.championship_after:.1f}% ({delta:+.1f})"
    )


def _confidence_line(decision: Decision) -> str | None:
    if decision.confidence is None:
        return None
    # Deliberately coarse. The model is not calibrated to the percentage point,
    # so reporting it to one would be false precision.
    pct = round(decision.confidence * 100 / 5) * 5
    return f"CONFIDENCE: ~{pct}%"


def format_single(decision: Decision) -> Alert:
    """One league, one decision."""
    icon = PRIORITY_ICON.get(decision.priority, "")
    title = f"{icon} {decision.league_name} — {decision.summary}"

    lines = [f"{icon} {decision.league_name.upper()}", ""]
    if decision.subject_name:
        lines.append(f"PLAYER: {decision.subject_name}")
    trigger = (decision.evidence.get("event") or {}).get("headline")
    if trigger:
        lines.append(f"NEWS: {trigger}")
    lines.append(f"ACTION: {decision.summary}")
    if decision.faab_low and decision.faab_high:
        lines.append(f"FAAB: ${decision.faab_low}-${decision.faab_high}")
    if decision.rationale:
        lines += ["", f"WHY: {decision.rationale}"]

    lines.append("")
    for line in (_impact_line(decision), _confidence_line(decision), _urgency_line(decision)):
        if line:
            lines.append(line)

    if decision.assumptions:
        lines += ["", *decision.assumptions]

    return Alert(
        priority=decision.priority,
        title=title[:120],
        body="\n".join(lines),
        decisions=[decision],
    )


def format_cross_league(decisions: list[Decision]) -> Alert:
    """One NFL event, both leagues, one message.

    This is the shape the whole system exists to produce: the same news, two
    different correct answers, delivered together so the difference is obvious.
    """
    subject = next((d.subject_name for d in decisions if d.subject_name), "Multiple players")
    trigger = next(
        (
            (d.evidence.get("event") or {}).get("headline")
            for d in decisions
            if (d.evidence.get("event") or {}).get("headline")
        ),
        None,
    )
    priority = min(
        (d.priority for d in decisions),
        key=lambda p: ["CRITICAL", "HIGH", "MEDIUM", "LOW"].index(p),
    )
    icon = PRIORITY_ICON.get(priority, "")

    title = f"{icon} BOTH LEAGUES — {subject}"
    lines = [f"{icon} NFL NEWS — AFFECTS BOTH LEAGUES", ""]
    if trigger:
        lines += [trigger, ""]

    for decision in sorted(decisions, key=lambda d: d.league_key):
        lines.append(f"── {decision.league_name.upper()} ──")
        lines.append(f"→ {decision.summary}")
        if decision.faab_low and decision.faab_high:
            lines.append(f"   FAAB: ${decision.faab_low}-${decision.faab_high}")
        if decision.rationale:
            lines.append(f"   {decision.rationale}")
        impact = _impact_line(decision)
        if impact:
            lines.append(f"   {impact}")
        confidence = _confidence_line(decision)
        if confidence:
            lines.append(f"   {confidence}")
        lines.append("")

    return Alert(priority=priority, title=title[:120], body="\n".join(lines).rstrip(),
                 decisions=decisions)


def format_digest(decisions: list[Decision], heading: str = "Weekly review") -> Alert:
    """Low-urgency roll-up. Used for the scheduled deep pass, never for breaking news."""
    lines = [heading, ""]
    by_league: dict[str, list[Decision]] = {}
    for decision in decisions:
        by_league.setdefault(decision.league_name, []).append(decision)

    for league, items in sorted(by_league.items()):
        lines.append(f"── {league.upper()} ──")
        for decision in sorted(items, key=lambda d: d.rank_key()):
            icon = PRIORITY_ICON.get(decision.priority, "")
            line = f"{icon} {decision.summary}"
            delta = decision.championship_delta
            if delta is not None and abs(delta) >= 0.1:
                line += f"  ({delta:+.1f}% title odds)"
            lines.append(line)
        lines.append("")

    return Alert(
        priority=Priority.MEDIUM,
        title=f"{heading} — {len(decisions)} item(s)",
        body="\n".join(lines).rstrip(),
        decisions=decisions,
    )


def format_no_action(league_names: list[str]) -> str:
    """What a quiet cycle looks like. Never sent -- logged only."""
    return (
        f"No action required in {' or '.join(league_names)}. "
        "Nothing material changed."
    )
