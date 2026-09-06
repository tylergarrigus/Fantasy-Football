"""THE critical test.

One NFL event. Two leagues. Two different correct answers.

  League 1 -- Tyler already rosters Jordan Mason  ->  START MASON
  League 2 -- Mason is on waivers                 ->  CLAIM MASON

If the system ever produces the same recommendation in both leagues just because
the NFL event was the same, it has failed at the one thing it exists to do, and
every other feature is decoration.
"""

from __future__ import annotations

import pytest

from tests.conftest import build_engine


@pytest.fixture()
def decisions(divergent_leagues, cmc_ruled_out, registry, news):
    out = {}
    for key, ctx in divergent_leagues.items():
        engine = build_engine(ctx, registry, news)
        out[key] = engine.evaluate([cmc_ruled_out], week=5)
    return out


def _find(decisions, action):
    return [d for d in decisions if d.action == action]


def test_league_1_recommends_starting_the_backup_it_already_owns(decisions):
    l1 = decisions["L1"]
    starts = _find(l1, "START")
    assert starts, f"expected a START recommendation in League 1, got {[d.action for d in l1]}"

    mason_start = [d for d in starts if d.subject_name == "Jordan Mason"]
    assert mason_start, "League 1 should start the backup it already rosters"

    decision = mason_start[0]
    assert decision.priority == "CRITICAL"
    assert decision.urgency == "NOW"
    assert "Jordan Mason" in decision.summary


def test_league_1_does_not_recommend_claiming_a_player_it_already_owns(decisions):
    """The failure mode: telling him to claim a player already on his roster."""
    l1 = decisions["L1"]
    claims = [d for d in l1 if d.action in ("CLAIM", "ADD")]
    mason_claims = [d for d in claims if d.subject_name == "Jordan Mason"]
    assert not mason_claims, (
        "League 1 rosters Mason -- recommending a claim would be nonsense. "
        f"Got: {[d.summary for d in mason_claims]}"
    )


def test_league_2_recommends_claiming_the_backup_it_does_not_own(decisions):
    l2 = decisions["L2"]
    claims = [d for d in l2 if d.action in ("CLAIM", "ADD")]
    assert claims, f"expected a CLAIM in League 2, got {[d.action for d in l2]}"

    mason_claims = [d for d in claims if d.subject_name == "Jordan Mason"]
    assert mason_claims, "League 2 should claim the available backup"

    decision = mason_claims[0]
    assert decision.priority in ("CRITICAL", "HIGH")
    assert decision.faab_low is not None, "a FAAB league claim must carry a bid range"
    assert decision.faab_high > decision.faab_low


def test_league_2_does_not_recommend_starting_a_player_it_does_not_roster(decisions):
    l2 = decisions["L2"]
    mason_starts = [
        d for d in l2 if d.action == "START" and d.subject_name == "Jordan Mason"
    ]
    assert not mason_starts, (
        "League 2 does not roster Mason -- 'start him' is not a legal action there."
    )


def test_the_two_leagues_produce_genuinely_different_actions(decisions):
    """The headline assertion, stated directly."""
    l1_actions = {d.action for d in decisions["L1"] if d.subject_name == "Jordan Mason"}
    l2_actions = {d.action for d in decisions["L2"] if d.subject_name == "Jordan Mason"}

    assert l1_actions, "League 1 produced no Mason decision at all"
    assert l2_actions, "League 2 produced no Mason decision at all"
    assert l1_actions != l2_actions, (
        f"Both leagues produced the same action for Mason ({l1_actions}). "
        "The same NFL event must not produce identical recommendations when "
        "roster context differs -- this is the core requirement."
    )
    assert "START" in l1_actions
    assert l2_actions & {"CLAIM", "ADD"}


def test_faab_recommendation_is_a_range_not_a_fake_precise_number(decisions):
    claims = [
        d for d in decisions["L2"]
        if d.action in ("CLAIM", "ADD") and d.subject_name == "Jordan Mason"
    ]
    decision = claims[0]
    assert decision.faab_low < decision.faab_high, "FAAB advice must be a range"
    assert decision.faab_high <= 100, "cannot bid more than the budget"
    assert decision.faab_low >= 1


def test_cross_league_alert_merges_into_one_notification(decisions, store):
    """One event affecting both leagues is one buzz, not two."""
    from ff.notify.notifier import Notifier

    combined = [
        d for d in decisions["L1"] + decisions["L2"]
        if d.subject_name == "Jordan Mason"
    ]
    notifier = Notifier(store)
    alerts = notifier.build_alerts(combined)

    merged = [a for a in alerts if len(a.league_keys) > 1]
    assert merged, "decisions about the same player in both leagues should merge"

    alert = merged[0]
    assert "BOTH LEAGUES" in alert.title
    assert "ALPHA LEAGUE" in alert.body
    assert "BETA LEAGUE" in alert.body
    # And the two sections must say different things.
    assert "Start" in alert.body or "START" in alert.body
    assert "laim" in alert.body or "CLAIM" in alert.body


def test_no_event_means_no_action(divergent_leagues, registry, news):
    """The default must be silence."""
    ctx = divergent_leagues["L1"]
    engine = build_engine(ctx, registry, news)
    decisions = engine.evaluate([], week=5)
    notifiable = engine.filter_for_notification(decisions)

    # A perfectly-set lineup with no news should not generate an alert.
    assert not [d for d in notifiable if d.priority == "CRITICAL"], (
        "no news should never produce a CRITICAL alert"
    )


def test_event_for_irrelevant_player_produces_nothing(divergent_leagues, registry, news):
    """An injury to someone neither league can use is not our problem."""
    from ff.intel.news import NFLEvent

    registry.upsert_player("p_nobody", "Third String Guy", position="RB", nfl_team="NYG")
    registry.store.commit()

    event = NFLEvent(
        kind="injury_status", player_id="p_nobody", headline="Third String Guy is Out",
        new_value="Out", severity="major", source="sleeper_status",
    )
    news.record([event])
    payload = dict(event.__dict__, event_id=event.event_id)

    for ctx in divergent_leagues.values():
        engine = build_engine(ctx, registry, news)
        decisions = [
            d for d in engine.evaluate([payload], week=5)
            if d.subject_name == "Third String Guy"
        ]
        assert not decisions, "a player in neither league should generate no decision"
