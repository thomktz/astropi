import type { CameraControl } from "../lib/types";
import { Field } from "./Field";
import { NumberField } from "./NumberField";
import { Switch } from "./Switch";

/**
 * One driver control, rendered from what the driver said about it.
 *
 * Nothing here knows what gain or a dew heater is. The backend advertises
 * a name, a range and whether it can be written; a boolean becomes a
 * switch, a number becomes a field with those limits, and anything
 * read-only becomes a readout. A camera that grows a control grows a
 * widget for it without this file changing.
 */
export function ControlField({
  control,
  disabled,
  tone,
  onSet,
}: {
  control: CameraControl;
  disabled?: boolean;
  tone?: string;
  onSet: (name: string, value: number) => void;
}) {
  // Units like "%" or "°C" sit inside the box; "e-/ADU steps" would not
  // fit, so it goes to the hover text with the rest of the explanation.
  const inline = control.unit != null && control.unit.length <= 2;
  const hint = [control.description, inline ? null : control.unit].filter(Boolean).join(" · ");

  if (!control.writable) {
    return <Field label={control.label} value={formatValue(control)} tone={tone} />;
  }

  if (control.kind === "boolean") {
    return (
      <Switch
        checked={(control.value ?? 0) >= 0.5}
        label={control.label}
        disabled={disabled}
        onChange={(next) => onSet(control.name, next ? 1 : 0)}
      />
    );
  }

  return (
    <NumberField
      label={control.label}
      title={hint || undefined}
      value={control.value}
      min={control.minimum ?? undefined}
      max={control.maximum ?? undefined}
      step={control.step}
      suffix={inline ? (control.unit ?? undefined) : undefined}
      placeholder={control.default == null ? undefined : String(control.default)}
      disabled={disabled}
      onCommit={(next) => next != null && onSet(control.name, next)}
    />
  );
}

/** Decimals follow the control's own step, so 0.1°C does not print as 12. */
function formatValue(control: CameraControl): string {
  if (control.value == null) return "--";
  const decimals = control.step < 1 ? 1 : 0;
  return `${control.value.toFixed(decimals)}${control.unit ?? ""}`;
}
