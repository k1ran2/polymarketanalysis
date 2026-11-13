"""Run the standard analyses and write the JSON the interface reads.

    python -m research.publish            # everything
    python -m research.publish --skip-traders
"""
from __future__ import annotations

import argparse
import json
import time
import warnings

import numpy as np
import pandas as pd

from . import engine, panel, scan, store, strategies as st, verify, wallets
from .server import equity_curve

warnings.filterwarnings("ignore")


def _write(name: str, obj):
    store.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (store.RESULTS_DIR / f"{name}.json").write_text(json.dumps(obj, default=float))


# ---- calibration -------------------------------------------------------

def calibration() -> dict:
    df = panel.build()
    d = df[df["tradable"] & (df["split"] != "test")]
    s = scan._two_sided(d)
    s["win"] = np.where(s["side"] == 0, s["payout0"], 1 - s["payout0"])
    s["px"] = np.where(s["side"] == 0, s["fill_p0"], 1 - s["fill_p0"])
    edges = np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0001])
    s["bin"] = np.digitize(s["px"], edges) - 1

    def table(x):
        # One vote per market per bin, so a long-lived market does not swamp the rest.
        per = x.groupby(["bin", "condition_id"], observed=True).agg(px=("px", "mean"), win=("win", "mean")).reset_index()
        g = per.groupby("bin")
        out = g.agg(price=("px", "mean"), win=("win", "mean"), n_markets=("win", "size")).reset_index()
        return [dict(price=float(r.price), win=float(r.win), n_markets=int(r.n_markets)) for r in out.itertuples() if r.n_markets >= 30]

    return {"overall": table(s), "by_category": {c: table(g) for c, g in s.groupby("category") if g["condition_id"].nunique() >= 300}}


# ---- named strategies ----------------------------------------------------

STRATEGIES = [
    ("Strong favourites (85-97c)", "Price bias", "Buy the side already priced at 85-97c.", st.favourite(0.85, 0.97)),
    ("Moderate favourites (65-85c)", "Price bias", "Buy the side priced at 65-85c.", st.favourite(0.65, 0.85)),
    ("Long shots (3-15c)", "Price bias", "Buy the side priced at 3-15c.", st.longshot(0.15)),
    ("Favourites in the final 3 days", "Timing", "Buy an 80-97c side within 3 days of the scheduled end.", st.favourite(0.80, 0.97, max_days=3)),
    ("Follow a 2-day rally", "Momentum", "Buy a side that rose 8+ points in two days.", st.momentum("mom4", 0.08, follow=True)),
    ("Fade a 2-day rally", "Momentum", "Buy against a side that rose 8+ points in two days.", st.momentum("mom4", 0.08, follow=False)),
    ("Follow a 12-hour jump", "Momentum", "Buy a side that rose 8+ points in 12 hours.", st.momentum("mom1", 0.08, follow=True)),
    ("Random side (no skill)", "Baseline", "Coin-flip a side in every market. Shows what costs alone do.", st.random_side()),
]


def named_strategies() -> list:
    out = []
    for name, family, desc, strat in STRATEGIES:
        row = {"name": name, "family": family, "description": desc}
        for split in ("train", "validation"):
            rep = engine.backtest(strat, split=split, name=name, hypothesis=desc)
            row[split] = {"stats": rep.stats, "equity": equity_curve(rep.trades)}
        out.append(row)
    return out


# ---- traders -------------------------------------------------------------

COPY_VARIANTS = [
    ("Every large buyer (no skill filter)", lambda s: s, False),
    ("Proven: 20+ markets, consistent profit", lambda s: s[(s["c_n"] >= 20) & (s["c_t"] >= 2) & (s["c_roi"] > 0.02)], False),
    ("Elite: 30+ markets, very consistent, 5%+ return", lambda s: s[(s["c_n"] >= 30) & (s["c_t"] >= 3) & (s["c_roi"] > 0.05)], False),
    ("New wallets making a large first bet", lambda s: s[(s["c_n"] == 0) & (s["capital"] >= 10_000)], False),
    ("Bet against consistent losers", lambda s: s[(s["c_n"] >= 20) & (s["c_t"] <= -2)], True),
]


def _flip(sig: pd.DataFrame) -> pd.DataFrame:
    out = sig.copy()
    out["outcome_index"] = 1 - out["outcome_index"]
    out["price"] = 1 - out["price"]
    return out


