import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDms, formatHms, temperature } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";

const MIN_SCALE = 1;
const MAX_SCALE = 8;

/**
 * The frame, filling the screen.
 *
 * Zoom and pan are not decoration. Judging focus and star roundness means
 * looking at stars at their real pixel size, and at a 26-megapixel sensor
 * scaled into a viewport a star is a fraction of a screen pixel - so the
 * only way to see whether the rig is working is to magnify.
 */
export function Viewer({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const frames = useQuery({
    queryKey: ["frames"],
    queryFn: api.camera.frames,
    // The socket announces each new frame; this only catches the case where
    // it was missed, so it can be slow.
    refetchInterval: 30_000,
  });

  // Refetch the moment a frame is announced. Without this the viewer lags
  // the rig by up to a poll interval, which during a centring loop or an
  // imaging run means watching stale sky.
  useEffect(() => {
    if (telemetry.frameSeq === 0) return;
    queryClient.invalidateQueries({ queryKey: ["frames"] });
  }, [telemetry.frameSeq, queryClient]);

  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [stretch, setStretch] = useState(true);
  const [isDragging, setDragging] = useState(false);
  const dragOrigin = useRef<{ x: number; y: number } | null>(null);

  const latest = frames.data?.[0];
  const frameCount = telemetry.camera?.state;

  // A newly captured frame is a new image; keeping the old pan would leave
  // the view parked on a part of the sensor the eye did not choose.
  //
  // Adjusted during render rather than in an effect: an effect would paint
  // the new frame at the old zoom for one frame before correcting it.
  const [shownFrameId, setShownFrameId] = useState(latest?.id);
  if (latest?.id !== shownFrameId) {
    setShownFrameId(latest?.id);
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }

  const reset = useCallback(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  const zoomBy = useCallback((factor: number) => {
    setScale((current) => {
      const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, current * factor));
      if (next === MIN_SCALE) setOffset({ x: 0, y: 0 });
      return next;
    });
  }, []);

  const onPointerDown = (event: React.PointerEvent) => {
    if (scale <= 1) return;
    dragOrigin.current = { x: event.clientX - offset.x, y: event.clientY - offset.y };
    setDragging(true);
    (event.target as HTMLElement).setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: React.PointerEvent) => {
    if (!dragOrigin.current) return;
    setOffset({ x: event.clientX - dragOrigin.current.x, y: event.clientY - dragOrigin.current.y });
  };

  const onPointerUp = () => {
    dragOrigin.current = null;
    setDragging(false);
  };

  const solve = telemetry.lastSolve;
  const camera = telemetry.camera;

  // Nothing to say yet: an empty bar would just be a stray box over the
  // frame, which is the one thing this layout is trying to keep clear.
  const hasMeta = latest != null || camera?.sensor_c != null || solve != null;

  return (
    <div className="viewer">
      <div
        className="viewer-stage"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={() => (scale > 1 ? reset() : zoomBy(4))}
        style={{ cursor: scale > 1 ? (isDragging ? "grabbing" : "grab") : "default" }}
      >
        {latest ? (
          <img
            className="viewer-image"
            src={`${api.camera.previewUrl(latest.id)}?stretch=${stretch}`}
            alt={`Frame, ${latest.duration_s} second exposure`}
            draggable={false}
            style={{
              transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
              // Nearest-neighbour once magnified: smoothing invents star
              // shapes, and star shape is the thing being inspected.
              imageRendering: scale > 2 ? "pixelated" : "auto",
            }}
          />
        ) : (
          <div className="viewer-empty">
            <p>{frameCount === "exposing" ? "Exposing…" : "No frame yet"}</p>
            <p className="small faint">Capture one from the camera panel.</p>
          </div>
        )}
      </div>

      <div className="viewer-controls">
        <button className="ghost" onClick={() => zoomBy(1 / 1.6)} disabled={scale <= MIN_SCALE} aria-label="Zoom out">
          &minus;
        </button>
        <button className="ghost mono" onClick={reset} title="Fit to screen">
          {scale === 1 ? "fit" : `${scale.toFixed(1)}×`}
        </button>
        <button className="ghost" onClick={() => zoomBy(1.6)} disabled={scale >= MAX_SCALE} aria-label="Zoom in">
          +
        </button>
        <button
          className="ghost"
          onClick={() => setStretch((on) => !on)}
          aria-pressed={stretch}
          title="Screen stretch - raw astronomical frames are black without it"
        >
          {stretch ? "stretched" : "linear"}
        </button>
      </div>

      {/*
        Where the mount says it is pointing, over the image rather than in
        the top bar. It belongs with the picture it describes, and the bar
        is for things that need watching, not for a reference readout.
      */}
      {telemetry.mount && (
        <div className="viewer-pointing small mono" title="Where the mount reports it is pointing">
          <span>{formatHms(telemetry.mount.ra_deg)}</span>
          <span>{formatDms(telemetry.mount.dec_deg)}</span>
        </div>
      )}

      {hasMeta && (
      <div className="viewer-meta small mono">
        {latest && (
          <span>
            {latest.duration_s}s
            {typeof latest.metadata.gain === "number" && ` · gain ${latest.metadata.gain}`}
            {` · ${latest.width}×${latest.height}`}
          </span>
        )}
        {camera?.sensor_c != null && <span>{temperature(camera.sensor_c)}</span>}
        {solve?.success && <span>{solve.stars_detected} stars solved</span>}
        {solve && !solve.success && <span className="poor">solve failed</span>}
      </div>
      )}
    </div>
  );
}
