import { useEffect, useRef, useState } from "react";

// Recharts 3's <ResponsiveContainer> renders a 0-size chart in some of our
// layouts (CSS grid cells especially). Measure the wrapper ourselves and pass
// explicit pixel dimensions to the chart instead.
export function useChartWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}
