"""Pattern scan: go through the data and ask which conditions carried an edge.

Every candidate is a simple rule: "buy the side priced in bucket P, when
feature F is in bucket B". Each is backtested with full costs on the TRAIN
period; only rules that are clearly positive there are then checked on the
VALIDATION period, which the search never saw.

Searching hundreds of rules guarantees some look good by luck. The scan
therefore reports how many rules were tried, how many passed train, and how
many of those survived validation, next to how many would be expected to
survive by chance alone.

    python -m research.scan
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from . import engine, panel, store

PRICE_BUCKETS = [(0.03, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80),
                 (0.80, 0.90), (0.90, 0.97)]
FEATURE_BUCKETS = {
    "all": [None],
    "category": ["Sports", "Politics", "Crypto", "Economy", "Culture", "World", "Tech", "Weather", "Other"],
    "days_to_end": [(-1e9, 0), (0, 1), (1, 3), (3, 10), (10, 30), (30, 1e9)],
    "age_days": [(0, 1), (1, 7), (7, 30), (30, 1e9)],
    "mom4_side": [(-1, -0.10), (-0.10, -0.03), (-0.03, 0.03), (0.03, 0.10), (0.10, 1)],
    "mom14_side": [(-1, -0.15), (-0.15, -0.05), (-0.05, 0.05), (0.05, 0.15), (0.15, 1)],
    "vol6": [(0, 0.01), (0.01, 0.03), (0.03, 1)],
    "neg_risk": [True, False],
}


def _two_sided(df: pd.DataFrame) -> tuple:
    """Stack the panel twice so each row is 'buy this side now', with features from that side's view."""
    a, b = df.copy(), df.copy()
    a["side"], b["side"] = 0, 1
    a["px_side"], b["px_side"] = a["p0"], 1 - b["p0"]
    for c in ("mom4", "mom14"):
        a[c + "_side"], b[c + "_side"] = a[c], -b[c]
    s = pd.concat([a, b], ignore_index=True)
    return s


NAMES = {"days_to_end": "days to scheduled end", "age_days": "market age (days)", "mom4_side": "2-day move",
         "mom14_side": "7-day move", "vol6": "recent volatility", "neg_risk": "multi-outcome event", "category": "category"}


def _label(feature, bucket) -> str:
    if bucket is None:
        return "any"
    feature = NAMES.get(feature, feature)
    if isinstance(bucket, tuple):
        lo, hi = bucket
        lo_s = "-inf" if lo <= -1e8 else f"{lo:g}"
        hi_s = "+inf" if hi >= 1e8 else f"{hi:g}"
        return f"{feature} {lo_s}..{hi_s}"
    return f"{feature}={bucket}"


def _mask(s: pd.DataFrame, feature, bucket) -> np.ndarray:
    if bucket is None:
        return np.ones(len(s), dtype=bool)
    col = s[feature]
    if isinstance(bucket, tuple):
        return ((col >= bucket[0]) & (col < bucket[1])).to_numpy()
    return (col == bucket).to_numpy()


def _evaluate(s: pd.DataFrame, mask: np.ndarray, **run_kw) -> dict:
    sig = pd.Series(np.where(mask, s["side"].to_numpy(), -1), index=s.index)
    return engine.summarise(engine.run(sig, s, **run_kw))


def scan(min_events: int = 150, **run_kw) -> pd.DataFrame:
    full = panel.build()
    stacks = {sp: _two_sided(full[(full["split"] == sp) & full["tradable"]]) for sp in ("train", "validation")}
    rows = []
    for plo, phi in PRICE_BUCKETS:
        pm = {sp: ((s["px_side"] >= plo) & (s["px_side"] < phi)).to_numpy() for sp, s in stacks.items()}
        for feature, buckets in FEATURE_BUCKETS.items():
            for bucket in buckets:
                tr = _evaluate(stacks["train"], pm["train"] & _mask(stacks["train"], feature, bucket), **run_kw)
                if tr.get("n_events", 0) < min_events:
                    continue
                row = {"price": f"{plo:.2f}-{phi:.2f}", "condition": _label(feature, bucket),
                       "train_events": tr["n_events"], "train_roi": tr["roi_pct"], "train_lo": tr["roi_lo"],
                       "train_hi": tr["roi_hi"], "train_edge_pts": tr["edge_pts"], "passed_train": tr["roi_lo"] > 0}
                if row["passed_train"]:
                    va = _evaluate(stacks["validation"], pm["validation"] & _mask(stacks["validation"], feature, bucket), **run_kw)
                    if va.get("n_events", 0) >= 30:
                        row.update(valid_events=va["n_events"], valid_roi=va["roi_pct"], valid_lo=va["roi_lo"],
                                   valid_hi=va["roi_hi"], survived=va["roi_lo"] > 0)
                rows.append(row)
    out = pd.DataFrame(rows)
    if "survived" not in out:
        out["survived"] = False
    out["survived"] = out["survived"].fillna(False).astype(bool)
    return out.sort_values(["survived", "train_lo"], ascending=False).reset_index(drop=True)


def main():
    t0 = time.time()
    out = scan()
    n, passed, survived = len(out), int(out["passed_train"].sum()), int(out["survived"].sum())
    # Chance alone puts ~2.5% of no-edge rules above zero on train, and 2.5% of those above zero on validation.
    summary = {"rules_tested": n, "passed_train": passed, "survived_validation": survived,
               "expected_by_chance_train": round(0.025 * n, 1), "expected_by_chance_survivors": round(0.025 * passed, 1),
               "seconds": round(time.time() - t0, 1)}
    store.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_json(store.RESULTS_DIR / "scan.json", orient="records")
    (store.RESULTS_DIR / "scan_summary.json").write_text(json.dumps(summary))
    pd.set_option("display.width", 220)
    print(json.dumps(summary))
    cols = ["price", "condition", "train_events", "train_roi", "train_lo", "valid_events", "valid_roi", "valid_lo", "survived"]
    print(out[out["passed_train"]][[c for c in cols if c in out]].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
