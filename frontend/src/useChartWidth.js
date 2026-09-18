import { useCallback, useEffect, useState } from "react";

// Recharts 3's <ResponsiveContainer> renders a 0-size chart in some of our
// layouts (CSS grid cells especially). Measure the wrapper ourselves and pass
// explicit pixel dimensions to the chart instead.
//
// Uses a CALLBACK ref (a function), not a plain useRef object, and that
// choice is load-bearing: every chart this hook feeds is wrapped in a
// conditional (equity.length > 1, sweepResults.length > 1, etc.) that's
// false when the page first mounts and only becomes true later once data
// arrives. A plain useRef + useEffect(fn, []) only checks ref.current ONCE,
// at mount, when that div doesn't exist yet -- it silently never attaches
// the ResizeObserver, and the chart never renders for the rest of that
// page visit even after real data arrives (confirmed live: a completed run
// with 167 trades showed the chart's heading but zero <svg> on the page).
// A callback ref fires again every time the node itself is attached, which
// is exactly when the div actually first exists.
export function useChartWidth() {
  const [node, setNode] = useState(null);
  const [width, setWidth] = useState(0);
  const ref = useCallback((el) => setNode(el), []);

  useEffect(() => {
    if (!node) return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(node);
    return () => ro.disconnect();
  }, [node]);

  return [ref, width];
}
