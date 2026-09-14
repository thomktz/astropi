import type { CelestialObject } from "./types";
import { MESSIER_OBJECTS } from "./messier";
import { NGC_OBJECTS } from "./ngc";
import { BRIGHT_STARS } from "./stars";
import { PLANET_OBJECTS } from "./planets";

export const CATALOG: CelestialObject[] = [...PLANET_OBJECTS, ...BRIGHT_STARS, ...MESSIER_OBJECTS, ...NGC_OBJECTS].sort(
  (a, b) => a.magnitude - b.magnitude
);

// Lower is a better match. Name/common-name matches always outrank a
// constellation match, so e.g. "andromeda" surfaces the Andromeda Galaxy
// itself before the dozens of unrelated stars that merely sit in that
// constellation.
function matchScore(obj: CelestialObject, q: string): number | null {
  const name = obj.name.toLowerCase();
  if (name === q) return 0;
  if (obj.commonNames.some((n) => n.toLowerCase() === q)) return 1;
  if (name.startsWith(q)) return 2;
  if (obj.commonNames.some((n) => n.toLowerCase().startsWith(q))) return 3;
  if (name.includes(q)) return 4;
  if (obj.commonNames.some((n) => n.toLowerCase().includes(q))) return 5;
  if (obj.constellation?.toLowerCase().includes(q)) return 6;
  return null;
}

export function searchCatalog(query: string): CelestialObject[] {
  const q = query.trim().toLowerCase();
  if (!q) return CATALOG;

  const scored = CATALOG.map((obj) => ({ obj, score: matchScore(obj, q) })).filter(
    (entry): entry is { obj: CelestialObject; score: number } => entry.score !== null
  );
  scored.sort((a, b) => a.score - b.score || a.obj.magnitude - b.obj.magnitude);
  return scored.map((entry) => entry.obj);
}
