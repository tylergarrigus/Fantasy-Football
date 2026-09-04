"""Draft board: value over replacement, tiers, scarcity, and the wait/reach call."""

from __future__ import annotations

import pytest

from ff.db.store import utcnow
from ff.engines.draft import DraftEngine
from tests.conftest import LeagueBuilder, add_player


@pytest.fixture()
def drafting_league(store, registry):
    """A 12-team PPR league, pre-draft, with a realistic value curve.

    RB is deliberately top-heavy (a few elite, then a cliff) and WR deliberately
    flat (many similar players). That is the real shape of a fantasy draft, and
    it is what makes 'take the RB now, wait on WR' the correct answer rather
    than a slogan.
    """
    builder = LeagueBuilder(store, 3003, "L1", "Draft League")
    builder.create(week=1, roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1,
                                         "FLEX": 1, "K": 1, "DST": 1})
    mine = builder.team("Tyler", mine=True)
    for i in range(11):
        builder.team(f"Team {i + 2}")

    now = utcnow()

    def rank(pid, name, position, projected, adp, rank_no):
        add_player(registry, pid, name, position, "SF")
        store.execute(
            "INSERT INTO draft_rankings(league_id, player_id, adp, draft_rank, "
            "projected, updated_at) VALUES(?,?,?,?,?,?)",
            (3003, pid, adp, rank_no, projected, now),
        )

    # RB: three elite, then a hard cliff.
    rank("rb1", "Elite RB One", "RB", 320.0, 2, 1)
    rank("rb2", "Elite RB Two", "RB", 310.0, 4, 2)
    rank("rb3", "Elite RB Three", "RB", 300.0, 6, 3)
    for i in range(4, 40):
        rank(f"rb{i}", f"RB {i}", "RB", 230.0 - i * 3.0, 10.0 + i * 2, i)

    # WR: flat. Lots of near-identical options, so waiting costs little.
    for i in range(1, 45):
        rank(f"wr{i}", f"WR {i}", "WR", 285.0 - i * 2.5, 5.0 + i * 2, i)

    rank("qb1", "Elite QB", "QB", 400.0, 25, 1)
    for i in range(2, 20):
        rank(f"qb{i}", f"QB {i}", "QB", 340.0 - i * 4.0, 60.0 + i * 4, i)

    for i in range(1, 20):
        rank(f"te{i}", f"TE {i}", "TE", 200.0 - i * 9.0, 30.0 + i * 6, i)
    for i in range(1, 14):
        rank(f"k{i}", f"K {i}", "K", 140.0 - i, 150.0 + i, i)
        rank(f"dst{i}", f"DST {i}", "DST", 130.0 - i * 2, 150.0 + i, i)

    store.commit()
    return builder.ctx, mine


def test_value_over_replacement_makes_positions_comparable(drafting_league):
    """The elite QB outscores every RB and should still not be the top pick.

    400 raw points from a QB looks dominant next to 320 from a RB. But in a
    1-QB league the 12th-best QB is nearly as good, so the *gap* is small --
    which is precisely what VOR captures and raw projection hides.
    """
    ctx, _ = drafting_league
    board = DraftEngine(ctx).build_board()
    by_name = {p.name: p for p in board}

    qb = by_name["Elite QB"]
    rb = by_name["Elite RB One"]

    assert qb.projected > rb.projected, "fixture sanity: the QB scores more"
    assert rb.vor > qb.vor, (
        f"RB should be the better pick on value over replacement "
        f"(RB {rb.vor:+.1f} vs QB {qb.vor:+.1f}) despite scoring fewer points"
    )
    assert board[0].position == "RB"


def test_tiers_form_at_real_value_cliffs(drafting_league):
    ctx, _ = drafting_league
    board = DraftEngine(ctx).build_board()
    elite_rbs = [p for p in board if p.position == "RB" and p.name.startswith("Elite")]

    assert len({p.tier for p in elite_rbs}) == 1, "the three elite RBs belong together"
    assert all(p.tier == 1 for p in elite_rbs)

    rb4 = next(p for p in board if p.name == "RB 4")
    assert rb4.tier > elite_rbs[0].tier, "the cliff after the elite RBs must be a tier break"


def test_streamable_positions_are_suppressed_early(drafting_league):
    """Drafting a kicker in round 2 is a real and avoidable mistake."""
    ctx, _ = drafting_league
    advice = DraftEngine(ctx).advise(pick_number=13)
    assert advice.recommendation is not None
    assert advice.recommendation.position not in ("K", "DST")


