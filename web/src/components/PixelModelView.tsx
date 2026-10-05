import type { PixelModelState } from "../lib/types";
import { Hint } from "./Hint";

const SIZE = 150;

/**
 * The guide loop's model, in sensor pixels.
 *
 * Three arrows from the lock point, on the sensor's own axes: where the
 * star drifts in a minute with nothing done, where one RA correction
 * takes it, and where ten Dec steps take it. Cancelling is choosing the
 * RA and Dec amounts whose arrows add up to the reverse of the drift.
 */
export function PixelModelView({
  model,
  raOffset,
  lastDecSteps,
}: {
  model: PixelModelState;
  /** RA rate offset held now, axis arcsec/s. */
  raOffset?: number;
  lastDecSteps?: number;
}) {
  const drift: [number, number] = [model.drift_x ?? 0, model.drift_y ?? 0];
  const ra = model.ra_response ?? model.ra_calibrated;
  const dec = model.dec_response ?? model.dec_calibrated;
  // How many units each arrow shows, so all three are drawable together.
  const raCount = model.ra_unit === "arcsec" ? 10 : 500;
  const decCount = model.dec_unit === "step" ? 10 : 500;
  const arrows: { label: string; v: [number, number]; color: string }[] = [
    { label: "drift / min", v: drift, color: "var(--poor)" },
    { label: `RA ${raCount}${unit(model.ra_unit)}`, v: [ra[0] * raCount, ra[1] * raCount], color: "var(--accent)" },
    { label: `Dec ${decCount}${unit(model.dec_unit)}`, v: [dec[0] * decCount, dec[1] * decCount], color: "var(--fair)" },
  ];
  const longest = Math.max(0.1, ...arrows.map((a) => Math.hypot(...a.v)));
  const scale = (SIZE / 2 - 18) / longest;
  const c = SIZE / 2;

  return (
    <div className="pixel-model">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="pixel-model-plot" role="img" aria-label="Drift and corrections on the sensor">
        <line x1={c} y1="4" x2={c} y2={SIZE - 4} stroke="var(--border)" />
        <line x1="4" y1={c} x2={SIZE - 4} y2={c} stroke="var(--border)" />
        <text x={SIZE - 4} y={c - 3} fontSize="8" textAnchor="end" fill="var(--text-faint)">
          x
        </text>
        <text x={c + 3} y={SIZE - 5} fontSize="8" fill="var(--text-faint)">
          y
        </text>
        {arrows.map((a) => {
          const x2 = c + a.v[0] * scale;
          const y2 = c + a.v[1] * scale;
          const angle = Math.atan2(y2 - c, x2 - c);
          const head = (turn: number) => `${x2 - 6 * Math.cos(angle + turn)},${y2 - 6 * Math.sin(angle + turn)}`;
          return (
            <g key={a.label}>
              <line x1={c} y1={c} x2={x2} y2={y2} stroke={a.color} strokeWidth="2" />
              <polygon points={`${x2},${y2} ${head(0.4)} ${head(-0.4)}`} fill={a.color} />
            </g>
          );
        })}
      </svg>

      <div className="pixel-legend small mono">
        {arrows.map((a) => (
          <span key={a.label} style={{ color: a.color }}>
            &#8594; {a.label}
          </span>
        ))}
      </div>

      <table className="pixel-table mono small">
        <thead>
          <tr>
            <th />
            <th>x →</th>
            <th>y ↓</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr>
            <th>
              Drift
              <Hint>
                Pixels a minute the star moves with no correction, fitted over the last{" "}
                {model.frames ?? "few"} frames with every correction taken into account, ± one
                standard error. Steady drift only: the worm&apos;s swing is its own row. While it is
                being cancelled at a steady rate, the window cannot tell drift from what the
                corrections do, so this leans on the calibration.
              </Hint>
            </th>
            <td>{pm(model.drift_x, model.drift_x_error)}</td>
            <td>{pm(model.drift_y, model.drift_y_error)}</td>
            <td className="faint">px/min</td>
          </tr>
          {model.worm_period_s != null && (
            <tr>
              <th>
                Worm
                <Hint>
                  Half the RA worm&apos;s peak-to-peak swing, along the RA direction on the sensor, ±
                  one standard error. Fitted as a swing of the worm&apos;s period, and cancelled as it
                  comes.
                </Hint>
              </th>
              <td colSpan={2}>
                {model.worm_px == null
                  ? "--"
                  : `${model.worm_px.toFixed(2)} ±${(model.worm_error_px ?? 0).toFixed(2)}`}
              </td>
              <td className="faint">px, {Math.round(model.worm_period_s)} s</td>
            </tr>
          )}
          <tr>
            <th>
              RA 1{unit(model.ra_unit)}
              <Hint>
                Where one {model.ra_unit === "arcsec" ? "arcsecond of RA axis turn" : "millisecond of RA pulse"}{" "}
                moves the star, as fitted now, ± how well the window pins it; faint: the calibration it
                started from. It moves off the calibration only as far as the window shows it should.
              </Hint>
            </th>
            <td>{pm(ra[0], model.ra_response_error?.[0], 3)}</td>
            <td>{pm(ra[1], model.ra_response_error?.[1], 3)}</td>
            <td className="faint">
              {fmt(model.ra_calibrated[0])}, {fmt(model.ra_calibrated[1])}
            </td>
          </tr>
          <tr>
            <th>Dec 1{unit(model.dec_unit)}</th>
            <td>{pm(dec[0], model.dec_response_error?.[0], 3)}</td>
            <td>{pm(dec[1], model.dec_response_error?.[1], 3)}</td>
            <td className="faint">
              {fmt(model.dec_calibrated[0])}, {fmt(model.dec_calibrated[1])}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="small faint mono">
        {raOffset != null && model.ra_unit === "arcsec" && <>RA {signedFmt(raOffset, 3)}&Prime;/s · </>}
        {lastDecSteps ? <>Dec {Math.abs(lastDecSteps)} {lastDecSteps > 0 ? "N" : "S"} · </> : null}
        {model.frames != null && <>{model.frames} frames, {Math.round(model.span_s ?? 0)}s · </>}
        {model.scatter != null && <>scatter {model.scatter.toFixed(2)} px</>}
      </div>
    </div>
  );
}

function unit(name: string): string {
  return name === "arcsec" ? "″" : name === "step" ? " step" : " ms";
}

function fmt(value: number | undefined): string {
  if (value == null) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(3)}`;
}

function signedFmt(value: number, digits: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function pm(value: number | undefined, error: number | undefined, digits = 2): string {
  if (value == null) return "--";
  const spread = error == null ? "" : ` ±${error.toFixed(digits)}`;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}${spread}`;
}
