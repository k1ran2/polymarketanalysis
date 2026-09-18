function verdictColor(label) {
  if (!label) return "var(--neutral)";
  if (label.startsWith("Strong Buy")) return "var(--success)";
  if (label.startsWith("Lean")) return "var(--success)";
  if (label.startsWith("Fade")) return "var(--danger)";
  if (label.startsWith("Mixed")) return "var(--warning)";
  return "var(--neutral)"; // Insufficient data
}

export default function VerdictBadge({ label, conviction }) {
  const color = verdictColor(label);
  return (
    <div className="verdict-badge" style={{ borderColor: color, color }}>
      <span className="verdict-label">{label}</span>
      {conviction != null && (
        <span className="verdict-conviction" title="Conviction score (0-100)">
          {conviction.toFixed(0)}
        </span>
      )}
    </div>
  );
}