def test_roster_needs_shrink_as_you_draft(drafting_league, store):
    ctx, mine = drafting_league
    engine = DraftEngine(ctx)
    assert "QB" in engine.roster_needs()

    store.execute(
        "INSERT INTO draft_picks(league_id, overall_pick, round_num, round_pick, "
        "team_id, player_id, updated_at) VALUES(?,?,?,?,?,?,?)",
        (3003, 1, 1, 1, mine, "qb1", utcnow()),
    )
    store.commit()

    engine = DraftEngine(ctx)
    assert "QB" not in engine.roster_needs(), "a drafted QB fills the QB requirement"
    assert "RB" in engine.roster_needs()


def test_drafted_players_leave_the_board(drafting_league, store):
    ctx, mine = drafting_league
    store.execute(
        "INSERT INTO draft_picks(league_id, overall_pick, round_num, round_pick, "
        "team_id, player_id, updated_at) VALUES(?,?,?,?,?,?,?)",
        (3003, 1, 1, 1, 2, "rb1", utcnow()),
    )
    store.commit()

    available = DraftEngine(ctx).available()
    assert "Elite RB One" not in {p.name for p in available}


def test_snake_pick_math_turns_at_the_end_of_each_round(drafting_league):
    ctx, _ = drafting_league
    engine = DraftEngine(ctx)

    # 12 teams, drafting from seat 3.
    picks = engine.snake_picks(slot=3, rounds=4)
    assert picks == [3, 22, 27, 46], f"snake order wrong: {picks}"

    # Seat 1 has the longest wait between picks; seat 12 the shortest.
    assert engine.snake_picks(slot=1, rounds=2) == [1, 24]
    assert engine.snake_picks(slot=12, rounds=2) == [12, 13]


def test_tier_warning_fires_when_a_cliff_will_not_survive(drafting_league, store):
    """The most useful thing a draft tool can say."""
    ctx, mine = drafting_league
    now = utcnow()
    # Tyler picks 1st; two elite RBs go before his next pick would come around.
    store.execute(
        "INSERT INTO draft_picks(league_id, overall_pick, round_num, round_pick, "
        "team_id, player_id, updated_at) VALUES(?,?,?,?,?,?,?)",
        (3003, 1, 1, 1, mine, "wr1", now),
    )
    store.execute(
        "INSERT INTO draft_picks(league_id, overall_pick, round_num, round_pick, "
        "team_id, player_id, updated_at) VALUES(?,?,?,?,?,?,?)",
        (3003, 2, 1, 2, 2, "rb1", now),
    )
    store.commit()

    advice = DraftEngine(ctx).advise()
    assert advice.picks_until_next, "should know when the next pick comes"
    warnings = " ".join(advice.tier_warnings)
    assert "RB" in warnings, (
        f"with two elite RBs left and a long wait, RB scarcity must be flagged. "
        f"Got: {advice.tier_warnings}"
    )


def test_strategy_reads_the_league_not_a_generic_template(drafting_league):
    ctx, _ = drafting_league
    strategy = DraftEngine(ctx).strategy()

    assert strategy["status"] == "ok"
    assert strategy["teams"] == 12
    assert strategy["priority_order"][0] == "RB", (
        "in this fixture RB has the steepest early value, so it should lead"
    )
    assert strategy["positional"]["RB"]["elite_tier_size"] == 3


def test_superflex_is_detected_and_changes_the_advice(store, registry):
    """A superflex league wants two QBs. Generic advice gets this badly wrong."""
    builder = LeagueBuilder(store, 4004, "L1", "Superflex")
    builder.create(week=1, roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1,
                                         "SUPERFLEX": 1, "K": 1, "DST": 1})
    builder.team("Tyler", mine=True)
    for i in range(11):
        builder.team(f"Team {i + 2}")

    now = utcnow()
    for i in range(1, 30):
        add_player(registry, f"sf_qb{i}", f"QB {i}", "QB", "SF")
        store.execute(
            "INSERT INTO draft_rankings(league_id, player_id, adp, draft_rank, "
            "projected, updated_at) VALUES(?,?,?,?,?,?)",
            (4004, f"sf_qb{i}", 10.0 + i * 3, i, 400.0 - i * 8.0, now),
        )
    for i in range(1, 40):
        add_player(registry, f"sf_rb{i}", f"RB {i}", "RB", "SF")
        store.execute(
            "INSERT INTO draft_rankings(league_id, player_id, adp, draft_rank, "
            "projected, updated_at) VALUES(?,?,?,?,?,?)",
            (4004, f"sf_rb{i}", 5.0 + i * 3, i, 300.0 - i * 5.0, now),
        )
    store.commit()

    strategy = DraftEngine(builder.ctx).strategy()
    assert any("superflex" in note.lower() for note in strategy["notes"]), (
        f"superflex must be called out explicitly, got {strategy['notes']}"
    )


