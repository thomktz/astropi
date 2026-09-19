import type { TrackingStatus } from "../api/types";

interface TrackingPanelProps {
  status: TrackingStatus;
  onPark: () => void;
}

export function TrackingPanel({ status, onPark }: TrackingPanelProps) {
  return (
    <div className="panel">
      <h2>Mount</h2>
      <dl>
        <dt>State</dt>
        <dd className={`state state-${status.state}`}>{status.state}</dd>
        <dt>Target</dt>
        <dd>{status.target?.name ?? "—"}</dd>
        <dt>RA / Dec</dt>
        <dd>
          {status.raDeg.toFixed(2)}° / {status.decDeg.toFixed(2)}°
        </dd>
      </dl>
      <button onClick={onPark} disabled={status.state === "parked"}>
        Park
      </button>
    </div>
  );
}
