import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { useColour } from "../../lib/colour";
import type { LibraryFrame } from "../../lib/types";
import { ErrorNote, Section } from "../Field";
import { Modal } from "../Modal";

const PATH_KEY = "astropi.library.path";

/**
 * The frames on the SSD, folder by folder, as stretched JPEG previews.
 *
 * Polled while open, so a frame shows up here a few seconds after the
 * session writes it. Previews are made on the Pi the first time they are
 * asked for and kept beside the frames, at low priority so a folder full
 * of them never holds up a capture.
 *
 * Frames and folders can be deleted, each after a confirmation naming
 * what goes. For good: there is no trash on the SSD.
 */
export function LibraryPanel() {
  const [path, setPath] = useState(() => readStored(PATH_KEY) ?? "");
  const [open, setOpen] = useState<LibraryFrame | null>(null);
  const [colour] = useColour();
  const queryClient = useQueryClient();
  const remove = useDelete(() => queryClient.invalidateQueries({ queryKey: ["library"] }));
  const folder = useQuery({
    queryKey: ["library", path],
    queryFn: () => api.library.list(path),
    refetchInterval: 10_000,
    retry: false,
  });

  useEffect(() => writeStored(PATH_KEY, path), [path]);

  // A remembered folder that has gone (renamed, or a different SSD) falls
  // back to the top rather than leaving the panel stuck on an error.
  useEffect(() => {
    if (folder.isError && path) setPath("");
  }, [folder.isError, path]);

  const parts = path ? path.split("/") : [];
  const frames = folder.data?.frames ?? [];
  const dirs = folder.data?.dirs ?? [];

  return (
    <>
      <nav className="crumbs" aria-label="Folder">
        <button className="ghost" onClick={() => setPath("")}>SSD</button>
        {parts.map((part, index) => (
          <span key={index}>
            /{" "}
            <button className="ghost" onClick={() => setPath(parts.slice(0, index + 1).join("/"))}>
              {part}
            </button>
          </span>
        ))}
      </nav>
      <ErrorNote error={folder.error} />

      {dirs.length > 0 && (
        <Section title="Folders">
          <div className="folder-list">
            {dirs.map((dir) => (
              <div key={dir.path} className="folder-row">
                <button className="ghost mono" onClick={() => setPath(dir.path)}>
                  {dir.name}
                </button>
                <button
                  className="ghost small"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(dir.path)}
                  title={`Delete ${dir.name} and everything in it`}
                >
                  Delete
                </button>
              </div>
            ))}
          </div>
        </Section>
      )}

      {frames.length > 0 && (
        <Section title={`${frames.length} frame${frames.length === 1 ? "" : "s"}, newest first`}>
          <div className="thumb-grid">
            {frames.map((frame) => (
              <button key={frame.path} className="thumb" onClick={() => setOpen(frame)} title={frame.name}>
                <img
                  className="library-image"
                  src={api.library.previewUrl(frame, "thumb", colour)}
                  alt={frame.name}
                  loading="lazy"
                  decoding="async"
                />
                <span>{shortName(frame.name)}</span>
              </button>
            ))}
          </div>
        </Section>
      )}

      {folder.isSuccess && dirs.length === 0 && frames.length === 0 && (
        <div className="small faint">Nothing here yet.</div>
      )}

      {path && folder.isSuccess && (
        <div className="row quick">
          <button
            className="danger"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(path, { onSuccess: (gone) => gone && setPath(parts.slice(0, -1).join("/")) })
            }
          >
            Delete this folder
          </button>
        </div>
      )}
      <ErrorNote error={remove.error} />

      {open && (
        <FrameModal
          frame={open}
          frames={frames}
          onSelect={setOpen}
          onClose={() => setOpen(null)}
          onDeleted={(next) => {
            setOpen(next);
            queryClient.invalidateQueries({ queryKey: ["library"] });
          }}
        />
      )}
    </>
  );
}

/**
 * Deleting a frame or folder, after a confirmation every time.
 *
 * The confirmation is built from what the Pi says is there - the count of
 * files, and their size - rather than from what the panel last listed.
 * Resolves to null when the confirmation is declined.
 */
