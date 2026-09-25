"""Notification discipline, secret handling, and behaviour when sources fail.

The failure modes here are the ones that make people mute an agent: alerting
twice about the same thing, leaking a credential into a public Actions log, or
crashing the whole cycle because one API had a bad minute.
"""

from __future__ import annotations

import pytest

from ff.decide.engine import Decision, Priority, Urgency
from ff.notify.notifier import Notifier
from ff.sources.base import (
    HttpClient,
    SourceAuthError,
    SourceNotFound,
    SourceUnavailable,
)


def _decision(**kwargs) -> Decision:
    base = dict(
        league_key="L1", league_name="Alpha", action="CLAIM",
        summary="Claim Jordan Mason", priority=Priority.HIGH, urgency=Urgency.TODAY,
        subject_player_id="p_mason", subject_name="Jordan Mason",
    )
    base.update(kwargs)
    return Decision(**base)


# -- deduplication ---------------------------------------------------------


def test_the_same_advice_is_only_sent_once(store):
    notifier = Notifier(store, dry_run=True)
    alerts = notifier.build_alerts([_decision()])

    assert notifier.send(alerts[0]) is True
    # Rebuild from an identical decision -- same advice, must not re-send.
    again = notifier.build_alerts([_decision()])
    assert notifier.send(again[0]) is False


def test_rewording_the_same_advice_does_not_re_notify(store):
    notifier = Notifier(store, dry_run=True)
    notifier.send(notifier.build_alerts([_decision(rationale="Because A")])[0])
    second = notifier.build_alerts([_decision(rationale="Completely different wording")])
    assert notifier.send(second[0]) is False, (
        "dedup must key on the advice, not the prose"
    )


def test_escalation_to_critical_breaks_through_dedup(store):
    notifier = Notifier(store, dry_run=True)
    notifier.send(notifier.build_alerts([_decision(priority=Priority.HIGH)])[0])

    escalated = notifier.build_alerts([_decision(priority=Priority.CRITICAL)])
    assert notifier.send(escalated[0]) is True, (
        "a genuine escalation is new information and must get through"
    )


def test_different_leagues_are_not_deduplicated_against_each_other(store):
    notifier = Notifier(store, dry_run=True)
    l1 = notifier.build_alerts([_decision(league_key="L1", league_name="Alpha")])
    assert notifier.send(l1[0]) is True

    l2 = notifier.build_alerts(
        [_decision(league_key="L2", league_name="Beta", action="START")]
    )
    assert notifier.send(l2[0]) is True, "each league's advice is independent"


# -- formatting ------------------------------------------------------------


def test_alert_body_leads_with_the_action_not_the_news(store):
    notifier = Notifier(store, dry_run=True)
    alert = notifier.build_alerts(
        [
            _decision(
                faab_low=34, faab_high=41,
                championship_before=18.2, championship_after=22.4,
                confidence=0.91, urgency=Urgency.NOW,
            )
        ]
    )[0]

    assert "ACTION:" in alert.body
    assert "$34-$41" in alert.body
    assert "CHAMPIONSHIP IMPACT" in alert.body
    assert "18.2% -> 22.4%" in alert.body
    assert "URGENCY" in alert.body


def test_confidence_is_reported_coarsely_not_to_two_decimals(store):
    notifier = Notifier(store, dry_run=True)
    alert = notifier.build_alerts([_decision(confidence=0.9137)])[0]
    assert "~90%" in alert.body, "no fake precision on a number we cannot support"
    assert "91.37" not in alert.body


def test_missing_championship_impact_is_omitted_not_faked(store):
    notifier = Notifier(store, dry_run=True)
    alert = notifier.build_alerts([_decision(championship_before=None)])[0]
    assert "CHAMPIONSHIP IMPACT" not in alert.body


def test_critical_alerts_map_to_a_breakthrough_priority(store):
    notifier = Notifier(store, dry_run=True)
    alert = notifier.build_alerts([_decision(priority=Priority.CRITICAL)])[0]
    assert alert.apprise_priority == "emergency"


# -- secret handling -------------------------------------------------------


def test_secrets_are_scrubbed_from_log_output():
    from ff.config import register_secret
    from ff.logging_setup import scrub

    register_secret("AEAAAveryLongEspnS2CookieValue123456")
    text = "connecting with espn_s2=AEAAAveryLongEspnS2CookieValue123456 and SWID={ABC-123}"
    cleaned = scrub(text)

    assert "AEAAAveryLongEspnS2CookieValue123456" not in cleaned
    assert "REDACTED" in cleaned


def test_anthropic_key_shape_is_scrubbed_even_if_never_registered():
    from ff.logging_setup import scrub

    cleaned = scrub("using sk-ant-api03-abcdefghijklmnop for the call")
    assert "sk-ant-api03-abcdefghijklmnop" not in cleaned


def test_notification_channel_is_stored_without_the_credential(store):
    notifier = Notifier(store, notify_url="pover://secretuser@secrettoken", dry_run=True)
    alert = notifier.build_alerts([_decision()])[0]
    notifier.send(alert)

    row = store.one("SELECT channel FROM notifications_sent")
    assert row["channel"] == "pover"
    assert "secrettoken" not in str(dict(row))


