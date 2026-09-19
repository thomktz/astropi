/**
 * A trend line small enough to sit in the status strip.
 *
 * No axes and no labels on purpose: the number beside it carries the value,
 * and this only has to answer "is it getting worse".
 */
export function Sparkline({
  values,
  width = 56,
  height = 16,
  max,
}: {
  values: number[];
  width?: number;
  height?: number;
  max?: number;
}) {
  if (values.length < 2) return null;

  // A fixed ceiling where one is given, so the shape means the same thing
  // from one glance to the next rather than rescaling to whatever is
  // currently on screen.
  const ceiling = max ?? Math.max(...values, 1e-6);
  const x = (index: number) => (index / (values.length - 1)) * width;
  const y = (value: number) => height - Math.min(value / ceiling, 1) * height;

  const path = values.map((value, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(value)}`).join(" ");

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.3" opacity="0.85" />
    </svg>
  );
}
