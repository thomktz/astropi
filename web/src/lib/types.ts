/**
 * Shapes returned by the backend.
 *
 * Hand-written rather than generated, because the surface is small and the
 * generated output would need checking in anyway. `npm run build` will fail
 * on any drift the moment a component reads a field that no longer exists.
 */

export interface Coordinate {
  ra_deg: number;
  dec_deg: number;
  ra_hms: string;
  dec_dms: string;
}

export type MountState = "idle" | "slewing" | "tracking" | "parked" | "error";

export interface MountStatus {
  state: MountState;
  tracking: boolean;
  tracking_rate: string;
  pier_side: string;
  slewing: boolean;
  coord: Coordinate;
  altitude_deg: number | null;
  azimuth_deg: number | null;
  target: Coordinate | null;
  /** Negative east of the meridian, positive west. 15 degrees an hour. */
  hour_angle_deg: number | null;
}

export interface CameraStatus {
  state: string;
  gain: number | null;
  offset: number | null;
  binning: number;
  exposure_progress: number | null;
  sensor: {
    width: number;
    height: number;
    pixel_size_um: number;
    bit_depth: number;
    bayer_pattern: string | null;
  };
  cooling: {
    supported: boolean;
    enabled: boolean;
    target_c: number | null;
    sensor_c: number | null;
    power_percent: number | null;
    /** `null` where the camera has no window heater at all. */
    dew_heater: boolean | null;
  };
  pixel_scale_arcsec: number;
  field_of_view_deg: [number, number];
}

/**
 * One camera setting as the driver describes it.
 *
 * The client holds no table of its own: whatever the backend advertises is
 * what gets rendered, read-only entries included. A sensor temperature is
 * as much a control as gain is - it just travels the other way.
 */
export interface CameraControl {
  name: string;
  label: string;
  value: number | null;
  writable: boolean;
  kind: "number" | "boolean";
  minimum: number | null;
  maximum: number | null;
  default: number | null;
  step: number;
  unit: string | null;
  supports_auto: boolean;
  auto: boolean;
  description: string | null;
}

export interface GuidingStatus {
  state:
    | "stopped"
    | "calibrating"
    | "guiding"
    | "settling"
    | "dithering"
    | "lost"
    | "error";
  calibrated: boolean;
  rms_ra_arcsec: number | null;
  rms_dec_arcsec: number | null;
  rms_total_arcsec: number | null;
  samples: number;
  calibration: {
    ra_rate_arcsec_per_s: number;
    dec_rate_arcsec_per_s: number;
    angle_deg: number;
    pixel_scale_arcsec: number;
    dec_at_calibration_deg: number;
  } | null;
}

export interface Target {
  id: string;
  name: string;
  display_name: string;
  object_type: string;
  source: string;
  magnitude: number;
  constellation: string | null;
  common_names: string[];
  coord: Coordinate;
  altitude_deg: number | null;
}

export interface Visibility {
  altitude_now_deg: number;
  azimuth_now_deg: number;
  max_altitude_deg: number;
  transit_at: string | null;
  rises_at: string | null;
  sets_at: string | null;
  circumpolar: boolean;
  never_rises: boolean;
  hours_above_horizon: number;
  moon_separation_deg: number;
  curve: { at: string; alt: number; az: number }[];
}

export interface Night {
  sunset: string | null;
  sunrise: string | null;
  astronomical_dusk: string | null;
  astronomical_dawn: string | null;
  dark_hours: number;
  moon_illumination: number;
  moon_altitude_deg: number;
}

export interface Site {
  latitude_deg: number;
  longitude_deg: number;
  elevation_m: number;
  name: string;
  hemisphere: string;
}

export interface Place {
  name: string;
  country: string | null;
  admin1: string | null;
  latitude_deg: number;
  longitude_deg: number;
  elevation_m: number;
}

