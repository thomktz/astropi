import { useState } from "react";
import { api } from "../lib/api";
import type { FrameSummary } from "../lib/types";
import { Field } from "./Field";
import { Modal } from "./Modal";

/**
 * The frame a capture just produced, nearly the size of the window.
 *
 * A capture is a thing you stop and look at, so it opens over the display
 * rather than replacing it - the live view behind carries on showing the
 * sky. Rendered once, at the top, because it can be started from two
 * places: the camera panel and the button under the display.
 */
export function CaptureOverlay({
  frame,
  onClose,
}: {
  frame: FrameSummary;
  onClose: () => void;
}) {
  const [stretch, setStretch] = useState(true);

  return (
    <Modal
      size="full"
      title="Captured frame"
      subtitle={`${frame.duration_s}s · ${frame.kind} · ${frame.width}×${frame.height}`}
      onClose={onClose}
    >
      <div className="capture-image">
        <img
          src={api.camera.previewUrl(frame.id, { stretch, maxDimension: 2200 })}
          alt={`Captured frame, ${frame.duration_s} second exposure`}
          draggable={false}
        />
      </div>
      <div className="spread">
        <Field label="Gain" value={String(frame.metadata.gain ?? "--")} />
        <Field label="Offset" value={String(frame.metadata.offset ?? "--")} />
        <Field label="Binning" value={String(frame.metadata.binning ?? 1)} />
        <Field
          label="Sensor"
          value={
            typeof frame.metadata.sensor_temp_c === "number"
              ? `${frame.metadata.sensor_temp_c.toFixed(1)}°C`
              : "--"
          }
        />
      </div>
      <div className="row modal-actions">
        <button
          className="ghost"
          aria-pressed={stretch}
          onClick={() => setStretch((on) => !on)}
          title="Screen stretch - a raw frame is black without it"
        >
          {stretch ? "stretched" : "linear"}
        </button>
        <button className="primary" onClick={onClose}>
          Done
        </button>
      </div>
    </Modal>
  );
}
