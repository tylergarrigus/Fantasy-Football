"""Shared fixtures.

Everything here is deterministic and offline. No network, no ESPN, no API keys
-- the whole system is testable without credentials, which is the point.
"""

from __future__ import annotations

import pytest

from ff.db.context import LeagueContext
from ff.db.store import Store, utcnow
from ff.decide.engine import DecisionEngine
from ff.engines.lineup import LineupEngine
from ff.engines.opponent import OpponentEngine
from ff.engines.roster import RosterEngine
from ff.engines.simulate import Simulator
from ff.engines.trade import TradeEngine
from ff.engines.valuation import ValuationEngine
from ff.engines.waiver import WaiverEngine
from ff.identity import PlayerRegistry
from ff.intel.news import NewsEngine


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture()
def registry(store):
    return PlayerRegistry(store)


@pytest.fixture()
def news(store, registry):
    return NewsEngine(store, registry)


class LeagueBuilder:
    """Builds a realistic league in the DB without touching ESPN."""

    def __init__(self, store: Store, league_id: int, key: str, name: str):
        self.store = store
        self.ctx = LeagueContext(store, league_id, key, name)
        self.league_id = league_id
        self.now = utcnow()
        self._next_team = 1

    def create(
        self,
        *,
        season: int = 2026,
        week: int = 5,
        waiver_type: str = "faab",
        faab_budget: int = 100,
        playoff_teams: int = 6,
        reg_weeks: int = 14,
        roster_slots: dict | None = None,
    ) -> "LeagueBuilder":
        import json

        slots = roster_slots or {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
        self.store.execute(
            "INSERT INTO leagues(league_id, league_key, name, season, waiver_type, "
            "faab_budget, playoff_teams, reg_season_weeks, current_week, roster_json, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.league_id, self.ctx.league_key, self.ctx.name, season, waiver_type,
                faab_budget, playoff_teams, reg_weeks, week, json.dumps(slots), self.now,
            ),
        )
        self.store.commit()
        return self

    def team(
        self, name: str, *, mine: bool = False, wins: int = 3, losses: int = 1,
        faab_remaining: int = 80, points_for: float = 520.0,
    ) -> int:
        team_id = self._next_team
        self._next_team += 1
        self.store.execute(
            "INSERT INTO league_teams(league_id, team_id, name, wins, losses, points_for, "
            "points_against, standing, faab_remaining, is_mine, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.league_id, team_id, name, wins, losses, points_for, 500.0,
                team_id, faab_remaining, 1 if mine else 0, self.now,
            ),
        )
        if mine:
            self.store.execute(
                "UPDATE leagues SET my_team_id = ? WHERE league_id = ?",
                (team_id, self.league_id),
            )
        self.store.commit()
        return team_id

    def roster(self, team_id: int, player_id: str, slot: str = "BE", projection: float = 10.0):
        self.store.execute(
            "INSERT INTO league_rosters(league_id, team_id, player_id, slot, updated_at) "
            "VALUES(?,?,?,?,?)",
            (self.league_id, team_id, player_id, slot, self.now),
        )
        self._project(player_id, projection)
        return self

    def free_agent(self, player_id: str, projection: float = 8.0, pct_owned: float = 15.0):
        self.store.execute(
            "INSERT INTO league_free_agents(league_id, player_id, pct_owned, updated_at) "
            "VALUES(?,?,?,?)",
            (self.league_id, player_id, pct_owned, self.now),
        )
        self._project(player_id, projection)
        return self

    def _project(self, player_id: str, projection: float):
        week = self.ctx.current_week()
        self.store.execute(
            "INSERT INTO league_projections(league_id, player_id, week, projected, "
            "season_avg, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(league_id, player_id, week) DO UPDATE SET projected = excluded.projected",
            (self.league_id, player_id, week, projection, projection, self.now),
        )
        self.store.commit()

    def matchup(self, week: int, team_id: int, opponent_id: int):
        for a, b in ((team_id, opponent_id), (opponent_id, team_id)):
            self.store.execute(
                "INSERT INTO league_matchups(league_id, week, team_id, opponent_id, "
                "completed, updated_at) VALUES(?,?,?,?,0,?) "
                "ON CONFLICT(league_id, week, team_id) DO NOTHING",
                (self.league_id, week, a, b, self.now),
            )
        self.store.commit()
        return self


def add_player(
    registry: PlayerRegistry, player_id: str, name: str, position: str, team: str = "SF"
) -> str:
    registry.upsert_player(player_id, name, position=position, nfl_team=team)
    registry.store.commit()
    return player_id


