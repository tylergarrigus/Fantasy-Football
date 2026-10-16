"""NFL intelligence: dedup, corroboration, staleness, conflict, and identity."""

from __future__ import annotations

import pytest

from ff.identity import normalize_name, normalize_team
from ff.intel.news import NFLEvent, NewsEngine, severity_of


# -- identity --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("D.J. Moore", "d j moore"),
        ("DJ Moore", "d j moore"),
        ("Michael Pittman Jr.", "michael pittman"),
        ("Kenneth Walker III", "kenneth walker"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Hollywood Brown", "marquise brown"),
        ("  Josh   Allen  ", "josh allen"),
    ],
)
def test_name_normalization_collapses_source_variants(raw, expected):
    assert normalize_name(raw) == expected


def test_team_abbreviation_drift_is_normalized():
    assert normalize_team("JAX") == normalize_team("JAC") == "JAC"
    assert normalize_team("WSH") == "WAS"
    assert normalize_team(None) is None


def test_ambiguous_name_refuses_to_guess(registry):
    """Two players with the same name must resolve to nothing, not a coin flip."""
    registry.upsert_player("p_a", "Josh Allen", position="QB", nfl_team="BUF")
    registry.upsert_player("p_b", "Josh Allen", position="LB", nfl_team="JAC")
    registry.store.commit()

    assert registry.by_name("Josh Allen") is None, "ambiguous name must not resolve"
    # Disambiguating by position is enough.
    hit = registry.by_name("Josh Allen", position="QB")
    assert hit is not None and hit.player_id == "p_a"
    assert hit.confidence < 1.0, "name matches must be flagged as less than certain"


def test_sleeper_ingest_builds_the_espn_crosswalk(registry):
    registry.ingest_sleeper_universe(
        {
            "4034": {
                "full_name": "Christian McCaffrey", "position": "RB", "team": "SF",
                "espn_id": 3117251, "gsis_id": "00-0033280", "age": 30,
            },
            "9999": {"full_name": "Some Guard", "position": "G", "team": "SF"},
        }
    )
    # ESPN's id now resolves without any name matching at all.
    hit = registry.by_source_id("espn", 3117251)
    assert hit is not None and hit.full_name == "Christian McCaffrey"
    assert hit.confidence == 1.0

    assert registry.by_source_id("nflverse", "00-0033280") is not None
    # Non-fantasy positions are skipped, not stored.
    assert registry.by_name("Some Guard") is None


# -- news engine -----------------------------------------------------------


@pytest.fixture()
def known_player(registry):
    """Events must reference a player the registry actually knows about."""
    registry.upsert_player("p_x", "Known Player", position="RB", nfl_team="SF")
    registry.store.commit()
    return "p_x"


def _event(**kwargs) -> NFLEvent:
    base = dict(
        kind="injury_status", headline="Player is Out", source="sleeper_status",
        player_id="p_x", new_value="Out",
    )
    base.update(kwargs)
    return NFLEvent(**base)


def test_event_for_an_unknown_player_is_kept_but_unlinked(store, news):
    """A dangling player reference must not crash the cycle or lose the news."""
    fresh = news.record([_event(player_id="p_never_ingested")])
    assert len(fresh) == 1
    assert fresh[0].player_id is None, "unresolvable reference should be dropped, not stored"
    row = store.one("SELECT headline FROM nfl_events")
    assert row is not None, "the headline itself must survive"


def test_identical_event_is_only_recorded_once(store, news, known_player):
    first = news.record([_event()])
    second = news.record([_event()])
    assert len(first) == 1
    assert second == [], "re-polling the same news must not produce a second event"


def test_same_change_from_two_sources_is_corroborated(store, news, known_player):
    news.record([_event(source="sleeper_status")])
    news.record([_event(source="espn_injury_report")])

    row = store.one("SELECT verified, source_tier FROM nfl_events WHERE player_id = 'p_x'")
    assert row["verified"] == 1, "two independent sources should mark the event verified"
    assert row["source_tier"] == 2, "tier should improve to the better source"


