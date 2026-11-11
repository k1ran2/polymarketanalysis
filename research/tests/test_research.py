"""Guards for the rules that make results trustworthy: no lookahead, costs
always charged, wallet records built only from results already known."""
import numpy as np
import pandas as pd
import pytest

from research import engine, panel, store, wallets

DAY = 86400


def _fake_store(monkeypatch, n_markets=400, seed=0, fair=True):
    """Markets whose true win chance equals their (constant) price: no strategy can have an edge."""
    rng = np.random.default_rng(seed)
    rows, prices = [], []
    for i in range(n_markets):
        p = rng.uniform(0.1, 0.9)
        payout = float(rng.random() < p) if fair else 1.0
        start = 1_700_000_000 + i * 1000
        resolved = start + 10 * DAY
        cid = f"0x{i:064x}"
        rows.append(dict(condition_id=cid, event_id=str(i), category="Politics", neg_risk=False, start_ts=start,
                         end_ts=resolved, resolved_ts=resolved, closed_ts=resolved, payout0=payout,
                         outcome0="Yes", split="train", in_universe=True, price_tried=True, volume=1e6))
        for k in range(20):
            prices.append(dict(condition_id=cid, ts=start + k * 43200, p0=p))
    m, pr = pd.DataFrame(rows), pd.DataFrame(prices)
    monkeypatch.setattr(store, "markets", lambda: m)
    monkeypatch.setattr(store, "prices", lambda: pr)
    panel.build.cache_clear()
    return m, pr


def test_fill_must_complete_before_outcome_known(monkeypatch):
    _fake_store(monkeypatch, n_markets=3)
    df = panel.build()
    fill_end = df["ts"] + 2 * 43200
    assert not df.loc[fill_end > df["resolved_ts"], "tradable"].any()
    assert df["tradable"].any()
    # the last observation of a market has no later price to fill at
    assert not df.groupby("condition_id")["tradable"].last().any()


def test_decision_never_fills_at_its_own_price(monkeypatch):
    m, pr = _fake_store(monkeypatch, n_markets=1)
    pr.loc[:, "p0"] = np.linspace(0.2, 0.6, len(pr))
    panel.build.cache_clear()
    df = panel.build()
    assert (df["fill_p0"].dropna() > df.loc[df["fill_p0"].notna(), "p0"]).all()


def test_fair_market_loses_about_the_costs(monkeypatch):
    _fake_store(monkeypatch, n_markets=3000, seed=1)
    df = panel.build()
    sig = pd.Series(0, index=df.index)
    free = engine.summarise(engine.run(sig, df, half_spread=0.0, fees=False))
    paid = engine.summarise(engine.run(sig, df, half_spread=0.02, fees=True))
    assert free["roi_lo"] < 0 < free["roi_hi"]          # no edge when trading is free
    assert paid["roi_pct"] < free["roi_pct"] - 2         # costs always bite
    assert not paid["significant"]


def test_fee_formula_matches_published_table(monkeypatch):
    # Politics: 100 shares at $0.50 -> $1.00 fee (docs.polymarket.com/trading/fees)
    df = pd.DataFrame({"fill_p0": [0.5], "payout0": [1.0], "fee_rate": [0.04]})
    r = panel.trade_returns(df, np.array([0]), half_spread=0.0, fees=True)
    assert r["cost"].iloc[0] == pytest.approx(0.5 + 0.01)


def test_one_position_per_market(monkeypatch):
    _fake_store(monkeypatch, n_markets=50)
    df = panel.build()
    t = engine.run(pd.Series(1, index=df.index), df)
    assert t["condition_id"].is_unique


def _fake_trades(monkeypatch):
    m = pd.DataFrame([
        dict(condition_id="A", payout0=1.0, resolved_ts=1000.0, event_id="a", category="Politics", split="train"),
        dict(condition_id="B", payout0=0.0, resolved_ts=5000.0, event_id="b", category="Politics", split="train"),
    ])
    t = pd.DataFrame([
        dict(condition_id="A", wallet="w1", side="BUY", outcome_index=0, price=0.30, size=100.0, ts=100, tx="1"),
        dict(condition_id="A", wallet="w1", side="SELL", outcome_index=0, price=0.60, size=100.0, ts=200, tx="2"),
        dict(condition_id="B", wallet="w1", side="BUY", outcome_index=0, price=0.50, size=1000.0, ts=1500, tx="3"),
        dict(condition_id="B", wallet="w2", side="BUY", outcome_index=1, price=0.50, size=10.0, ts=1600, tx="4"),
    ])
    monkeypatch.setattr(store, "markets", lambda: m)
    monkeypatch.setattr(store, "trades", lambda: t)
    monkeypatch.setattr(store, "trades_done", lambda: {"A": {"truncated": False}, "B": {"truncated": False}})
    for f in (wallets.fills, wallets.wallet_market, wallets.skill_asof, wallets._market_tape):
        f.cache_clear()


def test_round_trip_profit_is_exact(monkeypatch):
    _fake_trades(monkeypatch)
    wm = wallets.wallet_market().set_index(["wallet", "condition_id"])
    assert wm.loc[("w1", "A"), "pnl"] == pytest.approx(30.0)       # bought 0.30, sold 0.60, 100 shares
    assert wm.loc[("w1", "B"), "pnl"] == pytest.approx(-500.0)     # bought the loser


def test_sizing_drives_portfolio_return(monkeypatch):
    _fake_trades(monkeypatch)
    r = wallets.record(wallets.wallet_market())
    # one small win, one large loss: 50% of markets won, but the portfolio lost money
    assert r.loc["w1", "win_rate"] == pytest.approx(0.5)
    assert r.loc["w1", "roi"] < 0


def test_wallet_record_only_counts_results_already_known(monkeypatch):
    _fake_trades(monkeypatch)
    sig = wallets.signals(min_cash=1, lo=0.01, hi=0.99)
    b = sig[(sig["wallet"] == "w1") & (sig["condition_id"] == "B")].iloc[0]
    assert b["c_n"] == 1 and b["c_pnl"] == pytest.approx(30.0)    # market A known at ts 1000, B not yet
    a = sig[(sig["wallet"] == "w1") & (sig["condition_id"] == "A")].iloc[0]
    assert a["c_n"] == 0                                           # nothing was known when A was bought
