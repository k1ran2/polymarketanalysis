import { useState } from "react";
import { api, fmt } from "../api";
import { Badge, DataTable, LoadState, PageHead, Stat, useLoad } from "../components/ui";

export default function Scan() {
  const state = useLoad(api.scan);
  const [onlyPassed, setOnlyPassed] = useState(true);
  const s = state.data?.summary;
  const rules = state.data?.rules || [];
  const shown = onlyPassed ? rules.filter((r) => r.passed_train) : rules;

  return (
    <>
      <PageHead title="Pattern scan">
        Every simple rule of the form “buy the side priced X when condition Y holds”, tested automatically. Trying
        many rules always throws up some lucky ones, so each must also hold on unseen data.
      </PageHead>
      <LoadState state={state} />
      {s && (
        <>
          <div className="grid cols-4">
            <Stat label="Rules tested" value={fmt.int(s.rules_tested)} />
            <Stat label="Looked good in training" value={fmt.int(s.passed_train)} hint={`about ${s.expected_by_chance_train} expected by luck`} />
            <Stat label="Held on unseen data" value={fmt.int(s.survived_validation)} hint={`about ${s.expected_by_chance_survivors} expected by luck`} />
            <Stat label="Scan time" value={`${s.seconds}s`} />
          </div>
          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
              <h2>Rules</h2>
              <div className="chips">
                <button className={`chip ${onlyPassed ? "on" : ""}`} onClick={() => setOnlyPassed(true)}>
                  Passed training
                </button>
                <button className={`chip ${!onlyPassed ? "on" : ""}`} onClick={() => setOnlyPassed(false)}>
                  All rules
                </button>
              </div>
            </div>
            <p className="sub">Returns are after spread and fees. “Low end” is the bottom of the likely range.</p>
            {shown.length === 0 ? (
              <div className="empty">No rule was clearly profitable in training.</div>
            ) : (
              <DataTable
                initialSort="train_lo"
                rows={shown}
                columns={[
                  { key: "price", label: "Price of side bought" },
                  { key: "condition", label: "Condition" },
                  { key: "train_events", label: "Train events", num: true, render: (v) => fmt.int(v) },
                  { key: "train_roi", label: "Train return", num: true, render: (v) => <span className={fmt.sign(v)}>{fmt.pct(v)}</span> },
                  { key: "train_lo", label: "Train low end", num: true, render: (v) => fmt.pct(v) },
                  { key: "valid_events", label: "Unseen events", num: true, render: (v) => fmt.int(v) },
                  { key: "valid_roi", label: "Unseen return", num: true, render: (v) => (v == null ? "–" : <span className={fmt.sign(v)}>{fmt.pct(v)}</span>) },
                  { key: "valid_lo", label: "Unseen low end", num: true, render: (v) => fmt.pct(v) },
                  {
                    key: "survived",
                    label: "Verdict",
                    render: (v, r) =>
                      v ? <Badge kind="good">Held up</Badge> : r.passed_train ? <Badge kind="bad">Faded</Badge> : <Badge>No edge</Badge>,
                  },
                ]}
              />
            )}
          </div>
        </>
      )}
    </>
  );
}
