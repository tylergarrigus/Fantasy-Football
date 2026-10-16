"""League isolation.

If these fail, the product does not work -- the entire premise is that two
leagues are evaluated independently and never contaminate each other.
"""

from __future__ import annotations

import pytest

from ff.db.context import LeagueContext, LeagueIsolationError
from ff.db.store import Store, utcnow


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


@pytest.fixture()
def two_leagues(store):
    """L1 and L2 with deliberately overlapping ids and one shared player.

    Team ids collide on purpose (both leagues have a team 1 and a team 2) --
    that is exactly the situation where a missing league filter silently
    returns the wrong roster.
    """
    now = utcnow()
    store.execute(
        "INSERT INTO nfl_players(player_id, full_name, normalized, position, nfl_team, updated_at)"
        " VALUES('p_mason','Jordan Mason','jordan mason','RB','SF',?)",
        (now,),
    )
    store.execute(
        "INSERT INTO nfl_players(player_id, full_name, normalized, position, nfl_team, updated_at)"
        " VALUES('p_cmc','Christian McCaffrey','christian mccaffrey','RB','SF',?)",
        (now,),
    )
    for lid, key, name in ((111, "L1", "Alpha"), (222, "L2", "Beta")):
        store.execute(
            "INSERT INTO leagues(league_id, league_key, name, season, my_team_id,"
            " waiver_type, faab_budget, playoff_teams, current_week, updated_at)"
            " VALUES(?,?,?,2026,1,'faab',100,6,3,?)",
            (lid, key, name, now),
        )
        for tid, tname, mine in ((1, f"{name} Mine", 1), (2, f"{name} Rival", 0)):
            store.execute(
                "INSERT INTO league_teams(league_id, team_id, name, is_mine, updated_at)"
                " VALUES(?,?,?,?,?)",
                (lid, tid, tname, mine, now),
            )

    # The divergence that drives everything:
    #   L1 -> I roster Mason.        L2 -> Mason is a free agent.
    store.execute(
        "INSERT INTO league_rosters(league_id, team_id, player_id, slot, updated_at)"
        " VALUES(111, 1, 'p_mason', 'BE', ?)",
        (now,),
    )
    store.execute(
        "INSERT INTO league_free_agents(league_id, player_id, pct_owned, updated_at)"
        " VALUES(222, 'p_mason', 12.4, ?)",
        (now,),
    )
    # CMC is rostered by me in both.
    for lid in (111, 222):
        store.execute(
            "INSERT INTO league_rosters(league_id, team_id, player_id, slot, updated_at)"
            " VALUES(?, 1, 'p_cmc', 'RB', ?)",
            (lid, now),
        )
    store.commit()
    return (
        LeagueContext(store, 111, "L1", "Alpha"),
        LeagueContext(store, 222, "L2", "Beta"),
    )


def test_same_player_has_different_availability_per_league(two_leagues):
    l1, l2 = two_leagues
    assert l1.i_own("p_mason") is True
    assert l1.is_available("p_mason") is False
    assert l2.i_own("p_mason") is False
    assert l2.is_available("p_mason") is True

    assert l1.availability("p_mason") == "mine"
    assert l2.availability("p_mason") == "available"


def test_rosters_do_not_leak_across_leagues(two_leagues):
    l1, l2 = two_leagues
    l1_players = {r["player_id"] for r in l1.roster(1)}
    l2_players = {r["player_id"] for r in l2.roster(1)}
    assert l1_players == {"p_mason", "p_cmc"}
    assert l2_players == {"p_cmc"}  # colliding team_id=1, different roster


def test_team_names_are_league_specific_despite_colliding_ids(two_leagues):
    l1, l2 = two_leagues
    assert l1.my_team()["name"] == "Alpha Mine"
    assert l2.my_team()["name"] == "Beta Mine"


def test_query_without_league_filter_is_refused(two_leagues):
    l1, _ = two_leagues
    with pytest.raises(LeagueIsolationError, match="without binding"):
        l1.query("SELECT * FROM league_rosters")


def test_caller_cannot_override_league_id(two_leagues):
    l1, _ = two_leagues
    with pytest.raises(LeagueIsolationError, match="must not pass league_id"):
        l1.query(
            "SELECT * FROM league_rosters WHERE league_id = :league_id",
            league_id=222,
        )


def test_foreign_rows_are_caught_even_if_sql_slips_through(two_leagues):
    """Defence in depth: a query that mentions :league_id but filters wrongly."""
    l1, _ = two_leagues
    with pytest.raises(LeagueIsolationError, match="surfaced in context"):
        # References :league_id (so the SQL guard passes) but deliberately
        # selects the other league's rows.
        l1.query(
            "SELECT * FROM league_teams WHERE league_id != :league_id",
        )


def test_state_hashes_are_independent(two_leagues):
    l1, l2 = two_leagues
    payload = {"roster": ["p_cmc"]}
    assert l1.state_changed("roster", payload) is True
    assert l1.state_changed("roster", payload) is False
    # L2 has never seen this key -- must still register as changed.
    assert l2.state_changed("roster", payload) is True


def test_recommendations_are_scoped(two_leagues):
    l1, l2 = two_leagues
    l1.record_recommendation(
        {"action": "START", "summary": "Start Mason", "priority": "HIGH"}
    )
    l2.record_recommendation(
        {"action": "CLAIM", "summary": "Claim Mason", "priority": "HIGH"}
    )
    assert [r["action"] for r in l1.recent_recommendations()] == ["START"]
    assert [r["action"] for r in l2.recent_recommendations()] == ["CLAIM"]


def test_preferences_global_apply_to_both_but_league_prefs_do_not(store, two_leagues):
    l1, l2 = two_leagues
    store.set_preference("posture", "aggressive")  # global
    store.set_preference("never_trade", "p_cmc", league_id=111)  # L1 only

    assert l1.posture() == "aggressive"
    assert l2.posture() == "aggressive"
    assert l1.untouchable_players() == {"p_cmc"}
    assert l2.untouchable_players() == set()
