/**
 * Typed HTTP client.
 *
 * Paths are same-origin: in development Vite proxies them to the backend,
 * and in production the backend serves the built bundle, so there is no
 * base URL to configure on either side.
 */

import type {
  CameraControl,
  CameraStatus,
  MountDriverInfo,
  GuideFrameInfo,
  GuidingSettings,
  DeviceInfo,
  FrameSummary,
  GuidingStatus,
  MountStatus,
  Night,
  Place,
  Plan,
  PreviewConfig,
  PlanBlockIn,
  PolarError,
  Site,
  SystemInfo,
  Target,
  Task,
  ViewFrame,
  Visibility,
} from "./types";

export class ApiError extends Error {
  // Written out rather than declared as a constructor parameter property:
  // the project builds with erasableSyntaxOnly, which forbids TypeScript
  // syntax that emits runtime code.
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });

  if (!response.ok) {
    // The backend maps domain failures onto status codes and always sends a
    // `detail` string; surfacing that beats a bare "500".
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // Body was not JSON - keep the status text.
    }
    throw new ApiError(detail, response.status);
  }

  return response.status === 204 ? (undefined as T) : response.json();
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  system: () => request<SystemInfo>("/system"),
  devices: () => request<Record<string, DeviceInfo>>("/devices"),

  mount_driver: {
    get: () => request<MountDriverInfo>("/devices/mount/driver"),
    set: (driver: string, port?: string) =>
      request<MountDriverInfo>("/devices/mount/driver", {
        method: "PUT",
        body: JSON.stringify({ driver, port }),
      }),
    report: () => request<{ driver: string; report: Record<string, unknown> | null }>(
      "/devices/mount/report",
    ),
  },
  night: () => request<Night>("/night"),

  site: {
    get: () => request<Site>("/site"),
    set: (site: Omit<Site, "hemisphere">) =>
      request<Site>("/site", { method: "PUT", body: JSON.stringify(site) }),
    search: (q: string) => request<Place[]>(`/site/search?q=${encodeURIComponent(q)}`),
  },

  mount: {
    status: () => request<MountStatus>("/mount"),
    slew: (ra_deg: number, dec_deg: number) => post<MountStatus>("/mount/slew", { ra_deg, dec_deg }),
    sync: (ra_deg: number, dec_deg: number) => post<MountStatus>("/mount/sync", { ra_deg, dec_deg }),
    park: () => post<MountStatus>("/mount/park"),
    unpark: () => post<MountStatus>("/mount/unpark"),
    abort: () => post<MountStatus>("/mount/abort"),
    tracking: (enabled: boolean) => post<MountStatus>("/mount/tracking", { enabled }),
    pulse: (direction: "north" | "south" | "east" | "west", duration_ms: number) =>
      post<unknown>("/mount/pulse", { direction, duration_ms }),
  },

  camera: {
    status: (role = "main") => request<CameraStatus>(`/camera?role=${role}`),
    // A light frame by default: this is the deliberate capture, kept in
    // the frame store. The live view has its own endpoint and its own kind.
    expose: (
      duration_s: number,
      options: { gain?: number; binning?: number; kind?: string } = {},
    ) => post<FrameSummary>("/camera/expose", { duration_s, kind: "light", ...options }),
    abort: () => post<unknown>("/camera/abort"),
    controls: (role = "main") => request<CameraControl[]>(`/camera/controls?role=${role}`),
    setControl: (name: string, value: number, role = "main") =>
      request<CameraControl>(`/camera/controls/${name}?role=${role}`, {
        method: "PUT",
        body: JSON.stringify({ value }),
      }),
    cooling: (enabled: boolean, target_c?: number) =>
      post<unknown>("/camera/cooling", { enabled, target_c }),
    frames: () => request<FrameSummary[]>("/camera/frames"),
    preview: () => request<PreviewConfig>("/camera/preview"),
    // One live-view frame now, loop or no loop. Not stored: a look at the
    // sky is not a capture.
    previewFrame: () => post<{ captured_at: number }>("/camera/preview/frame"),
    setPreview: (changes: Partial<Omit<PreviewConfig, "running">>) =>
      request<PreviewConfig>("/camera/preview", { method: "PUT", body: JSON.stringify(changes) }),
    view: () => request<ViewFrame | null>("/camera/view"),
    // Cache-busted on the frame's own timestamp: it refetches exactly when
    // there is something new, not on every render.
    viewUrl: (stamp: number, stretch: boolean) =>
      `/api/camera/view.png?stretch=${stretch}&t=${Math.round(stamp * 1000)}`,
    previewUrl: (frameId: string, options: { stretch?: boolean; maxDimension?: number } = {}) =>
      `/api/camera/frames/${frameId}/preview.png?stretch=${options.stretch ?? true}` +
      `&max_dimension=${options.maxDimension ?? 1400}`,
  },

  guiding: {
    status: () => request<GuidingStatus>("/guiding"),
    calibrate: () => post<GuidingStatus>("/guiding/calibrate"),
    start: () => post<GuidingStatus>("/guiding/start"),
    stop: () => post<GuidingStatus>("/guiding/stop"),
    dither: (amount_px = 12) => post<GuidingStatus>("/guiding/dither", { amount_px }),
    clearCalibration: () => post<unknown>("/guiding/calibration/clear"),
    frameInfo: () => request<GuideFrameInfo | null>("/guiding/frame"),
    settings: () => request<GuidingSettings>("/guiding/settings"),
    updateSettings: (changes: Partial<GuidingSettings>) =>
      request<GuidingSettings>("/guiding/settings", {
        method: "PUT",
        body: JSON.stringify(changes),
      }),
    preview: () => post<{ width: number; height: number; stars: number }>("/guiding/preview"),
    lock: (x: number, y: number) => post<{ x: number; y: number; snr: number }>("/guiding/lock", { x, y }),
    // Cache-busted: the point of this image is that it is the newest one.
    frameUrl: (stamp: number) => `/api/guiding/frame.png?t=${Math.round(stamp)}`,
  },

  targets: {
    search: (q: string, minAltitude?: number) => {
      const params = new URLSearchParams({ q, limit: "30" });
      if (minAltitude !== undefined) params.set("min_altitude_deg", String(minAltitude));
      return request<Target[]>(`/targets/search?${params}`);
    },
    recommended: () => request<Target[]>("/targets/recommended?limit=12"),
    get: (id: string) => request<Target>(`/targets/${id}`),
    visibility: (id: string) => request<Visibility>(`/targets/${id}/visibility`),
  },

  sessions: {
    list: () => request<Plan[]>("/sessions"),
    get: (id: string) => request<Plan>(`/sessions/${id}`),
    create: (name: string, blocks: PlanBlockIn[]) => post<Plan>("/sessions", { name, blocks }),
    update: (id: string, name: string, blocks: PlanBlockIn[]) =>
      request<Plan>(`/sessions/${id}`, { method: "PUT", body: JSON.stringify({ name, blocks }) }),
    remove: (id: string) => request<unknown>(`/sessions/${id}`, { method: "DELETE" }),
    // Schedules a plan without saving, so the editor can show durations and
    // warnings while blocks are still being added.
    preview: (name: string, blocks: PlanBlockIn[]) => post<Plan>("/sessions/preview", { name, blocks }),
    run: (id: string) => post<Task>(`/sessions/${id}/run`),
  },

  tasks: {
    list: () => request<Task[]>("/tasks"),
    current: () => request<Task | null>("/tasks/current"),
    cancel: (id: string) => request<unknown>(`/tasks/${id}`, { method: "DELETE" }),
    goto: (body: { target_id?: string; coord?: { ra_deg: number; dec_deg: number }; center?: boolean }) =>
      post<Task>("/tasks/goto", body),
    polarAlign: (body: { points?: number; separation_deg?: number; exposure_s?: number } = {}) =>
      post<Task>("/tasks/polar-align", body),
    polarRefine: () => post<PolarError>("/tasks/polar-align/refine"),
    autofocus: (body: { steps?: number; step_size?: number } = {}) => post<Task>("/tasks/autofocus", body),
    capture: (body: { count: number; exposure_s: number; gain?: number; dither_every?: number }) =>
      post<Task>("/tasks/capture", body),
  },
};