function useDelete(onDone: () => void) {
  return useMutation({
    mutationFn: async (path: string) => {
      const summary = await api.library.summary(path);
      const size =
        summary.bytes >= 1e9
          ? `${(summary.bytes / 1e9).toFixed(1)} GB`
          : `${Math.round(summary.bytes / 1e6)} MB`;
      const what =
        summary.kind === "frame"
          ? `the frame ${path.split("/").pop()} (${size})`
          : `the folder ${path} and everything in it: ` +
            `${summary.files} file${summary.files === 1 ? "" : "s"}` +
            `${summary.frames !== summary.files ? `, ${summary.frames} of them frames` : ""} (${size})`;
      const sure = window.confirm(`Delete ${what}?\n\nThis deletes from the SSD for good. There is no trash.`);
      if (!sure) return null;
      return api.library.remove(path);
    },
    onSuccess: (gone) => {
      if (gone) onDone();
    },
  });
}

/**
 * One frame, large, with the arrow keys stepping through its folder.
 *
 * "Full quality" swaps the preview for the frame at its own resolution,
 * debayered. Off by default: it takes the Pi a few seconds to make the
 * first time, and is megabytes to send. Once loaded it can be looked at
 * pixel for pixel, scrolling to pan.
 */
function FrameModal({
  frame,
  frames,
  onSelect,
  onClose,
  onDeleted,
}: {
  frame: LibraryFrame;
  frames: LibraryFrame[];
  onSelect: (frame: LibraryFrame) => void;
  onClose: () => void;
  onDeleted: (next: LibraryFrame | null) => void;
}) {
  const [full, setFull] = useState(false);
  const [colour, setColour] = useColour();
  const [actual, setActual] = useState(false);
  const [loaded, setLoaded] = useState<string | null>(null);
  const src = api.library.previewUrl(frame, full ? "full" : "large", colour);
  const index = frames.findIndex((f) => f.path === frame.path);
  // Newest first, so "next" is the older one, the way a list is read.
  const newer = index > 0 ? frames[index - 1] : null;
  const older = index >= 0 && index < frames.length - 1 ? frames[index + 1] : null;
  // After a delete, on to the neighbour, the way a list closes up.
  const remove = useDelete(() => onDeleted(older ?? newer));

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft" && newer) onSelect(newer);
      if (event.key === "ArrowRight" && older) onSelect(older);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newer, older, onSelect]);

  return (
    <Modal
      title={frame.name}
      subtitle={`${new Date(frame.modified * 1000).toLocaleString()} · ${(frame.size / 1e6).toFixed(0)} MB`}
      size="full"
      onClose={onClose}
    >
      <div className={`capture-image${full && actual ? " actual" : ""}`}>
        <img className="library-image" src={src} alt={frame.name} onLoad={() => setLoaded(src)} />
      </div>
      <div className="row quick">
        <button
          className="ghost"
          aria-pressed={full}
          onClick={() => setFull(!full)}
          title="The frame at its own resolution. Made on the Pi the first time: a few seconds."
        >
          Full quality
        </button>
        {full && (
          <button
            className="ghost"
            aria-pressed={actual}
            onClick={() => setActual(!actual)}
            title="One frame pixel per screen pixel; scroll to pan"
          >
            1:1
          </button>
        )}
        <button
          className="ghost"
          aria-pressed={colour}
          onClick={() => setColour(!colour)}
          title="Debayer the colour sensor's frames, or show them as grayscale"
        >
          {colour ? "Colour" : "Gray"}
        </button>
        {loaded !== src && <span className="small faint">rendering on the Pi…</span>}
        <span style={{ flex: 1 }} />
        <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate(frame.path)}>
          Delete frame
        </button>
      </div>
      <ErrorNote error={remove.error} />
      <div className="spread">
        <button className="ghost" disabled={!newer} onClick={() => newer && onSelect(newer)}>
          ← Newer
        </button>
        <span className="small dim">
          {index + 1} of {frames.length}
        </span>
        <button className="ghost" disabled={!older} onClick={() => older && onSelect(older)}>
          Older →
        </button>
      </div>
    </Modal>
  );
}

/** The frame number and time are what tell neighbours apart in a grid. */
function shortName(name: string): string {
  const match = name.match(/(\d{8}-\d{6})?_?(\d{4})\.fits?$/i);
  if (!match) return name;
  const time = match[1] ? `${match[1].slice(9, 11)}:${match[1].slice(11, 13)} ` : "";
  return `${time}#${match[2]}`;
}

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // The folder simply will not be remembered.
  }
}
