import { api, fmt } from "../api";
import { Badge, DataTable, LoadState, PageHead, useLoad } from "../components/ui";

const RENDER = {
  int: (v) => fmt.int(v),
  usd: (v) => fmt.usdCompact(v),
  pct0: (v) => (v == null ? "–" : fmt.pctPlain(v, 0)),
  pct1: (v) => (v == null ? "–" : fmt.pctPlain(v, 1)),
  pts: (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(1) + " pts"),
  ret: (v) => (v == null ? "–" : <span className={fmt.sign(v)}>{fmt.pct(v)}</span>),
  ret_plain: (v) => (v == null ? "–" : fmt.pct(v)),
};

function columns(cols) {
  return cols.map((c) => ({ key: c.key, label: c.label, num: !!c.kind, render: c.kind ? RENDER[c.kind] : undefined }));
}

const STATUS = { good: "Works", bad: "Does not work", neutral: "Mixed" };

export default function DeepDives() {
  const state = useLoad(api.studies);
  return (
    <>
      <PageHead title="Deep dives">
        Specific ideas examined in detail. Each starts with the answer, then shows the numbers behind it.
      </PageHead>
      <LoadState state={state} />
      {state.data?.map((s) => (
        <div className="card" key={s.title}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <h2>{s.title}</h2>
            <Badge kind={s.status === "neutral" ? "warn" : s.status}>{STATUS[s.status]}</Badge>
          </div>
          <p style={{ margin: "8px 0 14px", maxWidth: 900 }}>{s.takeaway}</p>
          <DataTable columns={columns(s.columns)} rows={s.rows} />
          {s.extra && (
            <>
              <h2 style={{ marginTop: 22, marginBottom: 8 }}>{s.extra.title}</h2>
              <DataTable columns={columns(s.extra.columns)} rows={s.extra.rows} />
            </>
          )}
          <p className="note" style={{ marginTop: 12 }}>{s.note}</p>
        </div>
      ))}
    </>
  );
}
