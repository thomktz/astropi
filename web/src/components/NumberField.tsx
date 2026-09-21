/**
 * A number field you can actually type in.
 *
 * Uncontrolled while focused, and committed on blur or Enter. The obvious
 * controlled version - clamping and parsing on every keystroke - makes the
 * field unusable: clearing it to type a new value hands `Math.max(min, 0)`
 * straight back, so the box refills with the minimum before the second
 * keystroke arrives and you end up typing into the middle of it.
 *
 * Keyed on the committed value so the field re-syncs when the value
 * changes from elsewhere, without fighting whatever is being typed.
 */
export function NumberField({
  label,
  title,
  value,
  min,
  max,
  step,
  placeholder,
  disabled,
  suffix,
  onCommit,
}: {
  label?: string;
  title?: string;
  value: number | null;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
  disabled?: boolean;
  suffix?: string;
  onCommit: (value: number | null) => void;
}) {
  const commit = (raw: string) => {
    const text = raw.trim();
    if (text === "") {
      onCommit(null);
      return;
    }
    // A comma is a decimal separator in plenty of locales, and parseFloat
    // would stop at it and read 0,5 as 0.
    const parsed = parseFloat(text.replace(",", "."));
    if (Number.isNaN(parsed)) return;
    const clamped = Math.min(max ?? Infinity, Math.max(min ?? -Infinity, parsed));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <label style={{ flex: "1 1 80px" }} title={title}>
      {label && <span className="label">{label}</span>}
      <span className="number-field">
        <input
          key={value ?? "empty"}
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          placeholder={placeholder}
          disabled={disabled}
          defaultValue={value ?? ""}
          onBlur={(event) => commit(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.currentTarget.blur();
          }}
        />
        {suffix && <span className="number-suffix">{suffix}</span>}
      </span>
    </label>
  );
}
