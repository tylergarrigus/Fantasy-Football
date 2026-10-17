"""The extras layer reads espn_api objects defensively and never invents."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from ff.sources import espn_extras as ex


def fake_player(pid, stats=None, schedule=None, **kw):
    base = dict(playerId=pid, name=f"P{pid}", stats=stats or {}, schedule=schedule or {},
                total_points=50.5, avg_points=10.1, projected_total_points=180.0,
                projected_avg_points=12.0, posRank=7, percent_owned=88.2, percent_started=40.0)
    base.update(kw)
    return NS(**base)


def test_week_row_keeps_actual_projection_and_usage():
    stats = {3: {"points": 14.2, "projected_points": 11.0,
                 "breakdown": {"receivingTargets": 8, "receivingReceptions": 6, "rushingAttempts": 0}}}
    row = ex.week_row(stats, 3)
    assert row == {"w": 3, "pts": 14.2, "proj": 11.0, "played": True, "tgt": 8.0, "rec": 6.0}


def test_week_row_is_none_without_numbers():
    assert ex.week_row({3: {"breakdown": {}}}, 3) is None
    assert ex.week_row({}, 3) is None


def test_bye_weeks_are_the_gaps_in_the_schedule():
    sched = {str(w): {"team": "X"} for w in range(1, 19) if w != 6}
    assert ex.bye_weeks(NS(schedule=sched)) == [6]
    assert ex.bye_weeks(NS(schedule={})) == []


def test_player_facts_history_covers_only_played_weeks():
    p = fake_player(1, stats={1: {"points": 9.0, "breakdown": {"rushingAttempts": 12}},
                              2: {"projected_points": 8.0, "breakdown": {}},
                              0: {"points": 99}},
                    schedule={str(w): {} for w in range(1, 19) if w != 9})
    f = ex.player_facts(p, 2)
    assert [r["w"] for r in f["history"]] == [1, 2]
    assert f["history"][0]["car"] == 12.0 and f["history"][1]["played"] is False
    assert f["bye_weeks"] == [9] and f["season_pts"] == 50.5 and f["pos_rank"] == 7


def test_negative_ownership_means_unknown():
    f = ex.player_facts(fake_player(2, percent_owned=-1, percent_started=-1), 1)
    assert f["pct_owned"] is None and f["pct_started"] is None


def test_team_records_read_schedule_as_objects_or_ids():
    opp = NS(team_id=4)
    t = NS(team_id=3, wins=4, losses=1, ties=0, points_for=612.34, points_against=500.0,
           standing=2, streak_type="WIN", streak_length=3, playoff_pct=81.25,
           outcomes=["W", "W", "L", "W", "W"], scores=[101.2, 99.0, 80.4, 120.0, 111.1],
           schedule=[opp, 7, opp, 9, 10], acquisition_budget_spent=12, waiver_rank=5,
           trades=1, acquisitions=6)
    rec = ex.team_records(NS(teams=[t]))[3]
    assert rec["schedule"] == [4, 7, 4, 9, 10]
    assert rec["streak"] == "W3" and rec["playoff_pct"] == 81.2 and rec["points_for"] == 612.3


def test_live_matchups_flatten_box_scores():
    bp = NS(playerId=11, slot_position="RB", points=12.3, projected_points=14.0, game_played=100,
            pro_opponent="KC", pro_pos_rank=28, on_bye_week=False, game_date=datetime(2026, 10, 18, 13, 0))
    bye = NS(playerId=12, slot_position="BE", points=0, projected_points=0, game_played=0,
             pro_opponent="None", pro_pos_rank=0, on_bye_week=True)
    box = NS(home_team=NS(team_id=1), away_team=2, home_score=55.5, away_score=40.1,
             home_projected=120.2, away_projected=118.0, is_playoff=False,
             home_lineup=[bp, bye], away_lineup=[])
    league = NS(box_scores=lambda week: [box])
    matchups, live = ex.live_matchups(league, 6)
    assert matchups == [{"home": 1, "away": 2, "home_score": 55.5, "away_score": 40.1,
                         "home_proj": 120.2, "away_proj": 118.0, "playoff": False}]
    assert live[11]["opp"] == "KC" and live[11]["opp_rank"] == 28 and live[11]["kickoff"] == "2026-10-18T13:00"
    assert live[12]["bye"] is True and live[12]["opp"] is None and live[12]["opp_rank"] is None


def test_live_matchups_fail_soft():
    def boom(week):
        raise RuntimeError("espn down")
    assert ex.live_matchups(NS(box_scores=boom), 6) == ([], {})


def test_collect_extras_merges_cards_over_roster_objects():
    roster_p = fake_player(5, stats={1: {"points": 3.0, "breakdown": {}}})
    card_p = fake_player(5, stats={1: {"points": 3.0, "breakdown": {"receivingTargets": 4}},
                                   2: {"points": 7.5, "breakdown": {"receivingTargets": 9}}})
    calls = []

    def player_info(playerId=None):
        calls.append(list(playerId))
        return [card_p]

    league = NS(teams=[NS(team_id=1, roster=[roster_p], wins=1, losses=0, ties=0, points_for=1,
                          points_against=0, standing=1, streak_type="WIN", streak_length=1,
                          playoff_pct=50, outcomes=["W"], scores=[100.0], schedule=[2])],
                box_scores=lambda week: [],
                settings=NS(scoring_format=[{"abbr": "REC", "label": "Reception", "points": 1}]),
                recent_activity=lambda size: [],
                player_info=player_info)
    fa = NS(playerId=9, pro_opponent="DAL", pro_pos_rank=3, on_bye_week=False, points=0,
            projected_points=9.9, game_played=0, stats={}, schedule={})
    out = ex.collect_extras(league, 2, [5, 9], [fa])
    assert calls == [[5, 9]]
    assert [r["w"] for r in out["players"][5]["history"]] == [1, 2]
    assert out["players"][5]["history"][1]["tgt"] == 9.0
    assert out["live"][9]["opp"] == "DAL" and out["live"][9]["opp_rank"] == 3
    assert out["scoring_rules"] == [{"abbr": "REC", "label": "Reception", "points": 1.0}]
    assert out["records"][1]["wins"] == 1
