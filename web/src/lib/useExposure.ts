import { useEffect, useState } from "react";
import type { Telemetry } from "./useTelemetry";

/**
 * How far through its exposure the camera is, counted here.
 *
 * From the start time and the duration the camera reported once, rather
 * than from progress messages: a five minute sub would otherwise cost
 * three hundred messages to every connected browser to say something
 * both ends can work out from two numbers.
 */
export function useExposureFraction(camera: Telemetry["camera"]): number | null {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const exposing = camera?.state === "exposing" && camera.exposure_started_at != null;

  useEffect(() => {
    if (!exposing) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 120);
    return () => clearInterval(timer);
  }, [exposing]);

  if (!camera?.exposure_s || camera.exposure_started_at == null) return null;
  return Math.min(1, Math.max(0, (now - camera.exposure_started_at) / camera.exposure_s));
}

/** Seconds left on the exposure under way, or null when none is. */
export function useExposureRemaining(camera: Telemetry["camera"]): number | null {
  const fraction = useExposureFraction(camera);
  if (fraction == null || !camera?.exposure_s) return null;
  return Math.max(0, camera.exposure_s * (1 - fraction));
}