def traders() -> dict:
    sig = wallets.signals(min_cash=2_000)
    split = sig["condition_id"].astype(str).map(store.markets().set_index("condition_id")["split"])
    copy = []
    for label, pick, flip in COPY_VARIANTS:
        row = {"label": label}
        for sp in ("train", "validation"):
            s = pick(sig[(split == sp).to_numpy()])
            if flip:
                s = _flip(s)
            t = wallets.copy_trades(s)
            row[sp] = engine.summarise(t)
            row[sp + "_equity"] = equity_curve(t)
        copy.append(row)
        print("  copy:", label, {sp: round(row[sp].get("roi_pct", 0), 2) for sp in ("train", "validation")}, flush=True)
    pers = wallets.persistence().reset_index()
    lb = wallets.leaderboard(top=200).reset_index()
    f = wallets.fills()
    return {
        "persistence": [dict(group=int(r.group), wallets=int(r.wallets), past_t=float(r.past_t),
                             future_winners=float(r.future_winners), future_roi=float(r.future_roi)) for r in pers.itertuples()],
        "leaderboard": [dict(wallet=str(r.wallet), pnl=float(r.pnl), capital=float(r.capital), roi=float(r.roi),
                             n_markets=int(r.n_markets), t=float(r.t), win_rate=float(r.win_rate)) for r in lb.itertuples()],
        "copy": copy,
        "copy_note": ("Entry is 10 minutes after the trader's fill, at the next traded price plus spread and fees, "
                      "one position per market. Only fills of $500 or more are in the data, from markets above $1M "
                      "volume, so compare each row with the first row rather than with zero."),
        "n_fills": int(len(f)), "n_wallets": int(f["wallet"].nunique()), "n_markets": int(f["condition_id"].nunique()),
    }


# ---- overview ------------------------------------------------------------

def _verdict(tr: dict, va: dict) -> str:
    if not tr.get("n_trades") or not va.get("n_trades") or tr["n_events"] < 100 or va["n_events"] < 100:
        return "thin"
    return "good" if tr["significant"] and va["significant"] else "bad"


