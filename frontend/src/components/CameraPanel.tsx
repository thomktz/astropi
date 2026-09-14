import type { CameraStatus } from "../api/types";

interface CameraPanelProps {
  status: CameraStatus;
  onCapture: () => void;
}

export function CameraPanel({ status, onCapture }: CameraPanelProps) {
  return (
    <div className="panel">
      <h2>Camera</h2>
      <div className="live-view">
        {status.lastFrameUrl ? (
          <img src={status.lastFrameUrl} alt="Last captured frame" />
        ) : (
          <div className="live-view-placeholder">No frame yet</div>
        )}
      </div>
      <dl>
        <dt>ISO</dt>
        <dd>{status.iso}</dd>
        <dt>Shutter</dt>
        <dd>{status.shutterSpeed}</dd>
        <dt>Aperture</dt>
        <dd>{status.aperture}</dd>
      </dl>
      <button onClick={onCapture} disabled={status.isShooting}>
        {status.isShooting ? "Shooting…" : "Capture"}
      </button>
    </div>
  );
}
