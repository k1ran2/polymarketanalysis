"""Three focused studies, written to results/studies.json for the Deep dives page.

    python -m research.studies

  1. Betting NO when YES looks too high.
  2. Betting with past winners and against past losers.
  3. New wallets ("insider indicator"): who they are, how they do, and whether
     following them pays.
"""
from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd

from . import engine, panel, store, verify, wallets

warnings.filterwarnings("ignore")


def _ci(d: pd.DataFrame) -> tuple:
    lo, hi = engine._bootstrap_roi(d["pnl"].to_numpy(), d["capital"].to_numpy(), d["event_id"].to_numpy(), n=1000)
    return 100 * d["pnl"].sum() / d["capital"].sum(), 100 * lo, 100 * hi


# ---- 1. buy NO ---------------------------------------------------------------

def study_no() -> dict:
    df = panel.build()
    m = store.markets()
    yn = m[m["in_universe"] & m["outcome0"].str.lower().eq("yes")]
    no_share = float((yn["payout0"] == 0).mean())
    rows = []
    for lo, hi in [(0.03, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80)]:
        row = {"band": f"{lo * 100:.0f}-{hi * 100:.0f}c"}
        for sp in ("train", "validation"):
            d = df[df["split"] == sp]
            hit = d["is_yesno"] & (d["p0"] >= lo) & (d["p0"] < hi)
            cal = d[hit & d["tradable"]].groupby("condition_id").agg(px=("fill_p0", "mean"), win=("payout0", "mean"))
            t = engine.run(pd.Series(np.where(hit, 1, -1), index=d.index), d)
            s = engine.summarise(t)
            row[f"{sp}_charted"] = s.get("roi_pct")
            if sp == "validation":
                row["yes_price"] = float(cal["px"].mean() * 100)
                row["yes_won"] = float(cal["win"].mean() * 100)
                row["overpriced"] = row["yes_price"] - row["yes_won"]
                row["markets"] = int(len(cal))
                if lo >= 0.20 and hi <= 0.65:      # only the bands with an apparent edge are worth re-pricing
                    real = verify.reprice(t)
                    rs = engine.summarise(real) if len(real) else {}
                    row["real"] = rs.get("roi_pct")
                    row["real_lo"], row["real_hi"] = rs.get("roi_lo"), rs.get("roi_hi")
                    row["fillable"] = 100 * len(real) / max(len(t), 1)
        rows.append(row)
    mid = [r for r in rows if r.get("real") is not None and r["band"] in ("35-50c", "50-65c")]
    return {
        "title": "Betting NO when YES looks too high",
        "status": "bad",
        "takeaway": (f"You are right that most things do not happen: {no_share:.0%} of Yes/No markets resolved NO. But prices already "
                     f"reflect it. On the chart, YES priced 35-65c wins 4-7 points less often than its price, and buying NO there "
                     f"looks like +9%. At the prices buyers of NO actually paid, the return was "
                     f"{mid[0]['real']:+.1f}% and {mid[1]['real']:+.1f}%, and only about {np.mean([r['fillable'] for r in mid]):.0f}% of those trades could be filled."),
        "columns": [
            {"key": "band", "label": "YES priced"}, {"key": "markets", "label": "Markets", "kind": "int"},
            {"key": "yes_won", "label": "YES actually won", "kind": "pct0"},
            {"key": "overpriced", "label": "YES overpriced by", "kind": "pts"},
            {"key": "train_charted", "label": "Buy NO, training", "kind": "ret"},
            {"key": "validation_charted", "label": "Buy NO, unseen (chart price)", "kind": "ret"},
            {"key": "real", "label": "Buy NO, unseen (real price)", "kind": "ret"},
            {"key": "fillable", "label": "Fillable", "kind": "pct0"},
        ],
        "rows": rows,
        "note": "Returns include a 1c half-spread and fees. 'Real price' is the average price takers paid for NO in the six hours after the signal; trades nobody could have made are dropped.",
    }


# ---- 2. winners vs losers ----------------------------------------------------

def _buys() -> pd.DataFrame:
    m = store.markets().set_index("condition_id")
    sig = wallets.signals(min_cash=500, lo=0.05, hi=0.95)
    cid = sig["condition_id"].astype(str)
    sig["event_id"] = cid.map(m["event_id"]).to_numpy()
    sig["won"] = (sig["pnl"] > 0).astype(float)
    return sig


