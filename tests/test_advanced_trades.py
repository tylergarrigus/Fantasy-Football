"""Rest-of-season value, packages, buy-low, sell-high and multi-step plans."""

from __future__ import annotations

from ff.engines import trades as T

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1, "K": 1, "RB/WR/TE": 1, "BE": 7}


def pl(pid, pos, proj, avg=None, byes=(), hist=None, inj=None):
    return {"player_id": pid, "name": pid, "position": pos, "projected": proj,
            "proj_avg": avg, "bye_weeks": list(byes), "history": hist or [], "injury_status": inj}


def team(tid, players, me=False):
    return {"team_id": tid, "name": f"T{tid}", "is_me": me, "players": players}


def core(prefix, scale=1.0):
    return [pl(f"{prefix}QB", "QB", 300 * scale), pl(f"{prefix}RB1", "RB", 240 * scale), pl(f"{prefix}RB2", "RB", 200 * scale),
            pl(f"{prefix}WR1", "WR", 240 * scale), pl(f"{prefix}WR2", "WR", 200 * scale), pl(f"{prefix}TE", "TE", 150 * scale),
            pl(f"{prefix}K", "K", 120), pl(f"{prefix}D", "D/ST", 90)]


def test_games_left_skips_a_future_bye_but_not_a_past_one():
    assert T.games_left(pl("a", "RB", 100, byes=[9]), week=6) == 11
    assert T.games_left(pl("a", "RB", 100, byes=[5]), week=6) == 12


def test_ros_value_uses_per_game_projection_times_games_left():
    p = pl("a", "RB", 300, avg=20.0, byes=[9])
    assert T.ros_value(p, week=6) == 220.0
    assert T.ros_value(pl("b", "RB", 120), week=6) == 120.0  # no avg: 120/12 * 12 games


def test_with_ros_values_keeps_the_season_number():
    st = {"current_week": 6, "teams": [team(1, [pl("a", "RB", 300, avg=20.0, byes=[9])], me=True)], "free_agents": [], "roster_slots": SLOTS}
    out = T.with_ros_values(st)
    p = out["teams"][0]["players"][0]
    assert p["projected"] == 220.0 and p["season_proj"] == 300 and p["ros"] == 220.0


def test_packages_are_three_for_one_or_two_and_help_both():
    mine = core("m") + [pl("mRB3", "RB", 190), pl("mWR3", "WR", 190), pl("mWR4", "WR", 185)]
    theirs = core("t", 0.7) + [pl("tSTAR", "WR", 400)]
    ideas = T.find_packages(mine, [team(2, theirs)], SLOTS, min_my_gain=5, min_their_gain=3)
    assert ideas, "depth for a star should be findable"
    for i in ideas:
        assert len(i.send) == 3 and len(i.receive) in (1, 2)
        assert i.my_gain >= 5 and i.their_gain >= 3


def test_injured_players_are_never_offered():
    mine = core("m") + [pl("mHURT", "WR", 260, inj="OUT"), pl("mWR3", "WR", 190)]
    theirs = core("t", 0.7) + [pl("tSTAR", "WR", 400)]
    for i in T.find_trades(mine, [team(2, theirs)], SLOTS, min_my_gain=1, min_their_gain=1):
        assert all(p["player_id"] != "mHURT" for p in i.send)


def test_buy_low_targets_are_good_players_scoring_under_projection():
    cold = pl("cold", "WR", 220, avg=18.0, hist=[{"w": w, "pts": 9.0, "played": True} for w in (1, 2, 3)])
    fine = pl("fine", "WR", 220, avg=18.0, hist=[{"w": w, "pts": 18.0, "played": True} for w in (1, 2, 3)])
    new = pl("new", "WR", 220, avg=18.0, hist=[{"w": 1, "pts": 2.0, "played": True}])
    hits = T.buy_low_targets([team(2, [cold, fine, new])])
    assert [h["player_id"] for h in hits] == ["cold"]
    assert hits[0]["_gap"] == 9.0


def test_sell_high_flags_our_overperformers():
    hot = pl("hot", "RB", 150, avg=10.0, hist=[{"w": w, "pts": 16.0, "played": True} for w in (1, 2, 3)])
    meh = pl("meh", "RB", 150, avg=10.0, hist=[{"w": w, "pts": 11.0, "played": True} for w in (1, 2, 3)])
    ids = T.sell_high_ids([hot, meh])
    assert list(ids) == ["hot"] and "16.0" in ids["hot"]


def test_plans_chain_steps_that_each_stand_alone():
    """A spare QB buys a receiver from a QB-needy team, which frees a receiver to buy a back."""
    mine = core("m") + [pl("mQB2", "QB", 280), pl("mWR3", "WR", 150)]
    needs_qb = core("a", 0.8)
    needs_qb[0] = pl("aQB", "QB", 90)
    needs_qb.append(pl("aWRx", "WR", 230))
    needs_wr = core("b", 0.8)
    needs_wr[3] = pl("bWR1", "WR", 110)
    needs_wr[4] = pl("bWR2", "WR", 100)
    needs_wr.append(pl("bRBx", "RB", 300))
    plans = T.find_trade_plans(mine, [team(2, needs_qb), team(3, needs_wr)], SLOTS, min_their_gain=3, margin=1)
    for plan in plans:
        assert 2 <= len(plan.steps) <= 3
        assert all(s.their_gain >= 3 for s in plan.steps)
        assert len({s.partner_id for s in plan.steps}) == len(plan.steps)


def _named(pos, proj, adp):
    return {"player_id": f"{pos}{adp}", "position": pos, "projected": proj, "adp": adp}


def test_a_star_costs_a_star_not_a_qb_plus_filler():
    chase = _named("WR", 265, 6.1)
    allen, dowdle, wilson = _named("QB", 251, 13.2), _named("RB", 76, 136.9), _named("WR", 200, 30.0)
    assert not T._looks_fair([allen, dowdle], [chase])  # junk throw-in, no star premium
    assert not T._looks_fair([allen], [chase])
    assert T._looks_fair([allen, wilson], [chase])


def test_kickers_and_defenses_are_never_traded():
    wr = _named("WR", 200, 20.0)
    assert not T._looks_fair([wr], [_named("K", 140, 150.0)])
    assert not T._looks_fair([_named("D/ST", 90, 120.0), wr], [_named("WR", 210, 18.0)])
