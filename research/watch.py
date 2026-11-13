"""Live alert when a newly created wallet makes a large bet.

    python -m research.watch            # run until stopped; checks every minute
    python -m research.watch --once     # scan the last 24 hours and exit

How it decides (all thresholds are in PROFILES below):
  1. Read Polymarket's public trade feed for BUY fills of $1,000 or more.
  2. Add up each wallet's buying per market and outcome over the last 24 hours.
  3. When a wallet's total crosses a profile's stake threshold at a price in
     its band, look up when the wallet joined and how many markets it has
     ever traded.
  4. If it joined recently and has traded few markets, raise an alert:
     a desktop notification, a line in research/data/alerts.jsonl, and a row
     on the Alerts page.

This is a way to watch a pattern, not a proven strategy. See the backtest
notes on each profile.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass

from . import store
from .api import DATA, ApiError, Client

ALERTS = store.DATA_DIR / "alerts.jsonl"
MIN_FILL_USD = 1_000
WINDOW_S = 24 * 3600


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    min_stake_usd: float
    price_lo: float
    price_hi: float
    max_wallet_age_days: float
    max_lifetime_markets: int
    backtest: str


PROFILES = [
    Profile("late_conviction", "New wallet, large bet on a strong favourite", 10_000, 0.85, 0.95, 7, 3,
            "Wallets like this won about 95% of the time at an average price of 91.5c (2024-07 to 2026-03). "
            "Copying one minute later returned +3.7% in training and -2.0% on unseen data, so following is unproven."),
    Profile("big_fresh_bet", "New wallet, large bet at longer odds", 25_000, 0.05, 0.85, 7, 3,
            "Historically these lose: new wallets betting $10k+ at 5-70c lost money in both periods. "
            "Shown because a genuine insider would look like this; most such wallets are not."),
]


def notify(title: str, message: str):
    """macOS desktop notification. Text is passed as arguments, never spliced into the script."""
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        subprocess.run(["osascript", "-e", script, title[:120], message[:240]], timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


class Watcher:
    def __init__(self, client: Client | None = None, desktop: bool = True):
        self.c = client or Client()
        self.desktop = desktop
        self.fills: dict = {}          # (wallet, condition, outcome) -> list of (ts, cash, shares)
        self.meta: dict = {}           # (wallet, condition, outcome) -> last trade row (title etc.)
        self.seen: set = set()         # fill ids already counted
        self.alerted: set = set()      # (profile, wallet, condition, outcome)
        self.profiles_cache: dict = {} # wallet -> (join_ts, lifetime_markets)
        self.newest_ts = 0
        if ALERTS.exists():
            for line in ALERTS.read_text().splitlines():
                try:
                    a = json.loads(line)
                    self.alerted.add((a["profile"], a["wallet"], a["condition_id"], a["outcome_index"]))
                except (ValueError, KeyError):
                    continue

    def _fetch(self, since_ts: float) -> list:
        """Buy fills newer than since_ts, oldest first."""
        out, cursor = [], None
        for _ in range(40):
            p = dict(limit=1000, filter_type="CASH", filter_amount=MIN_FILL_USD, side="BUY")
            if cursor:
                p["cursor"] = cursor
            page = self.c.get(DATA + "/trades", p)
            rows = page.get("data") or []
            out.extend(r for r in rows if r["timestamp"] >= since_ts)
            cursor = (page.get("pagination") or {}).get("next_cursor")
            if not rows or rows[-1]["timestamp"] < since_ts or not cursor:
                break
        return out[::-1]

    def _wallet(self, wallet: str) -> tuple:
        if wallet not in self.profiles_cache:
            try:
                d = self.c.get(DATA + "/user-stats", dict(user=wallet)).get("data") or {}
            except ApiError:
                d = {}
            self.profiles_cache[wallet] = (d.get("join_date"), d.get("trades"))
        return self.profiles_cache[wallet]

    def poll(self, lookback_s: float = 180) -> list:
        """One pass. Returns the alerts raised."""
        now = time.time()
        since = max(self.newest_ts - 30, now - WINDOW_S) if self.newest_ts else now - lookback_s
        raised = []
        for r in self._fetch(since):
            fid = (r.get("transaction_hash"), r["proxy_wallet"], r["token_id"], r["size"], r["price"])
            if fid in self.seen or r.get("outcome_index", 999) not in (0, 1):
                continue
            self.seen.add(fid)
            self.newest_ts = max(self.newest_ts, r["timestamp"])
            key = (r["proxy_wallet"], r["condition_id"], r["outcome_index"])
            lst = self.fills.setdefault(key, [])
            lst.append((r["timestamp"], r["size"] * r["price"], r["size"]))
            self.meta[key] = r
            cutoff = r["timestamp"] - WINDOW_S
            lst[:] = [x for x in lst if x[0] >= cutoff]
            stake = sum(x[1] for x in lst)
            vwap = stake / max(sum(x[2] for x in lst), 1e-9)
            for prof in PROFILES:
                akey = (prof.key, *key)
                if akey in self.alerted or stake < prof.min_stake_usd or not (prof.price_lo <= vwap < prof.price_hi):
                    continue
                join_ts, n_markets = self._wallet(r["proxy_wallet"])
                if join_ts is None:
                    continue
                age_days = (r["timestamp"] - join_ts) / 86400
                if age_days > prof.max_wallet_age_days or (n_markets or 0) > prof.max_lifetime_markets:
                    continue
                self.alerted.add(akey)
                alert = dict(ts=r["timestamp"], profile=prof.key, profile_name=prof.name, wallet=r["proxy_wallet"],
                             wallet_age_days=round(max(age_days, 0), 2), lifetime_markets=n_markets or 0,
                             stake_usd=round(stake), price=round(vwap, 3), outcome=r.get("outcome"),
                             outcome_index=r["outcome_index"], title=r.get("title"), condition_id=r["condition_id"],
                             url=f"https://polymarket.com/event/{r.get('event_slug')}" if r.get("event_slug") else None)
                store.DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(ALERTS, "a") as f:
                    f.write(json.dumps(alert) + "\n")
                if self.desktop:
                    notify(f"New wallet: ${alert['stake_usd']:,} on {alert['outcome']} at {alert['price']:.0%}",
                           f"{alert['title']} (wallet {alert['wallet_age_days']:.1f} days old)")
                raised.append(alert)
        # forget fills that have aged out so memory stays flat on long runs
        if len(self.seen) > 400_000:
            self.seen.clear()
        return raised


def recent_alerts(limit: int = 200) -> list:
    if not ALERTS.exists():
        return []
    rows = []
    for line in ALERTS.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[::-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="scan the last 24 hours and exit")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--quiet", action="store_true", help="no desktop notifications")
    a = ap.parse_args()
    w = Watcher(desktop=not a.quiet and not a.once)
    first = w.poll(lookback_s=WINDOW_S)
    print(f"scanned last 24h: {len(w.seen):,} buys of ${MIN_FILL_USD:,}+, {len(first)} alert(s)")
    for al in first:
        print(f"  {al['profile']:16s} ${al['stake_usd']:>9,} at {al['price']:.2f} on {al['outcome']!s:12.12s} | "
              f"wallet {al['wallet_age_days']}d old, {al['lifetime_markets']} markets | {al['title']}")
    if a.once:
        return
    print(f"watching (every {a.interval}s). Alerts -> {ALERTS}")
    while True:
        time.sleep(a.interval)
        try:
            for al in w.poll():
                print(time.strftime("%H:%M:%S"), json.dumps(al))
        except ApiError as e:
            print("poll failed:", str(e)[:120])


if __name__ == "__main__":
    main()
