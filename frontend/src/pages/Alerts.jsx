import { api, fmt } from "../api";
import { DataTable, LoadState, PageHead, useLoad } from "../components/ui";

export default function Alerts() {
  const state = useLoad(api.alerts);
  const d = state.data;
  const names = Object.fromEntries((d?.profiles || []).map((p) => [p.key, p.name]));
  return (
    <>
      <PageHead title="Alerts">
        Raised when a newly created wallet makes a large bet. A way to watch the pattern live, not a signal to trade
        on: read each profile’s track record first.
      </PageHead>
      <LoadState state={state} />
      {d && (
        <>
          <div className="grid cols-2">
            {d.profiles.map((p) => (
              <div className="card" key={p.key}>
                <h2>{p.name}</h2>
                <p className="sub">
                  Wallet joined within {p.max_wallet_age_days} days, has traded at most {p.max_lifetime_markets} markets,
                  and buys {fmt.usd(p.min_stake_usd)} or more of one outcome within 24 hours at{" "}
                  {Math.round(p.price_lo * 100)}–{Math.round(p.price_hi * 100)}¢.
                </p>
                <p className="note">
                  <strong>Track record: </strong>
                  {p.backtest}
                </p>
              </div>
            ))}
          </div>
          <div className="card">
            <h2>Recent alerts</h2>
            <p className="sub">
              Newest first. The watcher must be running: <span className="mono">python -m research.watch</span>
            </p>
            {d.alerts.length === 0 ? (
              <div className="empty">No alerts yet.</div>
            ) : (
              <DataTable
                initialSort="ts"
                rows={d.alerts}
                columns={[
                  { key: "ts", label: "When", render: (v) => <span style={{ whiteSpace: "nowrap" }}>{fmt.dateTime(v)}</span> },
                  {
                    key: "title",
                    label: "Market",
                    render: (v, r) => (r.url ? <a href={r.url} target="_blank" rel="noreferrer">{v}</a> : v),
                  },
                  { key: "outcome", label: "Bought" },
                  { key: "price", label: "Price", num: true, render: (v) => Math.round(v * 100) + "¢" },
                  { key: "stake_usd", label: "Stake", num: true, render: (v) => fmt.usd(v) },
                  { key: "wallet_age_days", label: "Wallet age", num: true, render: (v) => v.toFixed(1) + " d" },
                  { key: "lifetime_markets", label: "Markets ever", num: true },
                  { key: "profile", label: "Profile", render: (v) => names[v] || v },
                  { key: "wallet", label: "Wallet", render: (v) => <span className="mono" title={v}>{fmt.wallet(v)}</span> },
                ]}
              />
            )}
          </div>
        </>
      )}
    </>
  );
}
