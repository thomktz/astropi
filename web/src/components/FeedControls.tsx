import type { ReactNode } from "react";

/**
 * What to do with a sensor's feed: stop it, take one frame, or run it.
 *
 * The same three verbs for the imaging sensor and the guide sensor,
 * because they are the same three decisions. The main one carries a fourth
 * - a real capture - which is the only thing the two sensors genuinely do
 * differently.
 *
 * Off and live are separate buttons rather than one toggle: a toggle has
 * to be read before you know which way it will go, and in the dark, on a
 * small inset, that is a click you have to think about.
 */
export function FeedControls({
  live,
  refreshing,
  disabled,
  disabledReason,
  onOff,
  onLive,
  onRefresh,
  children,
}: {
  live: boolean;
  refreshing?: boolean;
  disabled?: boolean;
  /** Shown instead of each button's own hover text when disabled. */
  disabledReason?: string;
  onOff: () => void;
  onLive: () => void;
  onRefresh: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="feed-controls">
      <button
        className="ghost"
        aria-pressed={!live}
        disabled={disabled}
        onClick={onOff}
        title={disabledReason ?? "Stop the loop and leave the sensor alone"}
      >
        off
      </button>
      <button
        className="ghost"
        disabled={disabled || refreshing}
        onClick={onRefresh}
        title={disabledReason ?? "One frame now, without starting the loop"}
      >
        {refreshing ? "…" : "refresh"}
      </button>
      <button
        className="ghost"
        aria-pressed={live}
        disabled={disabled}
        onClick={onLive}
        title={disabledReason ?? "Keep taking frames, so the view follows the sky"}
      >
        live
      </button>
      {children}
    </div>
  );
}
