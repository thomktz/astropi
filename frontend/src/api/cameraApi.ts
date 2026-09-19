const BACKEND_URL = `http://${window.location.hostname}:8000`;

export interface CameraDetectResult {
  connected: boolean;
  model: string | null;
}

export function previewUrl(cacheBust: number): string {
  return `${BACKEND_URL}/camera/preview?t=${cacheBust}`;
}

export async function detectCamera(): Promise<CameraDetectResult> {
  try {
    const res = await fetch(`${BACKEND_URL}/camera/detect`);
    if (!res.ok) return { connected: false, model: null };
    return await res.json();
  } catch {
    return { connected: false, model: null };
  }
}

export async function shootPhoto(): Promise<string> {
  const res = await fetch(`${BACKEND_URL}/camera/capture`, { method: "POST" });
  if (!res.ok) throw new Error(`Capture failed: ${res.status}`);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

export interface CameraSetting {
  key: string;
  label: string;
  value: string;
  readonly: boolean;
}

export async function readSettings(): Promise<CameraSetting[]> {
  const res = await fetch(`${BACKEND_URL}/camera/settings`);
  if (!res.ok) throw new Error(`Settings read failed: ${res.status}`);
  return res.json();
}

export async function getConnectionStatus(): Promise<boolean> {
  try {
    const res = await fetch(`${BACKEND_URL}/camera/connection`);
    if (!res.ok) return false;
    return (await res.json()).connected;
  } catch {
    return false;
  }
}

export async function connectCamera(): Promise<CameraDetectResult> {
  const res = await fetch(`${BACKEND_URL}/camera/connect`, { method: "POST" });
  if (!res.ok) throw new Error(`Connect failed: ${res.status}`);
  return res.json();
}

export async function disconnectCamera(): Promise<void> {
  await fetch(`${BACKEND_URL}/camera/disconnect`, { method: "POST" });
}
