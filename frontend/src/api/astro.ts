const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;
const SIDEREAL_TO_SOLAR = 1.0027379093; // sidereal hours run slightly fast

function julianDate(date: Date): number {
  return date.getTime() / 86400000 + 2440587.5;
}

// Greenwich Mean Sidereal Time, in degrees.
function gmstDeg(jd: number): number {
  const T = (jd - 2451545.0) / 36525;
  let gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T - (T * T * T) / 38710000;
  gmst %= 360;
  if (gmst < 0) gmst += 360;
  return gmst;
}

// Local Sidereal Time, in degrees.
export function localSiderealTimeDeg(date: Date, lonDeg: number): number {
  let lst = gmstDeg(julianDate(date)) + lonDeg;
  lst %= 360;
  if (lst < 0) lst += 360;
  return lst;
}

export function altitudeDeg(raDeg: number, decDeg: number, latDeg: number, lstDeg: number): number {
  const haDeg = lstDeg - raDeg;
  const dec = decDeg * DEG2RAD;
  const lat = latDeg * DEG2RAD;
  const ha = haDeg * DEG2RAD;
  const sinAlt = Math.sin(dec) * Math.sin(lat) + Math.cos(dec) * Math.cos(lat) * Math.cos(ha);
  return Math.asin(sinAlt) * RAD2DEG;
}

export type RiseSetStatus =
  | { kind: "circumpolar" }
  | { kind: "never-rises" }
  | { kind: "up"; hoursUntilSet: number }
  | { kind: "down"; hoursUntilRise: number };

function normalize360(deg: number): number {
  const d = deg % 360;
  return d < 0 ? d + 360 : d;
}

export function riseSetStatus(raDeg: number, decDeg: number, latDeg: number, lonDeg: number, now: Date): RiseSetStatus {
  const lat = latDeg * DEG2RAD;
  const dec = decDeg * DEG2RAD;
  const cosH0 = -Math.tan(lat) * Math.tan(dec);

  if (cosH0 < -1) return { kind: "circumpolar" };
  if (cosH0 > 1) return { kind: "never-rises" };

  const h0Deg = Math.acos(cosH0) * RAD2DEG; // hour angle magnitude at rise/set
  const lst = localSiderealTimeDeg(now, lonDeg);
  const ha = normalize360(lst - raDeg); // 0-360
  const riseHa = normalize360(-h0Deg);
  const setHa = normalize360(h0Deg);

  const alt = altitudeDeg(raDeg, decDeg, latDeg, lst);

  if (alt > 0) {
    const hoursUntilSet = (normalize360(setHa - ha) / 15) / SIDEREAL_TO_SOLAR;
    return { kind: "up", hoursUntilSet };
  }
  const hoursUntilRise = (normalize360(riseHa - ha) / 15) / SIDEREAL_TO_SOLAR;
  return { kind: "down", hoursUntilRise };
}

function formatHours(h: number): string {
  const totalMin = Math.round(h * 60);
  const hh = Math.floor(totalMin / 60);
  const mm = totalMin % 60;
  return hh > 0 ? `${hh}h${mm.toString().padStart(2, "0")}m` : `${mm}m`;
}

export function formatRiseSet(status: RiseSetStatus): string {
  switch (status.kind) {
    case "circumpolar":
      return "Always up";
    case "never-rises":
      return "Never rises here";
    case "up":
      return `Sets in ${formatHours(status.hoursUntilSet)}`;
    case "down":
      return `Rises in ${formatHours(status.hoursUntilRise)}`;
  }
}

export type VisibilityLevel = "good" | "warn" | "bad";

const VISIBILITY_EMOJI: Record<VisibilityLevel, string> = {
  good: "\u{1F7E2}", // green circle
  warn: "\u{1F7E0}", // orange circle
  bad: "\u{1F534}", // red circle
};

// "good" = comfortably shootable right now, "warn" = up but setting soon
// (under an hour), "bad" = not currently up at all.
export function visibilityLevel(status: RiseSetStatus): VisibilityLevel {
  switch (status.kind) {
    case "circumpolar":
      return "good";
    case "never-rises":
      return "bad";
    case "up":
      return status.hoursUntilSet > 1 ? "good" : "warn";
    case "down":
      return "bad";
  }
}

export function visibilityEmoji(status: RiseSetStatus): string {
  return VISIBILITY_EMOJI[visibilityLevel(status)];
}