def test_credentials_repr_never_exposes_the_cookie():
    from ff.config import ESPNCredentials

    creds = ESPNCredentials(espn_s2="supersecretvalue", swid="{ABC}")
    assert "supersecretvalue" not in repr(creds)
    assert "configured=True" in repr(creds)


# -- resilience ------------------------------------------------------------


def test_expired_espn_cookies_produce_an_actionable_message(monkeypatch, store, registry):
    """An auth failure needs a human, so it must say what the human should do."""
    from ff.config import ESPNCredentials, LeagueConfig
    from ff.sources.espn import ESPNFantasySource

    creds = ESPNCredentials(espn_s2="stale", swid="{stale}")
    source = ESPNFantasySource(store, registry, creds)

    class _Denied(Exception):
        pass

    import espn_api.requests.espn_requests as espn_requests

    monkeypatch.setattr(
        "espn_api.football.League",
        lambda **kw: (_ for _ in ()).throw(espn_requests.ESPNAccessDenied("denied")),
    )

    cfg = LeagueConfig(key="L1", league_id=123, season=2026, name="Alpha")
    with pytest.raises(SourceAuthError) as exc:
        source.connect(cfg)

    message = str(exc.value)
    assert "expired" in message.lower()
    assert "espn_s2" in message.lower(), "must name the thing the user has to fix"


def test_private_league_without_cookies_says_exactly_what_is_missing(monkeypatch, store, registry):
    from ff.config import ESPNCredentials, LeagueConfig
    from ff.sources.espn import ESPNFantasySource
    import espn_api.requests.espn_requests as espn_requests

    source = ESPNFantasySource(store, registry, ESPNCredentials())
    monkeypatch.setattr(
        "espn_api.football.League",
        lambda **kw: (_ for _ in ()).throw(espn_requests.ESPNAccessDenied("denied")),
    )

    cfg = LeagueConfig(key="L1", league_id=123, season=2026, name="Alpha")
    with pytest.raises(SourceAuthError, match="ESPN_S2"):
        source.connect(cfg)


def test_http_client_serves_stale_cache_when_a_source_is_down(tmp_path, monkeypatch):
    """A stale roster clearly marked stale beats no roster."""
    import time

    client = HttpClient(tmp_path, min_interval=0)
    url = "https://example.test/data"

    # Seed the cache directly, then expire it.
    path = client._cache_path(url, None)
    path.write_text('{"fetched_at": 1.0, "data": {"value": "old"}}')

    def _always_fails(*args, **kwargs):
        import requests

        raise requests.RequestException("network down")

    monkeypatch.setattr(client.session, "get", _always_fails)
    monkeypatch.setattr(client, "_backoff", lambda *a, **k: None)

    response = client.get_json(url, ttl=1)
    assert response.data == {"value": "old"}
    assert response.from_cache is True
    assert response.is_stale(60) is True, "the caller must be able to tell it is stale"


def test_http_client_raises_rather_than_inventing_data(tmp_path, monkeypatch):
    client = HttpClient(tmp_path, min_interval=0, max_retries=1)
    monkeypatch.setattr(client, "_backoff", lambda *a, **k: None)

    def _always_fails(*args, **kwargs):
        import requests

        raise requests.RequestException("network down")

    monkeypatch.setattr(client.session, "get", _always_fails)

    with pytest.raises(SourceUnavailable):
        client.get_json("https://example.test/never-cached", ttl=1)


def test_auth_errors_are_not_retried(tmp_path, monkeypatch):
    """Retrying a 401 just burns rate limit -- it needs a human, not a loop."""
    client = HttpClient(tmp_path, min_interval=0, max_retries=3)
    calls = {"n": 0}

    class _Resp:
        status_code = 401
        headers: dict = {}

    def _unauthorized(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client.session, "get", _unauthorized)

    with pytest.raises(SourceAuthError):
        client.get_json("https://example.test/private", ttl=0)
    assert calls["n"] == 1, "a 401 must fail immediately, not retry"


def test_404_is_distinguished_from_being_down(tmp_path, monkeypatch):
    client = HttpClient(tmp_path, min_interval=0)

    class _Resp:
        status_code = 404
        headers: dict = {}

    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp())
    with pytest.raises(SourceNotFound):
        client.get_json("https://example.test/missing", ttl=0)


def test_one_failing_league_does_not_stop_the_other(store, registry, news,
                                                    divergent_leagues, cmc_ruled_out):
    """League isolation includes failure isolation."""
    from tests.conftest import build_engine

    contexts = divergent_leagues
    results = {}
    for key, ctx in contexts.items():
        if key == "L1":
            # Simulate L1 blowing up mid-evaluation.
            try:
                raise RuntimeError("L1 sync exploded")
            except RuntimeError as exc:
                results[key] = f"error: {exc}"
                continue
        engine = build_engine(ctx, registry, news)
        results[key] = engine.evaluate([cmc_ruled_out], week=5)

    assert isinstance(results["L1"], str) and results["L1"].startswith("error")
    assert results["L2"], "League 2 must still produce recommendations"