def overview(cal: dict, strats: list, scan_summary: dict, tr: dict | None) -> dict:
    m = store.markets()
    uni = m[m["in_universe"]]
    findings = []

    gaps = [(p["win"] - p["price"]) * 100 for p in cal["overall"]]
    worst = max(gaps, key=abs)
    findings.append({
        "title": f"Prices are {'close to fair' if abs(worst) < 3 else 'not perfectly fair'}: the largest gap between price and win rate is {worst:+.1f} points",
        "detail": "Measured on a random sample of all markets, one vote per market. A gap has to beat the spread and fees (roughly 1-3 points) before it is tradable.",
        "status": "neutral"})

    real = [s for s in strats if s["family"] != "Baseline"]
    good = [s for s in real if _verdict(s["train"]["stats"], s["validation"]["stats"]) == "good"]
    best = max(real, key=lambda s: s["validation"]["stats"].get("roi_lo", -999))
    if good:
        findings.append({"title": f"{len(good)} simple price rule(s) stayed profitable on unseen data",
                         "detail": "; ".join(f"{s['name']}: {s['validation']['stats']['roi_pct']:+.1f}% unseen" for s in good),
                         "status": "good"})
    else:
        bs = best["validation"]["stats"]
        findings.append({"title": "No simple price or momentum rule beats costs on unseen data",
                         "detail": f"Best was '{best['name']}' at {bs.get('roi_pct', 0):+.1f}% unseen (likely range {bs.get('roi_lo', 0):+.1f}% to {bs.get('roi_hi', 0):+.1f}%).",
                         "status": "bad"})

    confirmed = scan_summary.get("confirmed_real_prices", 0)
    findings.append({
        "title": f"Pattern scan: {scan_summary['rules_tested']} rules tested, {scan_summary['survived_validation']} held on unseen data, {confirmed} survived real traded prices",
        "detail": (f"{scan_summary['passed_train']} looked profitable in training (about {scan_summary['expected_by_chance_train']} expected by luck alone). "
                   "Rules that held were re-priced at what buyers actually paid; in thin markets the charted price is not a price you can buy at."),
        "status": "good" if confirmed else "bad"})

    if tr:
        p = tr["persistence"]
        top, bottom = p[-1]["future_winners"], p[0]["future_winners"]
        strong = top - bottom > 0.15
        findings.append({
            "title": (f"Trader skill {'persists' if strong else 'only weakly persists'}: {top:.0%} of the most consistent "
                      f"past winners stayed profitable, against {bottom:.0%} of the worst group"),
            "detail": ("Ranked on markets resolved before the cut-off, measured on markets entered after it. Trades made "
                       "above 97c or below 3c are excluded: including them makes wallets that buy sure things after the "
                       "result is known look like the best traders."),
            "status": "good" if strong else "neutral"})
        base = tr["copy"][0]
        for c in tr["copy"][1:]:
            v = _verdict(c["train"], c["validation"])
            diff = c["validation"].get("roi_pct", 0) - base["validation"].get("roi_pct", 0)
            findings.append({
                "title": f"Copy test - {c['label']}: {c['validation'].get('roi_pct', 0):+.1f}% unseen ({diff:+.1f} points vs copying everyone)",
                "detail": f"Training {c['train'].get('roi_pct', 0):+.1f}%; unseen likely range {c['validation'].get('roi_lo', 0):+.1f}% to {c['validation'].get('roi_hi', 0):+.1f}% over {c['validation'].get('n_events', 0)} events.",
                "status": {"good": "good", "bad": "bad", "thin": "neutral"}[v]})

    findings.append({
        "title": "Choosing markets by their final volume creates a fake edge",
        "detail": "On markets above $1M volume, cheap outcomes appear to win about 7 points more often than their price. That is hindsight: surprise winners attract volume. All price results here use a random sample instead.",
        "status": "neutral"})

    n_tested = len(real) + scan_summary["rules_tested"] + (len(tr["copy"]) - 1 if tr else 0)
    n_survived = len(good) + scan_summary.get("confirmed_real_prices", 0) + (
        sum(1 for c in tr["copy"][1:] if _verdict(c["train"], c["validation"]) == "good") if tr else 0)
    return {
        "updated_at": int(m["resolved_ts"].max()),
        "published_at": int(time.time()),
        "data": {
            "markets": int(len(uni[uni["price_tried"]])), "events": int(uni.loc[uni["price_tried"], "event_id"].nunique()),
            "all_markets_listed": int(len(m)),
            "fills": tr["n_fills"] if tr else 0, "fill_markets": tr["n_markets"] if tr else 0,
            "wallets": tr["n_wallets"] if tr else 0,
            "strategies_tested": n_tested, "strategies_survived": n_survived,
            "splits": {sp: int((uni["split"] == sp).sum()) for sp in ("train", "validation", "test")},
        },
        "findings": findings,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-traders", action="store_true")
    a = ap.parse_args()
    t0 = time.time()

    print("calibration...", flush=True)
    cal = calibration()
    _write("calibration", cal)

    print("strategies...", flush=True)
    strats = named_strategies()
    _write("strategies", strats)

    print("scan...", flush=True)
    t1 = time.time()
    rules = scan.scan()
    n, passed = len(rules), int(rules["passed_train"].sum())
    scan_summary = {"rules_tested": n, "passed_train": passed, "survived_validation": int(rules["survived"].sum()),
                    "expected_by_chance_train": round(0.025 * n, 1), "expected_by_chance_survivors": round(0.025 * passed, 1),
                    "seconds": round(time.time() - t1, 1)}
    _write("scan", json.loads(rules.to_json(orient="records")))
    # Anything that held on unseen data gets re-priced at what buyers really paid.
    rules["real_roi"], rules["fillable"], rules["confirmed"] = np.nan, np.nan, False
    if scan_summary["survived_validation"]:
        print("verifying survivors at real traded prices...", flush=True)
        checks = verify.survivors()
        _write("verify", checks)
        for v in checks:
            hit = (rules["price"] == v["price"]) & (rules["condition"] == v["condition"])
            rules.loc[hit, "real_roi"] = v["validation"]["real"].get("roi_pct", np.nan)
            rules.loc[hit, "fillable"] = v["validation"]["fillable_share"]
            rules.loc[hit, "confirmed"] = v["confirmed"]
    scan_summary["confirmed_real_prices"] = int(rules["confirmed"].sum())
    _write("scan", json.loads(rules.to_json(orient="records")))
    _write("scan_summary", scan_summary)

    tr = None
    if not a.skip_traders:
        print("traders...", flush=True)
        tr = traders()
        _write("traders", tr)
    elif (store.RESULTS_DIR / "traders.json").exists():
        tr = json.loads((store.RESULTS_DIR / "traders.json").read_text())

    _write("overview", overview(cal, strats, scan_summary, tr))
    print(f"published in {time.time() - t0:.0f}s -> {store.RESULTS_DIR}")


if __name__ == "__main__":
    main()
