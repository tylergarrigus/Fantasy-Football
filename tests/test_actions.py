"""The action queue: short, honest, and empty when nothing matters."""

from __future__ import annotations

from ff.engines.actions import build_queue

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1, "K": 1, "RB/WR/TE": 1, "BE": 7}


def player(pid, pos, proj, injury=None, espn_id=None):
    return {
        "player_id": pid, "name": pid, "position": pos,
        "projected": proj, "injury_status": injury, "espn_id": espn_id,
    }


def full_roster(prefix=""):
    return [
        player(f"{prefix}QB0", "QB", 300),
        player(f"{prefix}RB0", "RB", 250), player(f"{prefix}RB1", "RB", 240),
        player(f"{prefix}WR0", "WR", 250), player(f"{prefix}WR1", "WR", 240),
        player(f"{prefix}WR2", "WR", 200),
        player(f"{prefix}TE0", "TE", 180),
        player(f"{prefix}K0", "K", 150), player(f"{prefix}DST0", "D/ST", 130),
    ]


def state(my_players, opp_players=None, free_agents=None, waiver_type="rolling"):
    teams = [
        {"team_id": 1, "name": "Me", "is_me": True, "players": my_players},
        {"team_id": 2, "name": "Them", "is_me": False,
         "players": opp_players or full_roster("t")},
    ]
    return {
        "teams": teams,
        "roster_slots": SLOTS,
        "free_agents": free_agents or [],
        "waiver_type": waiver_type,
    }


def test_quiet_league_says_no_action_needed():
    q = build_queue(state(full_roster()), [])
    assert len(q) == 1
    assert q[0].title == "No action needed"


def test_undrafted_league_is_an_info_state_not_an_error():
    s = state(full_roster())
    s["teams"][0]["players"] = []
    q = build_queue(s, [])
    assert q[0].kind == "info"
    assert "drafted" in q[0].why


def test_out_starter_becomes_an_urgent_action():
    mine = full_roster()
    mine[1] = player("RB0", "RB", 250, injury="OUT", espn_id="123")
    q = build_queue(state(mine), [])
    inj = [a for a in q if a.kind == "injury"]
    assert len(inj) == 1
    assert inj[0].urgency == 2
    assert q[0] is inj[0], "an OUT starter outranks everything else"


def test_injury_action_cites_the_story_when_news_names_him():
    mine = full_roster()
    mine[1] = player("RB0", "RB", 250, injury="OUT", espn_id="123")
    news = [{"headline": "RB0 to miss opener", "espn_athlete_ids": ["123"]}]
    q = build_queue(state(mine), news)
    inj = next(a for a in q if a.kind == "injury")
    assert "RB0 to miss opener" in inj.why


def test_questionable_is_not_an_action():
    """Half the league is Questionable in September; it is not actionable."""
    mine = full_roster()
    mine[1] = player("RB0", "RB", 250, injury="Questionable")
    q = build_queue(state(mine), [])
    assert not [a for a in q if a.kind == "injury"]


def test_waiver_add_names_the_drop():
    fa = [player("StudFA", "WR", 260)]
    q = build_queue(state(full_roster(), free_agents=fa), [])
    w = next(a for a in q if a.kind == "waiver")
    assert "Add StudFA" in w.title and "drop" in w.title


def test_bench_only_free_agent_is_not_an_action():
    fa = [player("ScrubFA", "WR", 100)]
    q = build_queue(state(full_roster(), free_agents=fa), [])
    assert not [a for a in q if a.kind == "waiver"]


def test_faab_league_gets_a_bid_note():
    fa = [player("StudFA", "WR", 260)]
    q = build_queue(state(full_roster(), free_agents=fa, waiver_type="faab"), [])
    w = next(a for a in q if a.kind == "waiver")
    assert "FAAB" in w.why