export type TaskState = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface Task {
  id: string;
  kind: string;
  name: string;
  state: TaskState;
  step: string;
  fraction: number | null;
  detail: Record<string, unknown>;
  messages: string[];
  error: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface FrameSummary {
  id: string;
  width: number;
  height: number;
  duration_s: number;
  kind: string;
  stored_at: number;
  metadata: Record<string, unknown>;
}

export interface DeviceInfo {
  id: string;
  name: string;
  driver: string;
  capabilities: string[];
  connection: "disconnected" | "connecting" | "connected" | "error";
}

export interface SystemInfo {
  backend: string;
  site: { name: string; latitude_deg: number; longitude_deg: number; elevation_m: number };
  optics: { focal_length_mm: number; pixel_scale_arcsec: number | null };
  catalog_size: number;
  devices: Record<string, Omit<DeviceInfo, "connection">>;
}

export interface PolarError {
  altitude_error_arcmin: number;
  azimuth_error_arcmin: number;
  total_error_arcmin: number;
  instructions: string[];
}

/** One guide-loop iteration, as it arrives over the socket. */
export interface GuideSample {
  timestamp: number;
  star_x: number;
  star_y: number;
  lock_x: number;
  lock_y: number;
  ra_error_arcsec: number;
  dec_error_arcsec: number;
  ra_pulse_ms: number;
  dec_pulse_ms: number;
  snr: number;
  hfd: number;
}

/** What the rig is working on. Session state, held by the backend. */
export interface ActiveTarget {
  id: string;
  name: string;
  display_name: string;
  object_type: string;
  source: string;
  magnitude: number | null;
  ra_deg: number;
  dec_deg: number;
  ra_hms: string;
  dec_dms: string;
  altitude_deg: number;
  azimuth_deg: number;
  hour_angle_deg: number;
  moon_separation_deg: number;
}

/** Geometry for the guide view's overlays, in guide-sensor pixels. */
export interface GuideFrameInfo {
  width: number;
  height: number;
  captured_at: number;
  lock: { x: number; y: number } | null;
  star: { x: number; y: number } | null;
  search_radius_px: number;
  candidates: { x: number; y: number; snr: number }[];
}

export type IssueSeverity = "info" | "warning" | "problem";

export interface PlanIssue {
  severity: IssueSeverity;
  message: string;
}

/** A block as the planner returns it: what to shoot, and when it lands. */
export interface PlanBlock {
  id: string;
  target_id: string | null;
  target_name: string;
  ra_deg: number;
  dec_deg: number;
  frames: number;
  exposure_s: number;
  gain: number | null;
  offset: number | null;
  binning: number;
  dither_every: number;
  center: boolean;
  autofocus: boolean;
  duration_s: number;
  integration_s: number;
  starts_at: string;
  ends_at: string;
  min_altitude_deg: number;
  max_altitude_deg: number;
  crosses_meridian: boolean;
  issues: PlanIssue[];
}

export interface Plan {
  id: string;
  name: string;
  created_at: number;
  duration_s: number;
  integration_s: number;
  starts_at: string;
  ends_at: string;
  issues: PlanIssue[];
  blocks: PlanBlock[];
}

/** A block as it is sent back: everything the planner derives is dropped. */
export interface PlanBlockIn {
  id?: string;
  target_id: string | null;
  target_name: string;
  ra_deg?: number;
  dec_deg?: number;
  frames: number;
  exposure_s: number;
  gain: number | null;
  binning: number;
  dither_every: number;
  center: boolean;
  autofocus: boolean;
}

export type DecGuideMode = "auto" | "north" | "south" | "off";

export interface GuidingSettings {
  exposure_s: number;
  gain: number;
  dec_mode: DecGuideMode;
  ra_aggressiveness: number;
  dec_aggressiveness: number;
  min_move_arcsec: number;
  max_pulse_ms: number;
  search_radius_px: number;
  edge_margin: number;
  calibration_pulse_ms: number;
  calibration_steps: number;
  settle_arcsec: number;
  settle_time_s: number;
  /** The idle loop that keeps the guide sub-display live between runs. */
  preview_enabled: boolean;
  preview_period_s: number;
}

export interface PreviewConfig {
  enabled: boolean;
  exposure_s: number;
  gain: number;
  binning: number;
  period_s: number;
  running: boolean;
}

/** What the main viewer should show: the live preview, or the last frame. */
export interface ViewFrame {
  source: "preview" | "frame";
  frame_id: string | null;
  width: number;
  height: number;
  captured_at?: number;
  stored_at?: number;
  duration_s: number;
  metadata: Record<string, unknown>;
}
