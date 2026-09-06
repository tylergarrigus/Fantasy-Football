"""Lineup construction and trade discovery."""

from __future__ import annotations

from ff.engines.trades import best_lineup, find_trades, lineup_value

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1, "K": 1, "RB/WR/TE": 1, "BE": 7}


def player(pid: str, pos: str, proj: float | None) -> dict:
    return {"player_id": pid, "name": pid, "position": pos, "projected": proj}


def roster(**counts: int) -> list[dict]:
    """A roster of `counts` players per position, descending in projection."""
    out = []
    for pos, n in counts.items():
        pos = pos.replace("DST", "D/ST")
        for i in range(n):
            out.append(player(f"{pos}{i}", pos, 200 - i * 10))
    return out


def test_defense_slot_is_filled():
    """ESPN spells it D/ST; a mismatch here silently benches every defense."""
    lineup = best_lineup(roster(QB=1, RB=3, WR=2, TE=1, K=1, DST=1), SLOTS)
    slots = {s["slot"] for s in lineup.starters}
    assert "D/ST" in slots
    assert lineup.unfilled == []


def test_flex_takes_the_best_leftover():
    players = roster(QB=1, RB=3, WR=2, TE=1, K=1, DST=1)
    lineup = best_lineup(players, SLOTS)
    flex = [s for s in lineup.starters if s["slot"] == "FLEX"]
    assert len(flex) == 1
    # RB0/RB1 fill the two RB slots, so the flex is the third back.
    assert flex[0]["player_id"] == "RB2"


def test_unfilled_slots_are_reported():
    lineup = best_lineup(roster(QB=1, RB=2, WR=2, TE=1, K=1), SLOTS)
    assert "D/ST" in lineup.unfilled


def test_players_without_a_projection_are_left_out():
    players = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    players.append(player("ghost", "RB", None))
    lineup = best_lineup(players, SLOTS)
    assert all(s["player_id"] != "ghost" for s in lineup.starters)
    assert all(p["player_id"] != "ghost" for p in lineup.bench)


def test_a_trade_must_help_both_sides():
    """The classic mutual trade: my spare QB for their buried WR.

    My second QB starts for nobody (one QB slot, QBs don't flex) and their
    third WR is stuck behind two better ones. Each is dead weight at home and
    a starter on the other roster.
    """
    mine = [
        player("QBa", "QB", 200), player("QBb", "QB", 190),
        player("RB0", "RB", 200), player("RB1", "RB", 195),
        player("WR0", "WR", 200), player("WRweak", "WR", 100),
        player("TE0", "TE", 180), player("K0", "K", 150),
        player("DST0", "D/ST", 140), player("RBflex", "RB", 170),
    ]
    theirs = [
        player("QBx", "QB", 120),
        player("tRB0", "RB", 240), player("tRB1", "RB", 235),
        player("tRBx", "RB", 232),
        player("tWR0", "WR", 250), player("tWR1", "WR", 240),
        player("WRstud", "WR", 230),
        player("tTE", "TE", 180), player("tK", "K", 150),
        player("tDST", "D/ST", 140),
    ]
    opponents = [{"team_id": 2, "name": "Them", "players": theirs}]

    ideas = find_trades(mine, opponents, SLOTS, min_my_gain=1.0, min_their_gain=1.0)
    assert ideas, "an obvious mutual upgrade should be found"
    assert any(
        [p["player_id"] for p in i.send] == ["QBb"]
        and i.receive[0]["player_id"] == "WRstud"
        for i in ideas
    )
    for idea in ideas:
        assert idea.my_gain > 0 and idea.their_gain > 0


def test_lopsided_offers_are_rejected():
    """If the other side gains nothing, the idea is dropped."""
    mine = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    theirs = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    theirs.append(player("WRstud", "WR", 400))
    opponents = [{"team_id": 2, "name": "Them", "players": theirs}]

    ideas = find_trades(mine, opponents, SLOTS, min_my_gain=1.0, min_their_gain=1.0)
    assert all(i.their_gain > 0 for i in ideas)


def test_untouchable_players_are_never_sent():
    mine = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    mine.append(player("RBspare", "RB", 190))
    theirs = roster(QB=1, RB=1, WR=2, TE=1, K=1, DST=1)
    theirs.append(player("WRstud", "WR", 260))
    opponents = [{"team_id": 2, "name": "Them", "players": theirs}]

    ideas = find_trades(
        mine, opponents, SLOTS,
        min_my_gain=1.0, min_their_gain=1.0,
        untouchable=["RBspare"],
    )
    for idea in ideas:
        assert all(p["player_id"] != "RBspare" for p in idea.send)


def test_value_counts_starters_only():
    """A bench player adds nothing, which is the whole reason to trade one."""
    base = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    with_scrub = base + [player("scrub", "K", 5)]
    assert lineup_value(base, SLOTS) == lineup_value(with_scrub, SLOTS)


def test_one_idea_per_target_player():
    mine = roster(QB=1, RB=3, WR=2, TE=1, K=1, DST=1)
    theirs = roster(QB=1, RB=1, WR=2, TE=1, K=1, DST=1)
    theirs.append(player("WRstud", "WR", 300))
    opponents = [{"team_id": 2, "name": "Them", "players": theirs}]

    ideas = find_trades(mine, opponents, SLOTS, min_my_gain=1.0, min_their_gain=1.0)
    targets = [i.receive[0]["player_id"] for i in ideas]
    assert len(targets) == len(set(targets))
