import type { CameraStatus, CelestialObject } from "./types";
import { searchCatalog } from "./catalog";
import { generateMockFrame } from "./mockFrame";

type Listener<T> = (value: T) => void;

let camera: CameraStatus = {
  connected: true,
  iso: 1600,
  shutterSpeed: "1/4",
  aperture: "f/2.8",
  lastFrameUrl: null,
  isShooting: false,
};

const cameraListeners = new Set<Listener<CameraStatus>>();

function emitCamera() {
  cameraListeners.forEach((cb) => cb(camera));
}

export function listObjects(query = ""): CelestialObject[] {
  return searchCatalog(query);
}

export function subscribeCamera(cb: Listener<CameraStatus>): () => void {
  cameraListeners.add(cb);
  cb(camera);
  return () => cameraListeners.delete(cb);
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