def study_record(sig: pd.DataFrame) -> dict:
    rec = sig[sig["c_n"] >= 20]
    groups = [("Heavy past losers (lost $100k+)", rec["c_pnl"] <= -1e5), ("Past losers (lost $10k-100k)", (rec["c_pnl"] > -1e5) & (rec["c_pnl"] <= -1e4)),
              ("About break-even", (rec["c_pnl"] > -1e4) & (rec["c_pnl"] <= 1e4)), ("Past winners (made $10k-100k)", (rec["c_pnl"] > 1e4) & (rec["c_pnl"] <= 1e5)),
              ("Big past winners (made $100k-1M)", (rec["c_pnl"] > 1e5) & (rec["c_pnl"] <= 1e6)), ("Biggest past winners (made $1M+)", rec["c_pnl"] > 1e6),
              ("Consistent losers (t below -2)", rec["c_t"] <= -2), ("Consistent winners (t above 2)", rec["c_t"] > 2),
              ("Very consistent winners (t above 3)", rec["c_t"] > 3)]
    rows = []
    for label, mask in groups:
        g = rec[mask.to_numpy()]
        roi, lo, hi = _ci(g)
        rows.append({"group": label, "wallets": int(g["wallet"].nunique()), "bets": int(len(g)), "capital": float(g["capital"].sum()),
                     "price": float(np.average(g["price"], weights=g["capital"]) * 100), "won": float(np.average(g["won"], weights=g["capital"]) * 100),
                     "roi": roi, "lo": lo, "hi": hi})
    w, l = rows[7], rows[6]
    return {
        "title": "Betting with past winners, against past losers",
        "status": "bad",
        "takeaway": (f"It is not true by definition, because a past record only pays if it predicts the NEXT bet. It barely does. Measured at the "
                     f"trader's own price with no delay and no costs, the next bets of consistent winners returned {w['roi']:+.1f}% and those of "
                     f"consistent losers {l['roi']:+.1f}%. Every group sits within about 2 points of zero, and the biggest past winners did slightly "
                     f"worse than the heavy past losers. Most past records were luck, and a copier also pays the spread and fees (about 3.5%)."),
        "columns": [
            {"key": "group", "label": "Trader's record when the bet was made"}, {"key": "wallets", "label": "Traders", "kind": "int"},
            {"key": "bets", "label": "Later bets", "kind": "int"}, {"key": "capital", "label": "Money staked", "kind": "usd"},
            {"key": "price", "label": "Avg price paid", "kind": "pct0"}, {"key": "won", "label": "Won", "kind": "pct0"},
            {"key": "roi", "label": "Return on those bets", "kind": "ret"}, {"key": "lo", "label": "Low end", "kind": "ret_plain"},
            {"key": "hi", "label": "High end", "kind": "ret_plain"},
        ],
        "rows": rows,
        "note": "Traders with at least 20 resolved markets at the time of each bet. Bets are purchases of $500+ at 5-95c in markets above $1M volume, July 2024 to March 2026. The record uses only results already known when the bet was placed.",
    }


# ---- 3. new wallets -----------------------------------------------------------

def _first_day_buys() -> pd.DataFrame:
    f = wallets.fills()
    first = f.groupby("wallet", observed=True)["ts"].min().rename("first_seen")
    b = f[f["buy"]].join(first, on="wallet")
    b = b[(b["ts"] - b["first_seen"]) <= 86400].sort_values(["wallet", "ts"], kind="stable")
    b["mk_new"] = (~b.duplicated(["wallet", "condition_id"])).astype(int)
    b["n_mkts_so_far"] = b.groupby("wallet", observed=True)["mk_new"].cumsum()
    g = b.groupby(["wallet", "condition_id", "outcome_index"], observed=True)
    b["cum_cap"] = g["capital"].cumsum()
    b["vwap"] = b["cum_cap"] / g["size"].cumsum()
    return b


def _triggers(b: pd.DataFrame, min_usd: float, lo: float, hi: float) -> pd.DataFrame:
    """The moment a first-day wallet's stake in one market crosses `min_usd` at a price in band, having traded nothing else."""
    t = b[(b["cum_cap"] >= min_usd) & (b["vwap"] >= lo) & (b["vwap"] < hi) & (b["price"] <= 0.97) & (b["n_mkts_so_far"] == 1)]
    return t.drop_duplicates(["wallet", "condition_id", "outcome_index"])