def build_engine(ctx: LeagueContext, registry: PlayerRegistry, news: NewsEngine,
                 iterations: int = 400) -> DecisionEngine:
    """Full engine stack for one league. Low iteration count keeps tests fast."""
    valuation = ValuationEngine(ctx, registry, news)
    simulator = Simulator(ctx, valuation, seed=42, iterations=iterations)
    roster = RosterEngine(ctx, valuation)
    lineup = LineupEngine(ctx, valuation, simulator)
    waiver = WaiverEngine(ctx, valuation, roster, registry)
    opponent = OpponentEngine(ctx, valuation, roster)
    trade = TradeEngine(ctx, valuation, roster, opponent, simulator)
    return DecisionEngine(
        ctx, valuation=valuation, roster=roster, lineup=lineup, waiver=waiver,
        trade=trade, simulator=simulator, news=news, min_delta=0.0,
    )


@pytest.fixture()
def divergent_leagues(store, registry, news):
    """The canonical scenario this whole system exists to handle.

    One NFL event (McCaffrey ruled out). Two leagues:
      League 1 -- Tyler starts McCaffrey and already rosters Jordan Mason.
      League 2 -- Tyler starts McCaffrey and Mason is a free agent.

    Correct behaviour is START in L1 and CLAIM in L2. Identical news,
    different answers, because availability differs.
    """
    add_player(registry, "p_cmc", "Christian McCaffrey", "RB", "SF")
    add_player(registry, "p_mason", "Jordan Mason", "RB", "SF")
    add_player(registry, "p_qb", "Brock Purdy", "QB", "SF")
    add_player(registry, "p_wr1", "Deebo Samuel", "WR", "SF")
    add_player(registry, "p_wr2", "Jauan Jennings", "WR", "SF")
    add_player(registry, "p_te", "George Kittle", "TE", "SF")
    add_player(registry, "p_k", "Jake Moody", "K", "SF")
    add_player(registry, "p_dst", "49ers DST", "DST", "SF")
    add_player(registry, "p_rb2", "Isaac Guerendo", "RB", "SF")
    add_player(registry, "p_scrub", "Deep Bench Guy", "RB", "CHI")
    add_player(registry, "p_fa_rb", "Waiver RB", "RB", "NYJ")

    def populate(builder: LeagueBuilder, mason_is_free_agent: bool):
        builder.create()
        mine = builder.team("Tyler", mine=True)
        rival = builder.team("Rival")
        builder.matchup(5, mine, rival)

        builder.roster(mine, "p_cmc", "RB", projection=18.0)
        builder.roster(mine, "p_rb2", "RB", projection=7.0)
        builder.roster(mine, "p_qb", "QB", projection=19.0)
        builder.roster(mine, "p_wr1", "WR", projection=14.0)
        builder.roster(mine, "p_wr2", "WR", projection=11.0)
        builder.roster(mine, "p_te", "TE", projection=12.0)
        builder.roster(mine, "p_k", "K", projection=8.0)
        builder.roster(mine, "p_dst", "DST", projection=7.0)
        builder.roster(mine, "p_scrub", "BE", projection=2.0)

        # The one difference between the two leagues.
        if mason_is_free_agent:
            builder.free_agent("p_mason", projection=13.5, pct_owned=8.0)
        else:
            builder.roster(mine, "p_mason", "BE", projection=13.5)

        builder.free_agent("p_fa_rb", projection=5.0, pct_owned=3.0)

        # Give the rival a full lineup so simulations have something to run.
        for idx, (pid, pos, proj) in enumerate(
            [("r_qb", "QB", 17.0), ("r_rb1", "RB", 12.0), ("r_rb2", "RB", 10.0),
             ("r_wr1", "WR", 13.0), ("r_wr2", "WR", 9.0), ("r_te", "TE", 8.0),
             ("r_k", "K", 8.0), ("r_dst", "DST", 6.0)]
        ):
            unique = f"{pid}_{builder.league_id}"
            add_player(registry, unique, f"Rival {pos} {idx}", pos, "DAL")
            builder.roster(rival, unique, pos, projection=proj)

    l1 = LeagueBuilder(store, 1001, "L1", "Alpha League")
    populate(l1, mason_is_free_agent=False)

    l2 = LeagueBuilder(store, 2002, "L2", "Beta League")
    populate(l2, mason_is_free_agent=True)

    return {"L1": l1.ctx, "L2": l2.ctx}


@pytest.fixture()
def cmc_ruled_out(store, registry, news):
    """The single NFL event, recorded once, shared by both leagues."""
    from ff.intel.news import NFLEvent

    event = NFLEvent(
        kind="injury_status",
        player_id="p_cmc",
        nfl_team="SF",
        headline="Christian McCaffrey ruled OUT for Sunday",
        old_value="Questionable",
        new_value="Out",
        severity="major",
        source="espn_injury_report",
        verified=True,
    )
    news.record([event])
    return dict(event.__dict__, event_id=event.event_id, fingerprint=event.fingerprint)
