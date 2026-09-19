import { useMemo } from "react";
import type { CelestialObject } from "../api/types";

interface SkyMapProps {
  objects: CelestialObject[];
  targetId: string | null;
  onSelect: (target: CelestialObject) => void;
}

// Simple equirectangular projection (RA/Dec -> x/y) as a placeholder until
// this is swapped for a real sky-chart widget (e.g. Aladin Lite).
export function SkyMap({ objects, targetId, onSelect }: SkyMapProps) {
  const width = 600;
  const height = 300;

  const points = useMemo(
    () =>
      objects
        .filter((obj) => obj.magnitude <= 5)
        .map((obj) => ({
          obj,
          x: (obj.ra / 360) * width,
          y: ((90 - obj.dec) / 180) * height,
        })),
    [objects]
  );

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="sky-map" role="img" aria-label="Sky map">
      <rect x={0} y={0} width={width} height={height} fill="#05070d" />
      {points.map(({ obj, x, y }) => (
        <g
          key={obj.id}
          transform={`translate(${x}, ${y})`}
          onClick={() => onSelect(obj)}
          className="sky-map-star"
        >
          <circle r={Math.max(2, 5 - obj.magnitude)} fill={obj.id === targetId ? "#4da6ff" : "#f5f5f5"} />
          <text x={6} y={4} fontSize={10} fill="#aaa">
            {obj.commonNames[0] || obj.name}
          </text>
        </g>
      ))}
    </svg>
  );
}
