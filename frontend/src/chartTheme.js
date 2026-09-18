// Shared Recharts theming so charts sit inside the dark UI instead of
// popping a default white tooltip/legend on top of it.
export const tooltipStyle = {
  contentStyle: {
    background: "#1a1e27",
    border: "1px solid #363c4a",
    borderRadius: 8,
    padding: "8px 12px",
    boxShadow: "0 4px 16px rgba(0,0,0,0.35)",
  },
  itemStyle: { color: "#eef0f4", fontSize: 12.5 },
  labelStyle: { color: "#939bab", fontSize: 11.5, marginBottom: 4 },
  cursor: { fill: "rgba(255,255,255,0.04)" },
};

export const legendStyle = {
  wrapperStyle: { fontSize: 12, color: "#939bab", paddingTop: 8 },
};

export const axisColor = "#656d7d";
export const gridColor = "#262b36";

export const CHART_COLORS = {
  accent: "#5b7fff",
  danger: "#ef5f6a",
  warning: "#e2ac4a",
  neutral: "#8791a3",
  violet: "#8a5bff",
};
