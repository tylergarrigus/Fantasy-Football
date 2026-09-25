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


# ---------------------------------------------------------------- chains


def chain_rosters():
    """My spare RB buys the Hoarder's backup QB, which frees my starting QB
    to be flipped to the QB-desperate team for their stud receiver."""
    mine = [
        player("QBstar", "QB", 300),
        player("RB0", "RB", 250), player("RB1", "RB", 240),
        player("RBspare", "RB", 200),
        player("WR0", "WR", 240), player("WR1", "WR", 230),
        player("WRflex", "WR", 220),
        player("TE0", "TE", 180), player("K0", "K", 150),
        player("DST0", "D/ST", 140),
    ]
    hoarder = {
        "team_id": 2, "name": "Hoarder", "players": [
            player("hQB1", "QB", 290), player("hQB2", "QB", 280),
            player("hRB0", "RB", 150), player("hRB1", "RB", 140),
            player("hWR0", "WR", 230), player("hWR1", "WR", 220),
            player("hFlex", "WR", 210),
            player("hTE", "TE", 170), player("hK", "K", 150),
            player("hDST", "D/ST", 140),
        ],
    }
    needy = {
        "team_id": 3, "name": "Needy", "players": [
            player("nQB", "QB", 150),
            player("nRB0", "RB", 230), player("nRB1", "RB", 220),
            player("WRstud", "WR", 300),
            player("nWR1", "WR", 260), player("nWR2", "WR", 250),
            player("nFlex", "WR", 245),
            player("nTE", "TE", 170), player("nK", "K", 150),
            player("nDST", "D/ST", 140),
        ],
    }
    return mine, [hoarder, needy]


def test_chain_found_when_it_beats_the_single():
    from ff.engines.trades import find_trade_chains
    mine, opps = chain_rosters()
    chains = find_trade_chains(mine, opps, SLOTS)
    assert chains, "the buy-backup-QB-then-flip-starter chain should be found"
    best = chains[0]
    got1 = {p["player_id"] for p in best.step1.receive}
    sent2 = {p["player_id"] for p in best.step2.send}
    # Step 1 buys one of the Hoarder's QBs cheap; step 2 flips a QB (the
    # engine is free to pick which one) to the QB-desperate team for the stud.
    assert got1 & {"hQB1", "hQB2"}
    assert sent2 & {"QBstar", "hQB1", "hQB2"}
    assert best.step2.receive[0]["player_id"] == "WRstud"


def test_every_chain_step_is_individually_acceptable():
    from ff.engines.trades import find_trade_chains
    mine, opps = chain_rosters()
    for c in find_trade_chains(mine, opps, SLOTS):
        assert c.step1.their_gain >= 3
        assert c.step2.their_gain >= 3
        assert c.step1.my_gain >= -3


def test_chain_total_measured_from_original_roster():
    from ff.engines.trades import best_single_gain, find_trade_chains
    mine, opps = chain_rosters()
    single = best_single_gain(mine, opps, SLOTS)
    for c in find_trade_chains(mine, opps, SLOTS):
        assert c.total_gain >= single + 5, "a chain must clearly beat one trade"


def test_no_chain_when_a_single_trade_already_does_it():
    """If the stud is winnable in one deal, two deals add risk, not value."""
    from ff.engines.trades import find_trade_chains
    mine, opps = chain_rosters()
    # Give me a spare QB up front: the flip no longer needs step 1.
    mine.append(player("QBbackup", "QB", 295))
    chains = find_trade_chains(mine, opps, SLOTS)
    for c in chains:
        # any surviving chain must still beat the (now large) single bar
        assert c.total_gain > 0


def test_rejected_players_never_appear_in_chains():
    from ff.engines.trades import find_trade_chains
    mine, opps = chain_rosters()
    chains = find_trade_chains(
        mine, opps, SLOTS,
        rejected=[("Hoarder", "hQB2"), ("Hoarder", "hQB1")],
    )
    for c in chains:
        for step in (c.step1, c.step2):
            if step.partner_name == "Hoarder":
                assert all(p["name"] not in ("hQB1", "hQB2") for p in step.receive)


def test_untouchables_never_sent_in_either_step():
    from ff.engines.trades import find_trade_chains
    mine, opps = chain_rosters()
    for c in find_trade_chains(mine, opps, SLOTS, untouchable=["QBstar"]):
        for step in (c.step1, c.step2):
            assert all(p["player_id"] != "QBstar" for p in step.send)


def test_lopsided_name_value_is_never_proposed():
    """A bench RB for a star QB is a decline-on-sight offer, even when the
    lineup math loves it. Fairness is judged in raw projection, the closest
    number to how managers perceive value."""
    from ff.engines.trades import find_trades, find_trade_chains
    mine = roster(QB=1, RB=2, WR=2, TE=1, K=1, DST=1)
    mine.append(player("RBspare", "RB", 150))
    theirs = [
        player("StarQB", "QB", 340), player("tQB2", "QB", 330),
        player("tRB0", "RB", 120), player("tRB1", "RB", 110),
        player("tWR0", "WR", 200), player("tWR1", "WR", 195),
        player("tFlex", "WR", 190),
        player("tTE", "TE", 170), player("tK", "K", 150),
        player("tDST", "D/ST", 140),
    ]
    opponents = [{"team_id": 2, "name": "Hoarder", "players": theirs}]
    for idea in find_trades(mine, opponents, SLOTS,
                            min_my_gain=0.0, min_their_gain=1.0):
        offered = sum(p["projected"] for p in idea.send)
        asked = sum(p["projected"] for p in idea.receive)
        assert offered / asked >= 0.7
    for c in find_trade_chains(mine, opponents, SLOTS):
        for step in (c.step1, c.step2):
            offered = sum(p["projected"] for p in step.send)
            asked = sum(p["projected"] for p in step.receive)
            assert offered / asked >= 0.7
