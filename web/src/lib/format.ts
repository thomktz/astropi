/** Small display helpers. */

export function degrees(value: number | null | undefined, digits = 1): string {
  return value == null ? "--" : `${value.toFixed(digits)}°`;
}

/**
 * A compass bearing, wrapped after rounding.
 *
 * Rounding first would turn an azimuth of 359.8 into "360°", which is a
 * bearing that does not exist.
 */
export function bearing(value: number | null | undefined, digits = 0): string {
  if (value == null) return "--";
  const factor = 10 ** digits;
  const scaled = Math.round(value * factor);
  const wrapped = ((scaled % (360 * factor)) + 360 * factor) % (360 * factor);
  return `${(wrapped / factor).toFixed(digits)}°`;
}

export function arcmin(value: number | null | undefined, digits = 1): string {
  return value == null ? "--" : `${value.toFixed(digits)}'`;
}

export function arcsec(value: number | null | undefined, digits = 2): string {
  return value == null ? "--" : `${value.toFixed(digits)}"`;
}

export function temperature(value: number | null | undefined): string {
  return value == null ? "--" : `${value.toFixed(1)}°C`;
}

export function clockTime(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function duration(hours: number): string {
  const total = Math.round(hours * 60);
  return total >= 60 ? `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, "0")}m` : `${total}m`;
}

/**
 * How observable something is right now.
 *
 * The thresholds are about atmosphere, not aesthetics: below roughly 20
 * degrees you are shooting through more than twice the air mass of the
 * zenith, and it shows in the data.
 */
export function altitudeQuality(altitude: number | null): "good" | "fair" | "poor" {
  if (altitude == null || altitude < 0) return "poor";
  if (altitude >= 40) return "good";
  if (altitude >= 20) return "fair";
  return "poor";
}

export function moonPhaseName(illumination: number): string {
  if (illumination < 0.03) return "New";
  if (illumination < 0.35) return "Crescent";
  if (illumination < 0.65) return "Quarter";
  if (illumination < 0.97) return "Gibbous";
  return "Full";
}

/**
 * Right ascension as HHhMMmSS.Ss.
 *
 * Formatted here rather than read from the REST payload so the readout can
 * follow the WebSocket, which pushes a position every second. Taking the
 * pre-formatted string from `/api/mount` left the primary coordinate
 * display stale between polls - visibly wrong right after a slew, which is
 * exactly when it is being watched.
 */
export function formatHms(raDeg: number): string {
  let [hours, minutes, seconds] = sexagesimal((((raDeg % 360) + 360) % 360) / 15);
  // Rounding can carry all the way round the clock; 24h is 0h.
  hours %= 24;
  return `${pad(hours)}h${pad(minutes)}m${seconds.toFixed(1).padStart(4, "0")}s`;
}

/** Declination as +DD\u00b0MM'SS.S". */
export function formatDms(decDeg: number): string {
  const sign = decDeg < 0 ? "-" : "+";
  const [degrees, minutes, seconds] = sexagesimal(Math.abs(decDeg));
  return `${sign}${pad(degrees)}\u00b0${pad(minutes)}'${seconds.toFixed(1).padStart(4, "0")}"`;
}

/**
 * Split a positive value into whole units, minutes and seconds.
 *
 * Rounds to the displayed precision before splitting. Rounding each field
 * on its own lets seconds reach 60 without carrying, so 29.99999 degrees
 * prints as 29\u00b059'60.0" instead of 30\u00b000'00.0".
 */
function sexagesimal(value: number): [number, number, number] {
  const totalSeconds = Math.round(value * 3600 * 10) / 10;
  const units = Math.floor(totalSeconds / 3600);
  const remainder = totalSeconds - units * 3600;
  const minutes = Math.floor(remainder / 60);
  return [units, minutes, remainder - minutes * 60];
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** "1h12m" or "47m" - `duration` with the space removed, for the top bar. */
export function durationShort(hours: number): string {
  const total = Math.max(0, Math.round(hours * 60));
  return total >= 60 ? `${Math.floor(total / 60)}h${String(total % 60).padStart(2, "0")}m` : `${total}m`;
}

/**
 * Hours until the mount crosses the meridian.
 *
 * Hour angle runs at 15.0411 degrees per *solar* hour, not 15 - the sky
 * turns once per sidereal day, which is about four minutes shorter. Using a
 * flat 15 would drift the estimate by roughly ten seconds per hour.
 */
const HOUR_ANGLE_DEG_PER_HOUR = 15.0410686;

export function hoursToMeridian(hourAngleDeg: number): number {
  return -hourAngleDeg / HOUR_ANGLE_DEG_PER_HOUR;
}

/**
 * Coordinates without the seconds field, for a narrow bar.
 *
 * A minute of right ascension is a quarter of a degree - far too coarse to
 * point by, but this is a glanceable "roughly where is it" readout, and the
 * full-precision version is one panel away.
 */
export function formatHmsShort(raDeg: number): string {
  const [hours, minutes] = sexagesimal((((raDeg % 360) + 360) % 360) / 15);
  return `${pad(hours % 24)}h${pad(minutes)}m`;
}

export function formatDmsShort(decDeg: number): string {
  const sign = decDeg < 0 ? "-" : "+";
  const [degrees, minutes] = sexagesimal(Math.abs(decDeg));
  return `${sign}${pad(degrees)}\u00b0${pad(minutes)}'`;
}
