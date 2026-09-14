import type { CameraStatus, CelestialObject, TrackingStatus } from "./types";
import { searchCatalog } from "./catalog";
import { generateMockFrame } from "./mockFrame";

type Listener<T> = (value: T) => void;

let tracking: TrackingStatus = {
  state: "idle",
  target: null,
  raDeg: 0,
  decDeg: 90,
};

let camera: CameraStatus = {
  connected: true,
  iso: 1600,
  shutterSpeed: "1/4",
  aperture: "f/2.8",
  lastFrameUrl: null,
  isShooting: false,
};

const trackingListeners = new Set<Listener<TrackingStatus>>();
const cameraListeners = new Set<Listener<CameraStatus>>();

function emitTracking() {
  trackingListeners.forEach((cb) => cb(tracking));
}

function emitCamera() {
  cameraListeners.forEach((cb) => cb(camera));
}

export function listObjects(query = ""): CelestialObject[] {
  return searchCatalog(query);
}

export function subscribeTracking(cb: Listener<TrackingStatus>): () => void {
  trackingListeners.add(cb);
  cb(tracking);
  return () => trackingListeners.delete(cb);
}

export function subscribeCamera(cb: Listener<CameraStatus>): () => void {
  cameraListeners.add(cb);
  cb(camera);
  return () => cameraListeners.delete(cb);
}

export async function gotoObject(target: CelestialObject): Promise<void> {
  tracking = { ...tracking, state: "slewing", target };
  emitTracking();

  await new Promise((resolve) => setTimeout(resolve, 1500));

  tracking = { ...tracking, state: "tracking", raDeg: target.ra, decDeg: target.dec };
  emitTracking();
}

export async function park(): Promise<void> {
  tracking = { state: "parked", target: null, raDeg: 0, decDeg: 90 };
  emitTracking();
}

export async function capturePhoto(): Promise<void> {
  camera = { ...camera, isShooting: true };
  emitCamera();

  await new Promise((resolve) => setTimeout(resolve, 800));

  camera = { ...camera, isShooting: false, lastFrameUrl: generateMockFrame(Date.now()) };
  emitCamera();
}

export function updateCameraSettings(partial: Partial<Pick<CameraStatus, "iso" | "shutterSpeed" | "aperture">>) {
  camera = { ...camera, ...partial };
  emitCamera();
}
