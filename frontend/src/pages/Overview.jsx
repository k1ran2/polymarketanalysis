import { api, fmt, verdict } from "../api";
import { CalibrationChart } from "../components/charts";
import { Badge, DataTable, LoadState, PageHead, Stat, useLoad } from "../components/ui";

export default function Overview() {
  const ov = useLoad(api.overview);
  const cal = useLoad(api.calibration);
  const strat = useLoad(api.strategies);

  return (
    <>
      <PageHead title="Overview">
        What the data says so far. Every number here includes trading costs and is checked on a later period the
        strategy never saw.
      </PageHead>
      <LoadState state={ov} />
      {ov.data && (
        <>
          <div className="grid cols-4">
            <Stat label="Resolved markets" value={fmt.int(ov.data.data.markets)} hint={`${fmt.int(ov.data.data.events)} independent events`} />
            <Stat label="Trades analysed" value={fmt.int(ov.data.data.fills)} hint={`from ${fmt.int(ov.data.data.fill_markets)} markets`} />
            <Stat label="Traders tracked" value={fmt.int(ov.data.data.wallets)} />
            <Stat
              label="Strategies that held up"
              value={`${ov.data.data.strategies_survived} of ${ov.data.data.strategies_tested}`}
              hint={`data to ${fmt.date(ov.data.updated_at)}`}
            />
          </div>

          <div className="card">
            <h2>Findings</h2>
            <p className="sub">Plain-English conclusions from the latest run.</p>
            {ov.data.findings.map((f) => (
              <div className="finding" key={f.title}>
                <Badge kind={f.status}>{f.status === "good" ? "Works" : f.status === "bad" ? "Does not work" : "Note"}</Badge>
                <div>
                  <div className="title">{f.title}</div>
                  <div className="detail">{f.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="grid cols-2">
        <div className="card">
          <h2>Are prices fair?</h2>
          <p className="sub">Points above the dashed line won more often than their price implied.</p>
          <LoadState state={cal} />
          {cal.data && <CalibrationChart points={cal.data.overall} />}
        </div>
        <div className="card">
          <h2>Strategy scoreboard</h2>
          <p className="sub">Each idea tested with costs, then re-tested on unseen data.</p>
          <LoadState state={strat} />
          {strat.data && (
            <DataTable
              initialSort="valid_roi"
              rows={strat.data.map((s) => ({
                name: s.name,
                family: s.family,
                train_roi: s.train.stats.roi_pct,
                valid_roi: s.validation.stats.roi_pct,
                events: s.validation.stats.n_events,
                v: verdict(s.train.stats, s.validation.stats),
              }))}
              columns={[
                { key: "name", label: "Strategy" },
                { key: "train_roi", label: "Train", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v)}</span> },
                { key: "valid_roi", label: "Unseen", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v)}</span> },
                { key: "events", label: "Events", num: true, render: (v) => fmt.int(v) },
                { key: "v", label: "Verdict", render: (v) => <Badge kind={v.kind}>{v.label}</Badge> },
              ]}
            />
          )}
        </div>
      </div>
    </>
  );
}
