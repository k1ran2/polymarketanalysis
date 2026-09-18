const NAV_GROUPS = [
  {
    label: null,
    items: [{ id: "overview", label: "Overview" }],
  },
  {
    label: "Research",
    items: [
      { id: "markets", label: "Markets" },
      { id: "insiders", label: "Insiders" },
    ],
  },
  {
    label: "Strategy",
    items: [{ id: "backtest", label: "Backtest" }],
  },
];

export default function Sidebar({ tab, onSwitch }) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="app-logo" aria-hidden="true" />
        <div className="sidebar-brand-name">Smart Money Dashboard</div>
      </div>

      {NAV_GROUPS.map((group, i) => (
        <div className="sidebar-group" key={group.label || i}>
          {group.label && <div className="sidebar-group-label">{group.label}</div>}
          {group.items.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "sidebar-link active" : "sidebar-link"}
              onClick={() => onSwitch(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      ))}
    </aside>
  );
}
