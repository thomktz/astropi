import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useCaptureSettings } from "../lib/captureSettings";
import { formatDms, formatHms, temperature } from "../lib/format";
import type { FrameSummary } from "../lib/types";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote } from "./Field";

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
export function Viewer({
  telemetry,
  busy,
  onOpenGuiding,
  onCaptured,
}: {
  telemetry: Telemetry;
  busy: boolean;
  onOpenGuiding: () => void;
  onCaptured: (frame: FrameSummary) => void;
}) {
  const queryClient = useQueryClient();
  // One endpoint decides what to show - the live preview or the last stored
  // frame, whichever is newer - rather than the client comparing timestamps
  // across two sources and getting it subtly wrong during a capture run.
  const view = useQuery({
    queryKey: ["camera-view"],
    queryFn: api.camera.view,
    // The socket announces each new frame; this only catches a missed one.
    refetchInterval: 10_000,
  });

  // Refetch the moment a frame is announced. Without this the viewer lags
  // the rig by up to a poll interval, which during a centring loop or an
  // imaging run means watching stale sky.
  useEffect(() => {
    if (telemetry.frameSeq === 0) return;
    queryClient.invalidateQueries({ queryKey: ["camera-view"] });
  }, [telemetry.frameSeq, queryClient]);

  // Cached under the same key the action bar uses, so this is a read of
  // what it already fetched rather than a second request.
  const previewConfig = useQuery({
    queryKey: ["camera-preview"],
    queryFn: api.camera.preview,
    retry: false,
  });

  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [stretch, setStretch] = useState(true);
  const [isDragging, setDragging] = useState(false);
  const dragOrigin = useRef<{ x: number; y: number } | null>(null);

  const latest = view.data;
  const frameCount = telemetry.camera?.state;

  // A newly captured frame is a new image; keeping the old pan would leave
  // the view parked on a part of the sensor the eye did not choose.
  //
  // Adjusted during render rather than in an effect: an effect would paint
  // the new frame at the old zoom for one frame before correcting it.
  // A live preview replaces itself constantly; resetting the zoom on
  // every frame would make it impossible to inspect anything. Only a
  // change of source counts as a new image.
  const identity = latest ? `${latest.source}:${latest.frame_id ?? "live"}` : null;
  const [shownFrameId, setShownFrameId] = useState(identity);
  if (identity !== shownFrameId) {
    setShownFrameId(identity);
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
  const age = useFrameAge(latest?.captured_at ?? latest?.stored_at ?? null);

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
            src={api.camera.viewUrl(latest.captured_at ?? latest.stored_at ?? 0, stretch)}
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
            <p className="small faint">Turn on the live view, or capture one.</p>
          </div>
        )}
      </div>

      {/*
        The guide sensor, inset over the main frame. The Duo has two chips
        looking through the same optics and both matter at once: the main
        one for framing, the guide one for whether there is still a star to
        hold on to. Switching panels to find that out means not seeing the
        other while you look.
      */}
      {telemetry.system?.devices?.guide_camera != null && (
        <GuideInset telemetry={telemetry} onOpen={onOpenGuiding} />
      )}

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

      {/*
        The three things you do to the picture, under the picture. They
        were a panel away, which meant opening a drawer over the display
        to take a look through the telescope - and the drawer covers the
        thing you opened it to see.
      */}
      <ViewerActions busy={busy} onCaptured={onCaptured} />

      {hasMeta && (
      <div className="viewer-meta small mono">
        {latest && (
          <span>
            {/*
              How long ago this frame landed, counted here and ticking.
              A live view of a tracked field looks identical frame to
              frame, so without this there is no way to tell a running
              loop from a stalled one by looking at the picture.
            */}
            {latest.source === "preview" &&
              (previewConfig.data?.enabled ? (
                <>
                  <span className="live-tag">live</span>
                  {age != null && ` ${age}s ago \u00b7 `}
                </>
              ) : (
                // A single frame from the refresh button. Calling that
                // "live" when nothing is following it would be a lie that
                // gets worse by one second per second.
                <>{age != null && `preview ${age}s ago \u00b7 `}</>
              ))}
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

/**
 * Live view on or off, one frame now, and a capture.
 *
 * The live view's own settings live in the camera panel; these are the
 * verbs, not the settings.
 */
function ViewerActions({
  busy,
  onCaptured,
}: {
  busy: boolean;
  onCaptured: (frame: FrameSummary) => void;
}) {
  const queryClient = useQueryClient();
  const { exposure_s: exposure, gain } = useCaptureSettings();

  const preview = useQuery({
    queryKey: ["camera-preview"],
    queryFn: api.camera.preview,
    retry: false,
  });

  const setPreview = useMutation({
    mutationFn: api.camera.setPreview,
    onSuccess: (next) => {
      queryClient.setQueryData(["camera-preview"], next);
      queryClient.invalidateQueries({ queryKey: ["camera-view"] });
    },
  });

  const refresh = useMutation({
    mutationFn: api.camera.previewFrame,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["camera-view"] }),
  });

  const capture = useMutation({
    mutationFn: () => api.camera.expose(exposure, gain == null ? {} : { gain }),
    onSuccess: (frame) => {
      onCaptured(frame);
      queryClient.invalidateQueries({ queryKey: ["camera-view"] });
    },
  });

  // No camera, so no verbs to offer.
  if (preview.isError) return null;

  const live = preview.data?.enabled ?? false;
  const working = refresh.isPending || capture.isPending;

  return (
    <div className="viewer-actions">
      <button
        className="ghost"
        aria-pressed={live}
        disabled={setPreview.isPending}
        onClick={() => setPreview.mutate({ enabled: !live })}
        title={
          live
            ? `A new frame every ${preview.data?.period_s}s. Click to stop.`
            : "Keep taking short frames, so the display follows the sky"
        }
      >
        <span className={`dot ${live ? "live" : ""}`} />
        live
      </button>
      <button
        className="ghost"
        disabled={working || busy}
        onClick={() => refresh.mutate()}
        title="One live-view frame now, without starting the loop"
      >
        {refresh.isPending ? "…" : "refresh"}
      </button>
      <button
        className="primary"
        disabled={working || busy}
        onClick={() => capture.mutate()}
        title="A real exposure, kept in the frame store and shown full size"
      >
        {busy ? "Rig busy" : capture.isPending ? "Exposing…" : `Capture ${exposure}s`}
      </button>
      <ErrorNote error={refresh.error ?? capture.error} />
    </div>
  );
}

/**
 * Seconds since a frame arrived, ticking.
 *
 * Counted in the browser from the timestamp the frame carries, rather than
 * pushed from the backend: a second-by-second countdown for every viewer
 * is a message per second per browser for something both ends already know.
 */
function useFrameAge(capturedAt: number | null): number | null {
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  if (capturedAt == null) return null;
  return Math.max(0, Math.round(now - capturedAt));
}

/** How often to refetch the guide frame if no event announced one. */
const GUIDE_FALLBACK_MS = 8_000;

/**
 * The guide camera, small, in the corner of the main display.
 *
 * Its own loop and its own switch, at the same cadence question as the
 * main display but answered separately - the two sensors are independent,
 * and neither starts exposing because a dashboard was opened.
 */
function GuideInset({ telemetry, onOpen }: { telemetry: Telemetry; onOpen: () => void }) {
  const queryClient = useQueryClient();
  const [missing, setMissing] = useState(false);
  const [tick, setTick] = useState(0);

  const settings = useQuery({
    queryKey: ["guiding-settings"],
    queryFn: api.guiding.settings,
    retry: false,
  });

  const update = useMutation({
    mutationFn: api.guiding.updateSettings,
    onSuccess: (next) => queryClient.setQueryData(["guiding-settings"], next),
  });

  // The socket announces each guide frame, so the count itself is the
  // cache key; the timer only catches one that was missed.
  useEffect(() => {
    const timer = setInterval(() => setTick((count) => count + 1), GUIDE_FALLBACK_MS);
    return () => clearInterval(timer);
  }, []);

  const stamp = `${telemetry.guideFrameSeq}-${tick}`;
  const state = telemetry.guideState;
  const guiding = state !== "stopped";
  // Guiding produces its own frames, so the idle loop is beside the point
  // while it runs - the sub-display is live either way.
  const live = guiding || (settings.data?.preview_enabled ?? false);

  return (
    <div className="guide-inset">
      <button
        className="guide-inset-image"
        onClick={onOpen}
        title="Guide camera - open the guiding panel"
      >
        {/*
          The image stays mounted when there is nothing to show, hidden
          behind the placeholder. Swapping it out for the placeholder
          instead left nothing to fire `onLoad`, so the first 404 - the
          normal state with the loop off - was permanent, and switching
          the guide view on afterwards showed "waiting" for ever.
        */}
        <img
          className={missing ? "hidden" : undefined}
          src={`/api/guiding/frame.png?max_dimension=420&t=${stamp}`}
          alt="Guide camera"
          draggable={false}
          onError={() => setMissing(true)}
          onLoad={() => setMissing(false)}
        />
        {missing && (
          <span className="guide-inset-empty small faint">
            {live ? "waiting\u2026" : "guide view off"}
          </span>
        )}
      </button>
      <div className="guide-inset-label small mono">
        <button
          className="ghost"
          aria-pressed={live}
          disabled={guiding || update.isPending || settings.isError}
          onClick={() => update.mutate({ preview_enabled: !live })}
          title={
            guiding
              ? "Guiding is producing frames of its own"
              : live
                ? `A guide frame every ${settings.data?.preview_period_s}s. Click to stop.`
                : "Keep the guide view live while the loop is stopped"
          }
        >
          <span className={`dot ${live ? "live" : ""}`} />
          guide
        </button>
        {guiding && <span className="faint">{state}</span>}
      </div>
    </div>
  );
}