def test_missing_rankings_reports_unavailable_rather_than_an_empty_board(store, registry):
    builder = LeagueBuilder(store, 5005, "L1", "Empty")
    builder.create(week=1)
    builder.team("Tyler", mine=True)

    advice = DraftEngine(builder.ctx).advise()
    assert advice.recommendation is None
    assert any("DATA UNAVAILABLE" in c for c in advice.caveats)


def test_draft_boards_are_isolated_between_leagues(store, registry):
    """The same player can carry a different rank in each league, and must."""
    now = utcnow()
    add_player(registry, "shared_rb", "Shared Back", "RB", "SF")

    for league_id, key, projected in ((6006, "L1", 300.0), (7007, "L2", 240.0)):
        builder = LeagueBuilder(store, league_id, key, f"League {key}")
        builder.create(week=1)
        builder.team("Tyler", mine=True)
        for i in range(11):
            builder.team(f"Team {i + 2}")
        store.execute(
            "INSERT INTO draft_rankings(league_id, player_id, adp, draft_rank, "
            "projected, updated_at) VALUES(?,?,?,?,?,?)",
            (league_id, "shared_rb", 12.0, 1, projected, now),
        )
    store.commit()

    from ff.db.context import LeagueContext

    l1 = LeagueContext(store, 6006, "L1", "League L1")
    l2 = LeagueContext(store, 7007, "L2", "League L2")

    p1 = next(p for p in DraftEngine(l1).build_board() if p.name == "Shared Back")
    p2 = next(p for p in DraftEngine(l2).build_board() if p.name == "Shared Back")

    assert p1.projected == 300.0
    assert p2.projected == 240.0, "each league keeps its own projection for the same player"


def test_unfilled_pick_slots_are_not_counted_as_made_picks(monkeypatch, store, registry):
    """ESPN pre-creates every pick slot when a draft is scheduled.

    A 12-team, 16-round draft returns 192 entries before anyone has picked, with
    playerId 0 on the unfilled ones. Counting those as completed picks makes a
    draft that has not started look finished -- which is exactly what happened
    on the first live test.
    """
    from ff.config import ESPNCredentials
    from ff.sources.base import HttpClient, Response
    from ff.sources.espn_draft import ESPNDraftSource

    scheduled_but_empty = {
        "draftDetail": {
            "drafted": False,
            "inProgress": False,
            "picks": [
                {
                    "overallPickNumber": n,
                    "roundId": (n - 1) // 12 + 1,
                    "roundPickNumber": (n - 1) % 12 + 1,
                    "teamId": (n - 1) % 12 + 1,
                    # ESPN's placeholder for "nobody has picked here yet"
                    "playerId": 0 if n > 2 else 3117251,
                }
                for n in range(1, 193)
            ],
        }
    }

    http = HttpClient(store.path.parent / "cache")
    monkeypatch.setattr(
        http, "get_json",
        lambda *a, **k: Response(scheduled_but_empty, from_cache=False, fetched_at=0.0),
    )
    source = ESPNDraftSource(http, store, registry, ESPNCredentials())

    picks = source.fetch_picks(1, 2026)
    assert len(picks) == 2, (
        f"only the 2 real picks should count, not all 192 slots (got {len(picks)})"
    )
    assert all(p.espn_player_id > 0 for p in picks)


def test_draft_order_is_readable_before_the_draft_starts(monkeypatch, store, registry):
    """The slot has to come from the published order -- there are no picks yet."""
    from ff.config import ESPNCredentials
    from ff.sources.base import HttpClient, Response
    from ff.sources.espn_draft import ESPNDraftSource

    payload = {
        "draftDetail": {
            "picks": [
                {"overallPickNumber": n, "roundId": 1, "roundPickNumber": n,
                 "teamId": 100 + n, "playerId": 0}
                for n in range(1, 13)
            ]
        }
    }
    http = HttpClient(store.path.parent / "cache2")
    monkeypatch.setattr(
        http, "get_json",
        lambda *a, **k: Response(payload, from_cache=False, fetched_at=0.0),
    )
    source = ESPNDraftSource(http, store, registry, ESPNCredentials())

    order = source.draft_order(1, 2026)
    assert len(order) == 12
    assert order[0] == (1, 101)
    assert order[-1] == (12, 112)
