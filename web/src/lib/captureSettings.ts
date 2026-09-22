import { useSyncExternalStore } from "react";

/** What a deliberate capture shoots at. */
export interface CaptureSettings {
  exposure_s: number;
  /** `null` means the camera's own default rather than an override. */
  gain: number | null;
}

const KEY = "astropi.capture";
const DEFAULTS: CaptureSettings = { exposure_s: 5, gain: null };

/**
 * One set of capture settings, shared by the camera panel and the button
 * under the display.
 *
 * A module-level store rather than state in either component: they are two
 * views of the same decision, and a Capture button that shoots something
 * different from what the panel says is a trap.
 */
let current = load();
const listeners = new Set<() => void>();

function load(): CaptureSettings {
  try {
    const stored = localStorage.getItem(KEY);
    return stored ? { ...DEFAULTS, ...JSON.parse(stored) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

export function setCaptureSettings(changes: Partial<CaptureSettings>): void {
  current = { ...current, ...changes };
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    // Remembering the last exposure is not worth a blank screen.
  }
  for (const listener of listeners) listener();
}

export function useCaptureSettings(): CaptureSettings {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
}
