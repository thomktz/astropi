/**
 * A number whose digits roll like an odometer when they change.
 *
 * Only worth it where the value genuinely moves on its own - the mount's
 * altitude and azimuth, which slide continuously while tracking because
 * they are horizon coordinates and the Earth is turning underneath them.
 * A digit that changes without moving reads as a redraw; one that rolls
 * reads as a measurement.
 *
 * Each digit is a column of 0-9 shifted vertically, so only a transform
 * animates and the browser never reflows the readout mid-roll.
 */
export function RollingNumber({
  value,
  decimals = 3,
  suffix = "",
}: {
  value: number | null | undefined;
  decimals?: number;
  suffix?: string;
}) {
  if (value == null || Number.isNaN(value)) {
    return <span className="rolling">--{suffix}</span>;
  }

  const text = value.toFixed(decimals);

  return (
    <span className="rolling" aria-label={`${text}${suffix}`}>
      {text.split("").map((character, index) =>
        character >= "0" && character <= "9" ? (
          // Index is a stable key here: the string's length only changes
          // when the value crosses a digit boundary, and a remount there
          // is what should happen anyway.
          <RollingDigit key={index} digit={Number(character)} />
        ) : (
          <span key={index} aria-hidden="true">
            {character}
          </span>
        ),
      )}
      {suffix && <span aria-hidden="true">{suffix}</span>}
    </span>
  );
}

function RollingDigit({ digit }: { digit: number }) {
  // No state: the first render already carries the right transform, so a
  // readout appearing for the first time sits at its value rather than
  // spinning up from zero, and every later change transitions from
  // wherever it was.
  return (
    <span className="rolling-digit" aria-hidden="true">
      <span className="rolling-stack" style={{ transform: `translateY(${-digit}em)` }}>
        {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((candidate) => (
          <span key={candidate}>{candidate}</span>
        ))}
      </span>
    </span>
  );
}