def study_new_wallets(sig: pd.DataFrame) -> dict:
    m = store.markets().set_index("condition_id")
    exp_rows = []
    for label, mask in [("No resolved bets yet (new)", sig["c_n"] == 0), ("1-4 resolved markets", (sig["c_n"] >= 1) & (sig["c_n"] <= 4)),
                        ("5-19 resolved markets", (sig["c_n"] >= 5) & (sig["c_n"] <= 19)), ("20-99 resolved markets", (sig["c_n"] >= 20) & (sig["c_n"] <= 99)),
                        ("100+ resolved markets", sig["c_n"] >= 100)]:
        g = sig[mask.to_numpy()]
        roi, lo, hi = _ci(g)
        exp_rows.append({"group": label, "wallets": int(g["wallet"].nunique()), "capital": float(g["capital"].sum()),
                         "price": float(g["price"].mean() * 100), "won": float(g["won"].mean() * 100), "roi": roi, "lo": lo, "hi": hi})

    b = _first_day_buys()
    trig_rows = []
    for label, (mu, lo, hi) in {"$10k+ at 85-95c (strong favourite)": (1e4, 0.85, 0.95), "$10k+ at 70-85c": (1e4, 0.70, 0.85),
                                "$10k+ at 50-70c": (1e4, 0.50, 0.70), "$10k+ at 5-50c (long odds)": (1e4, 0.05, 0.50)}.items():
        t = _triggers(b, mu, lo, hi)
        sp = t["condition_id"].astype(str).map(m["split"]).to_numpy()
        row = {"profile": label, "wallets": int(t["wallet"].nunique()), "price": float(t["vwap"].mean() * 100),
               "won": float((t["pnl"] > 0).mean() * 100)}
        for s in ("train", "validation"):
            st = engine.summarise(wallets.copy_trades(t[sp == s], delay_s=60, max_slip=0.02))
            row[f"copy_{s}"] = st.get("roi_pct")
            row[f"copy_{s}_lo"], row[f"copy_{s}_hi"] = st.get("roi_lo"), st.get("roi_hi")
        trig_rows.append(row)
    new = exp_rows[0]
    fav = trig_rows[0]
    return {
        "title": "New wallets as an insider indicator",
        "status": "neutral",
        "takeaway": (f"As a group, new wallets are the worst bettors on the platform, not the best: bets by wallets with no resolved history returned "
                     f"{new['roi']:+.1f}%, against {exp_rows[-1]['roi']:+.1f}% for wallets with 100+ markets. One narrow profile does stand out: a new wallet "
                     f"putting $10k+ into a single market at 85-95c won {fav['won']:.0f}% of the time at an average price of {fav['price']:.1f}c. "
                     f"Copying it one minute later returned {fav['copy_train']:+.1f}% in training but {fav['copy_validation']:+.1f}% on unseen data, so the "
                     f"wallets do well but following them is not proven. The Alerts page watches for this profile live."),
        "columns": [
            {"key": "group", "label": "Experience when the bet was made"}, {"key": "wallets", "label": "Wallets", "kind": "int"},
            {"key": "capital", "label": "Money staked", "kind": "usd"}, {"key": "price", "label": "Avg price paid", "kind": "pct0"},
            {"key": "won", "label": "Won", "kind": "pct0"}, {"key": "roi", "label": "Return on those bets", "kind": "ret"},
            {"key": "lo", "label": "Low end", "kind": "ret_plain"}, {"key": "hi", "label": "High end", "kind": "ret_plain"},
        ],
        "rows": exp_rows,
        "extra": {
            "title": "First-day wallets staking $10k+ in a single market",
            "columns": [
                {"key": "profile", "label": "Profile"}, {"key": "wallets", "label": "Wallets", "kind": "int"},
                {"key": "price", "label": "Avg price paid", "kind": "pct1"}, {"key": "won", "label": "They won", "kind": "pct0"},
                {"key": "copy_train", "label": "Copying, training", "kind": "ret"},
                {"key": "copy_validation", "label": "Copying, unseen", "kind": "ret"},
                {"key": "copy_validation_lo", "label": "Low end", "kind": "ret_plain"},
                {"key": "copy_validation_hi", "label": "High end", "kind": "ret_plain"},
            ],
            "rows": trig_rows,
        },
        "note": ("'New' in the history means first seen in the data (fills of $500+ in markets above $1M). For the 85-95c profile, 64% of the wallets "
                 "had joined Polymarket within the previous 7 days by their true join date. Copying enters one minute later at the next traded "
                 "price plus spread and fees, one position per market."),
    }


def main():
    sig = _buys()
    out = []
    for fn in (lambda: study_no(), lambda: study_record(sig), lambda: study_new_wallets(sig)):
        s = fn()
        out.append(s)
        print("\n##", s["title"], "\n", s["takeaway"])
    store.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (store.RESULTS_DIR / "studies.json").write_text(json.dumps(out, default=float))


if __name__ == "__main__":
    main()
