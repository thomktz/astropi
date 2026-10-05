/**
 * Polar alignment from hand-placed frames: the client for `/api/polar`.
 *
 * Kept beside the main client rather than in it, as the one screen that
 * uses these endpoints; the request helper is the same shape.
 */

import { ApiError } from "./api";

export type PolarStep = "first" | "more" | "live";

export type PolarState = {
  step: PolarStep;
  frames: { ra_deg: number; dec_deg: number; mount_ra_deg: number }[];
  fit: {
    altitude_error_arcmin: number;
    azimuth_error_arcmin: number;
    total_error_arcmin: number;
    uncertainty_arcmin: number;
    rotation_deg: number;
    frames: number;
    residual_arcsec: number;
    warnings: string[];
  } | null;
};

export type PolarShot = {
  id: string;
  frame_id: string;
  width: number;
  height: number;
  solve_error: string | null;
  solved: {
    ra_deg: number;
    dec_deg: number;
    pixel_scale_arcsec: number;
    rotation_deg: number;
    stars: number;
    solver: string;
    solve_time_s: number;
  } | null;
  /** RA axis turn since frame 1, while taking measurement frames. */
  moved_deg: number | null;
  error?: {
    altitude_error_arcmin: number;
    azimuth_error_arcmin: number;
    total_error_arcmin: number;
    instructions: string[];
  };
  /** Points as fractions of the frame: x from the left, y from the top. */
  overlay?: {
    start: [number, number];
    after_altitude: [number, number];
    aligned: [number, number];
  } | null;
};

export type ShotSettings = { exposure_s: number; gain?: number | null; binning: number };

async function call<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/polar${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // Not JSON - keep the status text.
    }
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), response.status);
  }
  return response.json();
}

export const polarApi = {
  state: () => call<PolarState>("", "GET"),
  reset: () => call<PolarState>("", "DELETE"),
  capture: (settings: ShotSettings) => call<PolarShot>("/capture", "POST", settings),
  accept: (shotId: string) => call<PolarState>("/accept", "POST", { shot_id: shotId }),
  dropLast: () => call<PolarState>("/frames/last", "DELETE"),
  live: (settings: ShotSettings) => call<PolarShot>("/live", "POST", settings),
};
