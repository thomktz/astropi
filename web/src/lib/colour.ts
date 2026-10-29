import { useSyncExternalStore } from "react";

const KEY = "astropi.colour";

/**
 * Whether frames are shown debayered to colour or as grayscale luminance.
 *
 * One switch for every place a frame is drawn, remembered across visits:
 * flipping it in the viewer and then opening a capture in the other mode
 * would read as the capture being wrong.
 */
let current = load();
const listeners = new Set<() => void>();

function load(): boolean {
  try {
    return localStorage.getItem(KEY) !== "mono";
  } catch {
    return true;
  }
}

export function setColour(on: boolean): void {
  current = on;
  try {
    localStorage.setItem(KEY, on ? "colour" : "mono");
  } catch {
    // Not remembered, but still switched for this visit.
  }
  for (const listener of listeners) listener();
}

export function useColour(): [boolean, (on: boolean) => void] {
  const colour = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
  return [colour, setColour];
}
