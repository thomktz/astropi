import type { CelestialObject, TrackingStatus } from "./types";

// Backend runs on the same host as the frontend (the Pi), on port 8000,
// regardless of which hostname/IP the frontend itself was reached through.
const BACKEND_URL = `http://${window.location.hostname}:8000`;

type Listener<T> = (value: T) => void;

interface StatusResponse {
  state: TrackingStatus["state"];
  ra_deg: number;
  dec_deg: number;
  target: { ra_deg: number; dec_deg: number } | null;
}

const trackingListeners = new Set<Listener<TrackingStatus>>();

// The backend only knows RA/Dec - it has no idea an object is "M31" vs a
// plain coordinate. The frontend remembers which catalog object was last
// commanded so the UI can still show its name/commonNames.
let lastTarget: CelestialObject | null = null;
let pollHandle: ReturnType<typeof setInterval> | null = null;

function toTrackingStatus(res: StatusResponse): TrackingStatus {
  return {
    state: res.state,
    target: res.target ? lastTarget : null,
    raDeg: res.ra_deg,
    decDeg: res.dec_deg,
  };
}

async function fetchStatus(): Promise<TrackingStatus | null> {
  try {
    const res = await fetch(`${BACKEND_URL}/status`);
    if (!res.ok) return null;
    return toTrackingStatus(await res.json());
  } catch {
    return null; // backend unreachable - leave listeners on their last known state
  }
}

function emit(status: TrackingStatus) {
  trackingListeners.forEach((cb) => cb(status));
}

function ensurePolling() {
  if (pollHandle) return;
  pollHandle = setInterval(async () => {
    const status = await fetchStatus();
    if (status) emit(status);
  }, 2000);
}

export function subscribeTracking(cb: Listener<TrackingStatus>): () => void {
  trackingListeners.add(cb);
  fetchStatus().then((status) => {
    if (status) emit(status);
  });
  ensurePolling();
  return () => {
    trackingListeners.delete(cb);
    if (trackingListeners.size === 0 && pollHandle) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  };
}

export async function gotoObject(target: CelestialObject): Promise<void> {
  lastTarget = target;
  emit({ state: "slewing", target, raDeg: target.ra, decDeg: target.dec });

  const res = await fetch(`${BACKEND_URL}/goto`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ra_deg: target.ra, dec_deg: target.dec }),
  });
  if (res.ok) emit(toTrackingStatus(await res.json()));
}

export async function park(): Promise<void> {
  const res = await fetch(`${BACKEND_URL}/park`, { method: "POST" });
  if (res.ok) {
    lastTarget = null;
    emit(toTrackingStatus(await res.json()));
  }
}
