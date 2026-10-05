import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { ErrorNote } from "./Field";

export type NudgeDirection = "north" | "south" | "east" | "west";

/**
 * The Target panel's nudge keypad, for use elsewhere: a row of step sizes
 * and a compass of buttons, each moving one axis by that angle.
 *
 * Unparks first where it has to, and locks every button while a move is
 * under way - a large step is seconds of a mount quietly turning, and
 * without the lock the only feedback was a button looking as it did.
 */
export function NudgePad({
  steps,
  step,
  onStep,
  parked,
  disabled,
  onMoved,
}: {
  steps: { degrees: number; label: string }[];
  step: number;
  onStep: (degrees: number) => void;
  parked: boolean;
  disabled?: boolean;
  /** Called after each completed move, with what was asked for. */
  onMoved?: (direction: NudgeDirection, degrees: number) => void;
}) {
  const queryClient = useQueryClient();
  const nudge = useMutation({
    mutationFn: async ({ direction, degrees }: { direction: NudgeDirection; degrees: number }) => {
      if (parked) await api.mount.unpark();
      return api.mount.nudge(direction, degrees);
    },
    onSuccess: (_, { direction, degrees }) => onMoved?.(direction, degrees),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["mount"] }),
  });
  const moving = nudge.isPending ? nudge.variables.direction : null;

  const button = (direction: NudgeDirection, label: string) => (
    <button
      disabled={disabled || moving !== null}
      aria-label={`Nudge ${direction}`}
      aria-busy={moving === direction}
      className={moving === direction ? "primary" : undefined}
      onClick={() => nudge.mutate({ direction, degrees: step })}
    >
      {label}
    </button>
  );

  return (
    <>
      <div className="row quick">
        {steps.map((option) => (
          <button
            key={option.label}
            className="ghost"
            aria-pressed={step === option.degrees}
            onClick={() => onStep(option.degrees)}
            title={`Move ${option.label} per press`}
          >
            {option.label}
          </button>
        ))}
      </div>
      <div className="keypad">
        <span className="spacer" />
        {button("north", "N")}
        <span className="spacer" />
        {button("west", "W")}
        <span className="spacer" />
        {button("east", "E")}
        <span className="spacer" />
        {button("south", "S")}
        <span className="spacer" />
      </div>
      <ErrorNote error={nudge.error} />
    </>
  );
}
