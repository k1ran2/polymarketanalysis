import { api, fmt, verdict } from "../api";
import { GroupBars } from "../components/charts";
import { Badge, DataTable, LoadState, PageHead, useLoad } from "../components/ui";

export default function Traders() {
  const state = useLoad(api.traders);
  const d = state.data;

  return (
    <>
      <PageHead title="Traders">
        Skill is measured like a portfolio: dollars made per dollar put at risk, so large positions count for more.
        A market only counts once its result was known.
      </PageHead>
      <LoadState state={state} />
      {d && (
        <>
          <div className="grid cols-2">
            <div className="card">
              <h2>Does past skill carry forward?</h2>
              <p className="sub">
                Traders ranked by past consistency (1 = worst, {d.persistence.length} = best), then the share who made
                money on markets they entered afterwards.
              </p>
              <GroupBars
                data={d.persistence.map((p) => ({ ...p, share: p.future_winners * 100 }))}
                xKey="group"
                yKey="share"
                xLabel="Past skill group"
                baseline={50}
                yFormat={(v) => v + "%"}
                tipRows={(p) => [
                  ["Profitable afterwards", p.share.toFixed(0) + "%"],
                  ["Later return on capital", fmt.pct(p.future_roi * 100, 2)],
                  ["Traders", fmt.int(p.wallets)],
                ]}
              />
            </div>
            <div className="card">
              <h2>Copying them</h2>
              <p className="sub">
                Buy the same side shortly after a qualifying trader makes a large purchase. Compared with copying
                every large purchase regardless of skill.
              </p>
              <DataTable
                rows={d.copy.map((c) => ({
                  label: c.label,
                  train: c.train.roi_pct,
                  valid: c.validation.roi_pct,
                  lo: c.validation.roi_lo,
                  events: c.validation.n_events,
                  v: verdict(c.train, c.validation),
                }))}
                columns={[
                  { key: "label", label: "Who is copied" },
                  { key: "train", label: "Train", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v)}</span> },
                  { key: "valid", label: "Unseen", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v)}</span> },
                  { key: "lo", label: "Low end", num: true, render: (v) => fmt.pct(v) },
                  { key: "events", label: "Events", num: true, render: (v) => fmt.int(v) },
                  { key: "v", label: "Verdict", render: (v) => <Badge kind={v.kind}>{v.label}</Badge> },
                ]}
              />
              <p className="note" style={{ marginTop: 10 }}>{d.copy_note}</p>
            </div>
          </div>

          <div className="card">
            <h2>Leaderboard</h2>
            <p className="sub">
              Top {d.leaderboard.length} by consistency of profit across markets. Return is total profit divided by
              total capital put at risk.
            </p>
            <DataTable
              initialSort="t"
              limit={100}
              rows={d.leaderboard}
              columns={[
                { key: "wallet", label: "Wallet", render: (v) => <span className="mono" title={v}>{fmt.wallet(v)}</span> },
                { key: "pnl", label: "Profit", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.usdCompact(v)}</span> },
                { key: "capital", label: "Capital at risk", num: true, render: (v) => fmt.usdCompact(v) },
                { key: "roi", label: "Portfolio return", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v * 100, 2)}</span> },
                { key: "n_markets", label: "Markets", num: true, render: (v) => fmt.int(v) },
                { key: "win_rate", label: "Markets won", num: true, render: (v) => fmt.pctPlain(v * 100, 0) },
                { key: "t", label: "Consistency", num: true, render: (v) => fmt.num(v, 1) },
              ]}
            />
          </div>
        </>
      )}
    </>
  );
}