def test_status_change_produces_a_new_event(store, news, known_player):
    news.record([_event(new_value="Questionable")])
    fresh = news.record([_event(new_value="Out", old_value="Questionable")])
    assert len(fresh) == 1, "a genuine downgrade is new news even for the same player"


def test_injury_diff_only_reports_actual_changes(registry, news):
    registry.ingest_sleeper_universe(
        {"1": {"full_name": "A Player", "position": "RB", "team": "SF"}}
    )
    previous = {"1": {"status": "Questionable", "name": "A Player", "team": "SF", "position": "RB"}}
    unchanged = news.from_injury_diff(previous, previous)
    assert unchanged == [], "an unchanged designation is not news"

    current = {"1": {"status": "Out", "name": "A Player", "team": "SF", "position": "RB"}}
    changed = news.from_injury_diff(previous, current)
    assert len(changed) == 1
    assert changed[0].new_value == "Out"
    assert changed[0].severity == "major"


def test_clearing_an_injury_designation_is_also_news(registry, news):
    registry.ingest_sleeper_universe(
        {"1": {"full_name": "A Player", "position": "RB", "team": "SF"}}
    )
    previous = {"1": {"status": "Out", "name": "A Player", "team": "SF", "position": "RB"}}
    events = news.from_injury_diff(previous, {})
    assert len(events) == 1
    assert events[0].new_value == "Active"


def test_conflicting_sources_produce_a_data_conflict_not_a_guess(store, news, known_player):
    news.record([_event(source="espn_news", new_value="Out")])
    news.record([_event(source="sleeper_status", new_value="Questionable")])

    conflicts = news.conflicts("p_x")
    assert len(conflicts) == 2, "two different claims about the same player is a conflict"

    verdict = news.best_current_status("p_x")
    assert verdict["status"] == "DATA CONFLICT"
    assert verdict["advice"] == "NO ACTION RECOMMENDED UNTIL VERIFIED"


def test_best_status_prefers_the_more_reliable_source(store, news, known_player):
    news.record([_event(source="sleeper_trending", new_value="Out")])
    verdict = news.best_current_status("p_x")
    assert verdict["status"] == "Out"
    assert verdict["tier"] == 7, "a social signal should be labelled as the weak source it is"


def test_missing_data_says_so_rather_than_defaulting(news):
    verdict = news.best_current_status("p_never_seen")
    assert verdict["status"] == "DATA UNAVAILABLE"
    assert "reason" in verdict


def test_stale_events_are_marked_not_silently_used(store, news, known_player):
    news.record([_event(kind="inactive", first_seen_at="2020-01-01T00:00:00+00:00")])
    marked = news.mark_stale()
    assert marked >= 1

    row = store.one("SELECT stale FROM nfl_events WHERE kind = 'inactive'")
    assert row["stale"] == 1
    # And a stale event must not drive a status decision.
    assert news.best_current_status("p_x")["status"] == "DATA UNAVAILABLE"


def test_fantasy_advice_headlines_are_filtered_out(news):
    events = news.from_espn_articles(
        [
            {"id": "1", "headline": "Week 5 Start 'Em Sit 'Em: fantasy football lineup advice"},
            {"id": "2", "headline": "Best waiver wire pickups for Week 5"},
            {"id": "3", "headline": "Nico Collins ruled out with a hamstring injury"},
        ]
    )
    headlines = [e.headline for e in events]
    assert len(headlines) == 1, f"content-farm headlines should be dropped, got {headlines}"
    assert "Nico Collins" in headlines[0]


def test_severity_ordering_detects_a_downgrade():
    assert severity_of("Out") > severity_of("Doubtful") > severity_of("Questionable")
    assert severity_of("IR") > severity_of("Out")
    assert severity_of(None) == 0


def test_depth_chart_promotion_to_starter_is_major(registry, news):
    registry.ingest_sleeper_universe(
        {"1": {"full_name": "Backup Back", "position": "RB", "team": "SF"}}
    )
    events = news.from_depth_chart_diff({"1": "SF:RB:2"}, {"1": "SF:RB:1"})
    assert len(events) == 1
    assert events[0].severity == "major"
    assert "moved up" in events[0].headline
