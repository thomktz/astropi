import { useState } from "react";
import type { Telemetry } from "../../lib/useTelemetry";
import { PolarAlignModal } from "../PolarAlignModal";

/**
 * Polar alignment: one button, into the modal that does the work.
 *
 * Deliberately nothing here that moves the mount. There was a three-point
 * sweep that slewed by itself to fixed hour angles, which is the opposite
 * of what a rig behind a window needs - only the operator knows where the
 * window has sky. The modal measures from frames taken wherever the mount
 * was put by hand.
 */
export function AlignPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <div className="stack">
        <button className="primary" disabled={busy} onClick={() => setOpen(true)}>
          Polar alignment…
        </button>
        <p className="small dim" style={{ margin: 0 }}>
          Take a frame where the mount is, move it however your window allows, take another - as many
          as you like - then follow the arrows on the live view. The mount only moves when you nudge it.
        </p>
      </div>
      {open && <PolarAlignModal telemetry={telemetry} busy={busy} onClose={() => setOpen(false)} />}
    </>
  );
}
