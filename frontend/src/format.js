export function timeAgo(ts) {
  if (!ts) return "never";
  const secs = Date.now() / 1000 - ts;
  if (secs < 5) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 1) return `${Math.round(secs)}s ago`;
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

export function formatTenure(days) {
  if (days == null) return "—";
  if (days < 1) return "<1d";
  if (days < 60) return `${Math.round(days)}d`;
  return `${(days / 30).toFixed(0)}mo`;
}

export function tierColor(tier) {
  return { Gold: "#c9971e", Silver: "#8892a0", Bronze: "#a56b3a", Unrated: "#b8b8b8" }[tier] || "#999";
}
