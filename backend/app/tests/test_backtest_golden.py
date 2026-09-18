"""
Golden-output regression test for app.backtest_engine -- the safety net for
the upcoming speed-optimization work.

The whole point of that work is to change SPEED and nothing else. This test
locks in that a full backtest run over a fixed set of already-cached real
markets produces byte-identical trades/summary before and after. If this
test's output ever changes, either a refactor broke something, or the
fixture needs deliberately regenerating with a clear explanation of what
legitimately changed (see the golden-fixture generation notes in
app/fixtures/backtest_golden_output.json's sibling condition_ids list).

Unlike the rest of app/tests (synthetic in-memory SQLite, fast, hermetic),
this one runs against the REAL dashboard.db and real already-cached data --
no network calls happen (everything needed is already synced), but it does
need that real data present, and it's slow (a real per-checkpoint backtest
over 12 markets, ~30-90s depending on machine and how far the speed work
has progressed). Run explicitly during the refactor:

    pytest app/tests/test_backtest_golden.py -q

Skipped automatically (not part of the default `pytest app/tests/` sweep in
spirit, though not excluded by config either -- there's no pytest.ini in
this project) whenever the fixture or its cached data isn't present, e.g. a
fresh checkout or CI with an empty dashboard.db.
"""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.backtest_engine import _run, get_run

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "backtest_golden_output.json"


def _load_fixture():
    if not FIXTURE_PATH.exists():
        return None
    return json.loads(FIXTURE_PATH.read_text())


def _data_is_cached(condition_ids):
    try:
        with db.db_session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM backtest_market_meta WHERE condition_id IN ({}) "
                "AND trades_synced_at IS NOT NULL AND prices_synced_at IS NOT NULL".format(
                    ",".join("?" * len(condition_ids))
                ),
                condition_ids,
            ).fetchone()
    except Exception:
        return False
    return row["n"] == len(condition_ids)


def _normalize_trades(trades):
    out = []
    for t in trades:
        t = dict(t)
        for k in ("id", "run_id"):
            t.pop(k, None)
        out.append(t)
    out.sort(key=lambda t: (t["condition_id"], t["signal_ts"]))
    return out


_fixture = _load_fixture()
pytestmark = pytest.mark.skipif(
    _fixture is None or not _data_is_cached(_fixture["condition_ids"]),
    reason="golden fixture data not cached in this environment's dashboard.db -- "
           "run the backtest once against app/fixtures' condition_ids to populate it, "
           "or regenerate the fixture (see this file's docstring).",
)


def _run_and_compare(expected):
    run_id = uuid.uuid4().hex
    params = dict(expected["params"])
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO backtest_runs (id, created_at, status, params) VALUES (?,?,?,?)",
            (run_id, 0, "pending", json.dumps(params)),
        )
    _run(run_id, params)  # synchronous: blocks until the whole run (all markets) finishes
    result = get_run(run_id)
    assert result["status"] == "done", result.get("error")
    assert _normalize_trades(result["trades"]) == expected["trades"], (
        "Trade-level output changed -- a speed refactor must never change WHICH trades "
        "are taken, at what price, or their outcome. Compare against app/fixtures/"
        "backtest_golden_output.json to see exactly what differs."
    )
    assert result["summary"] == expected["summary"]


def test_smart_money_matches_golden_output():
    _run_and_compare(_fixture["smart_money"])


def test_insider_matches_golden_output():
    _run_and_compare(_fixture["insider"])
