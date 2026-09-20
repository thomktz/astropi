import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { api } from "../lib/api";
import type { GuideSample } from "../lib/types";

/** Candidates fainter than this are not worth offering as a guide star. */
const MIN_CANDIDATE_SNR = 25;

/**
 * What the guide camera sees, with the loop's own state drawn over it.
 *
 * The frame alone says very little. The frame plus "this is the star I am
 * locked to, this is how far it has wandered, and these are the others I
 * could use instead" is what diagnoses a bad night in one look.
 */
export function GuideView({
  latest,
  running,
}: {
  latest: GuideSample | undefined;
  running: boolean;
}) {
  const queryClient = useQueryClient();
  const container = useRef<HTMLDivElement>(null);

  const info = useQuery({
    queryKey: ["guide-frame"],
    queryFn: api.guiding.frameInfo,
    // While the loop runs it produces a frame every couple of seconds;
    // idle, the image only changes when a preview is taken.
    refetchInterval: running ? 2_000 : false,
    retry: false,
  });

  const preview = useMutation({
    mutationFn: api.guiding.preview,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["guide-frame"] }),
  });

  const lock = useMutation({
    mutationFn: ({ x, y }: { x: number; y: number }) => api.guiding.lock(x, y),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["guide-frame"] }),
  });

  const frame = info.data;
  if (!frame) {
    return (
      <div className="guide-view empty">
        <p className="small faint">No guide frame yet.</p>
        <button className="ghost" disabled={preview.isPending} onClick={() => preview.mutate()}>
          {preview.isPending ? "Exposing…" : "Take one"}
        </button>
      </div>
    );
  }

  // The loop's live positions when it is running, the last stored frame's
  // when it is not.
  const star = running && latest ? { x: latest.star_x, y: latest.star_y } : frame.star;
  const lockPoint = running && latest ? { x: latest.lock_x, y: latest.lock_y } : frame.lock;

  const onClick = (event: React.MouseEvent) => {
    const bounds = container.current?.getBoundingClientRect();
    if (!bounds) return;
    // Click position back into sensor pixels: the image is letterboxed to
    // fit, so the displayed box is not the element's box.
    const scale = Math.min(bounds.width / frame.width, bounds.height / frame.height);
    const drawnWidth = frame.width * scale;
    const drawnHeight = frame.height * scale;
    const x = (event.clientX - bounds.left - (bounds.width - drawnWidth) / 2) / scale;
    const y = (event.clientY - bounds.top - (bounds.height - drawnHeight) / 2) / scale;
    if (x < 0 || y < 0 || x > frame.width || y > frame.height) return;
    lock.mutate({ x, y });
  };

  return (
    <div className="stack">
      <div
        className="guide-view"
        ref={container}
        onClick={onClick}
        title="Click a star to guide on it"
      >
        {/*
          Keyed on when the frame was taken, not on the clock. The image
          then refetches exactly when there is a new one to fetch,
          instead of on every render.
        */}
        <img
          src={api.guiding.frameUrl(frame.captured_at * 1000)}
          alt="Guide camera"
          draggable={false}
        />
        <svg viewBox={`0 0 ${frame.width} ${frame.height}`} preserveAspectRatio="xMidYMid meet">
          {/* Candidates worth switching to, so the click target is visible. */}
          {frame.candidates
            .filter((candidate) => candidate.snr >= MIN_CANDIDATE_SNR)
            .map((candidate) => (
              <circle
                key={`${candidate.x}-${candidate.y}`}
                cx={candidate.x}
                cy={candidate.y}
                r={frame.search_radius_px * 0.5}
                className="candidate"
              />
            ))}

          {lockPoint && (
            <>
              {/* The search radius: outside this, the lock is lost. */}
              <circle
                cx={lockPoint.x}
                cy={lockPoint.y}
                r={frame.search_radius_px}
                className="search"
              />
              <path
                d={`M${lockPoint.x - 16},${lockPoint.y} h9 M${lockPoint.x + 7},${lockPoint.y} h9
                    M${lockPoint.x},${lockPoint.y - 16} v9 M${lockPoint.x},${lockPoint.y + 7} v9`}
                className="lock"
              />
            </>
          )}

          {star && lockPoint && (
            // Error vector, exaggerated: a real excursion is a pixel or two
            // and would be invisible drawn true to scale.
            <line
              x1={lockPoint.x}
              y1={lockPoint.y}
              x2={lockPoint.x + (star.x - lockPoint.x) * 8}
              y2={lockPoint.y + (star.y - lockPoint.y) * 8}
              className="error-vector"
            />
          )}

          {star && <circle cx={star.x} cy={star.y} r={4} className="star" />}
        </svg>
      </div>

      <div className="row small">
        <span className="faint">
          {frame.width}&#215;{frame.height} &middot;{" "}
          {frame.candidates.filter((c) => c.snr >= MIN_CANDIDATE_SNR).length} usable stars
        </span>
        {!running && (
          <button
            className="ghost"
            style={{ flex: "0 0 auto" }}
            disabled={preview.isPending}
            onClick={() => preview.mutate()}
          >
            {preview.isPending ? "Exposing…" : "Refresh"}
          </button>
        )}
      </div>
      {lock.error instanceof Error && <div className="error">{lock.error.message}</div>}
    </div>
  );
}
