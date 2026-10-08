import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
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
 */
export function LibraryPanel() {
  const [path, setPath] = useState(() => readStored(PATH_KEY) ?? "");
  const [open, setOpen] = useState<LibraryFrame | null>(null);
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
              <button key={dir.path} className="ghost mono" onClick={() => setPath(dir.path)}>
                {dir.name}
              </button>
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
                  src={api.library.previewUrl(frame, "thumb")}
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

      {open && <FrameModal frame={open} frames={frames} onSelect={setOpen} onClose={() => setOpen(null)} />}
    </>
  );
}

/** One frame, large, with the arrow keys stepping through its folder. */
function FrameModal({
  frame,
  frames,
  onSelect,
  onClose,
}: {
  frame: LibraryFrame;
  frames: LibraryFrame[];
  onSelect: (frame: LibraryFrame) => void;
  onClose: () => void;
}) {
  const index = frames.findIndex((f) => f.path === frame.path);
  // Newest first, so "next" is the older one, the way a list is read.
  const newer = index > 0 ? frames[index - 1] : null;
  const older = index >= 0 && index < frames.length - 1 ? frames[index + 1] : null;

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
      <div className="capture-image">
        <img className="library-image" src={api.library.previewUrl(frame, "large")} alt={frame.name} />
      </div>
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
