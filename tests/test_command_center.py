"""The page builder: one self-contained file, real data, honest placeholders."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_command_center as bcc  # noqa: E402


@pytest.fixture(scope="module")
def page() -> str:
    if not (bcc.DATA / "state_L1.json").exists():
        pytest.skip("no league data checked out")
    return bcc.build()


def test_page_embeds_both_leagues_and_no_template_holes(page):
    assert "{{" not in page
    m = re.search(r"window\.FCC=(\{.*?\});window\.FCC_MSG=", page, re.S)
    assert m, "payload missing"
    data = json.loads(m.group(1))
    assert data["order"] and all(k in data["leagues"] for k in data["order"])
    lg = data["leagues"][data["order"][0]]
    assert lg["myId"] in [t["id"] for t in lg["teams"]]
    assert lg["players"] and lg["queue"] is not None


def test_every_player_row_is_slim(page):
    m = re.search(r"window\.FCC=(\{.*?\});window\.FCC_MSG=", page, re.S)
    data = json.loads(m.group(1))
    for lg in data["leagues"].values():
        for p in lg["players"].values():
            assert set(p) >= {"id", "n", "pos", "proj", "wk", "hist", "owner"}
            for r in p["hist"]:
                assert set(r) <= set(bcc.HIST_KEYS)


def test_group_chat_messages_are_keyed_by_action(page):
    m = re.search(r"window\.FCC_MSG=(\{.*?\});</script>", page, re.S)
    msgs = json.loads(m.group(1))
    for k, v in msgs.items():
        assert k.count("|") == 2 and v
