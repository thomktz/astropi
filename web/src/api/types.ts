export interface CelestialObject {
  id: string;
  name: string;
  commonNames: string[];
  constellation?: string;
  type: string;
  ra: number; // degrees
  dec: number; // degrees
  magnitude: number;
}

export interface TrackingStatus {
  state: "idle" | "slewing" | "tracking" | "parked";
  target: CelestialObject | null;
  raDeg: number;
  decDeg: number;
}

export interface CameraStatus {
  connected: boolean;
  iso: number;
  shutterSpeed: string;
  aperture: string;
  lastFrameUrl: string | null;
  isShooting: boolean;
}