def test_trade_action_carries_a_sendable_message():
    mine = full_roster() + [player("QBspare", "QB", 290)]
    theirs = [
        player("tQB", "QB", 120),
        player("tRB0", "RB", 240), player("tRB1", "RB", 235),
        player("tWR0", "WR", 250), player("tWR1", "WR", 240),
        player("tWRstud", "WR", 230),
        player("tTE", "TE", 180), player("tK", "K", 150),
        player("tDST", "D/ST", 140), player("tRBx", "RB", 232),
    ]
    q = build_queue(state(mine, opp_players=theirs), [])
    trades = [a for a in q if a.kind == "trade"]
    assert trades
    assert trades[0].draft_message and "straight swap" in trades[0].draft_message
    assert trades[0].trade["send"]


def test_untouchables_never_appear_in_offers():
    mine = full_roster() + [player("QBspare", "QB", 290)]
    theirs = [
        player("tQB", "QB", 120),
        player("tRB0", "RB", 240), player("tRB1", "RB", 235),
        player("tWR0", "WR", 250), player("tWR1", "WR", 240),
        player("tWRstud", "WR", 230),
        player("tTE", "TE", 180), player("tK", "K", 150),
        player("tDST", "D/ST", 140), player("tRBx", "RB", 232),
    ]
    q = build_queue(state(mine, opp_players=theirs), [], untouchable=["QBspare"])
    for a in q:
        if a.kind == "trade":
            assert all(p["name"] != "QBspare" for p in a.trade["send"])


# ---------------------------------------------------------------- weekly lineup


def weekly(pid, pos, season, week, slot, bye=False, injury=None):
    return {
        "player_id": pid, "name": pid, "position": pos, "projected": season,
        "week_projected": 0.0 if bye else week, "on_bye": bye,
        "opponent": "BYE" if bye else "OPP", "current_slot": slot,
        "injury_status": injury, "espn_id": None,
    }


def weekly_roster(**overrides):
    base = {
        "QB0": weekly("QB0", "QB", 300, 20, "QB"),
        "RB0": weekly("RB0", "RB", 320, 22, "RB"),
        "RB1": weekly("RB1", "RB", 240, 15, "RB"),
        "WR0": weekly("WR0", "WR", 250, 16, "WR"),
        "WR1": weekly("WR1", "WR", 240, 15, "WR"),
        "WR2": weekly("WR2", "WR", 200, 12, "RB/WR/TE"),
        "TE0": weekly("TE0", "TE", 180, 10, "TE"),
        "K0": weekly("K0", "K", 150, 8, "K"),
        "DST0": weekly("DST0", "D/ST", 130, 7, "D/ST"),
        "RBb": weekly("RBb", "RB", 150, 9, "BE"),
    }
    base.update(overrides)
    return list(base.values())


def test_starter_on_bye_is_an_urgent_lineup_fix():
    """The season star on bye scores zero; season averages must not hide it."""
    mine = weekly_roster(RB0=weekly("RB0", "RB", 320, 22, "RB", bye=True))
    q = build_queue(state(mine), [])
    fix = next(a for a in q if a.kind == "lineup")
    assert "bench RB0" in fix.title and "RBb" in fix.title
    assert "on bye" in fix.why
    assert fix.urgency == 2
    assert q[0] is fix


def test_bye_star_is_never_recommended_into_the_lineup():
    mine = weekly_roster(RBb=weekly("RBb", "RB", 400, 0, "BE", bye=True))
    q = build_queue(state(mine), [])
    assert not any("Start RBb" in a.title for a in q if a.kind == "lineup")


def test_weekly_projection_beats_season_average():
    """Bench guy with a lower season number but a better week gets the start."""
    mine = weekly_roster(RBb=weekly("RBb", "RB", 150, 19, "BE"))
    q = build_queue(state(mine), [])
    fix = next(a for a in q if a.kind == "lineup")
    assert fix.title == "Start RBb, bench WR2"


def test_out_starter_produces_one_card_not_two():
    mine = weekly_roster(WR0=weekly("WR0", "WR", 250, 16, "WR", injury="OUT"))
    q = build_queue(state(mine), [])
    mentions = [a for a in q if "WR0" in a.players]
    assert len(mentions) == 1
