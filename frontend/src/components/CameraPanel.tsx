import { Fragment, useEffect, useRef, useState } from "react";
import {
  type CameraSetting,
  connectCamera,
  detectCamera,
  disconnectCamera,
  getConnectionStatus,
  previewUrl,
  readSettings,
  shootPhoto,
} from "../api/cameraApi";

// Confirmed by hand: the Zf denies remote writes to these regardless of what
// the widget's own `readonly` flag says (that flag only reliably reflects
// the AF/MF switch, not these three) - see backend/README.md.
const KNOWN_NOT_SETTABLE = new Set(["iso", "shutterspeed", "exposurecompensation"]);

// The backend now holds a persistent gphoto2 session (~80-150ms/frame)
// instead of spawning a CLI process per request (~500-600ms), so this can
// run much faster than the original 1fps.
const PREVIEW_INTERVAL_MS = 200;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function CameraPanel() {
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState(false);
  const [lastCaptureUrl, setLastCaptureUrl] = useState<string | null>(null);
  const [isShooting, setIsShooting] = useState(false);
  const [detectResult, setDetectResult] = useState<{ connected: boolean; model: string | null } | null>(null);
  const [isDetecting, setIsDetecting] = useState(false);
  const [settings, setSettings] = useState<CameraSetting[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const isShootingRef = useRef(false);
  const isConnectedRef = useRef(false);

  useEffect(() => {
    isConnectedRef.current = isConnected;
  }, [isConnected]);

  const refreshSettings = () => {
    readSettings()
      .then(setSettings)
      .catch(() => {
        // camera busy/disconnected - leave the last known settings displayed
      });
  };

  useEffect(() => {
    getConnectionStatus().then((connected) => {
      setIsConnected(connected);
      if (connected) refreshSettings();
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let currentObjectUrl: string | null = null;

    // Self-pacing loop: never fires the next request until the previous one
    // finished, so a slow/stuck response can't pile up overlapping fetches -
    // and skips entirely while disconnected or a Shoot is in flight, since
    // the camera can only serve one PTP session at a time (and holding a
    // session open while idle drains the camera's battery for nothing).
    async function loop() {
      while (!cancelled) {
        if (isConnectedRef.current && !isShootingRef.current) {
          try {
            const res = await fetch(previewUrl(Date.now()));
            if (res.ok) {
              const blob = await res.blob();
              const url = URL.createObjectURL(blob);
              if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl);
              currentObjectUrl = url;
              if (!cancelled) {
                setPreviewSrc(url);
                setPreviewError(false);
              }
            } else if (!cancelled) {
              setPreviewError(true);
            }
          } catch {
            if (!cancelled) setPreviewError(true);
          }
        }
        await sleep(PREVIEW_INTERVAL_MS);
      }
    }

    void loop();
    return () => {
      cancelled = true;
      if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl);
    };
  }, []);

  const handleConnect = async () => {
    setIsConnecting(true);
    try {
      const result = await connectCamera();
      setIsConnected(result.connected);
      if (result.connected) refreshSettings();
    } catch {
      setIsConnected(false);
    } finally {
      setIsConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setIsConnecting(true);
    try {
      await disconnectCamera();
    } finally {
      setIsConnected(false);
      setSettings([]);
      setPreviewSrc(null);
      setIsConnecting(false);
    }
  };

  const handleShoot = async () => {
    isShootingRef.current = true;
    setIsShooting(true);
    try {
      const url = await shootPhoto();
      setLastCaptureUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return url;
      });
      refreshSettings();
    } catch {
      // capture failed (camera disconnected, PTP busy, etc.) - leave last frame as-is
    } finally {
      isShootingRef.current = false;
      setIsShooting(false);
    }
  };

  const handleDetect = async () => {
    setIsDetecting(true);
    try {
      setDetectResult(await detectCamera());
    } finally {
      setIsDetecting(false);
    }
  };

  return (
    <div className="panel">
      <h2>Camera</h2>
      <div className="live-view">
        {previewSrc && (
          <img src={previewSrc} alt="Live preview" className={previewError ? "stale" : undefined} />
        )}
        {!previewSrc && (
          <div className="live-view-placeholder">
            {isConnected ? "No live preview - is the camera connected?" : "Camera disconnected"}
          </div>
        )}
      </div>

      {lastCaptureUrl && (
        <div className="last-capture">
          <img src={lastCaptureUrl} alt="Last capture" />
        </div>
      )}

      <div className="camera-buttons">
        {isConnected ? (
          <button onClick={() => void handleDisconnect()} disabled={isConnecting}>
            {isConnecting ? "Disconnecting…" : "Disconnect"}
          </button>
        ) : (
          <button onClick={() => void handleConnect()} disabled={isConnecting}>
            {isConnecting ? "Connecting…" : "Connect"}
          </button>
        )}
        <button onClick={() => void handleDetect()} disabled={isDetecting}>
          {isDetecting ? "Detecting…" : "Detect Camera"}
        </button>
      </div>

      <div className="camera-buttons">
        <button onClick={() => void handleShoot()} disabled={isShooting || !isConnected}>
          {isShooting ? "Shooting…" : "Shoot"}
        </button>
      </div>

      {detectResult && (
        <p className="object-meta camera-detect-result">
          {detectResult.connected ? `USB present: ${detectResult.model}` : "No camera detected on USB"}
        </p>
      )}

      {settings.length > 0 && (
        <dl className="camera-settings">
          {settings.map((s) => (
            <Fragment key={s.key}>
              <dt>{s.label}</dt>
              <dd className={KNOWN_NOT_SETTABLE.has(s.key) ? "setting-locked" : undefined}>
                {s.value}
                {KNOWN_NOT_SETTABLE.has(s.key) && " (camera-only)"}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}
