import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import {
  arcsec,
  bearing,
  clockTime,
  degrees,
  duration,
  durationShort,
  formatDms,
  formatHms,
  hoursToMeridian,
  meridianIsMeaningful,
  moonPhaseName,
} from "../../lib/format";
import { dotClass, guidingHealth, mountHealth } from "../../lib/status";
import type { DrawerId } from "../railEntries";
import type { Telemetry } from "../../lib/useTelemetry";
import { Field, Section } from "../Field";

/**
 * Everything at a glance, in one place.
 *
 * The top bar answers "is anything wrong" in a row of dots. This answers
 * "what is going on", which needs more room than a bar has - and is where
 * the readings that are useful but not urgent belong: time to the
 * meridian, the moon, how much darkness is left.
 */
export function OverviewPanel({
  telemetry,
  onOpen,
}: {
  telemetry: Telemetry;
  onOpen: (drawer: DrawerId) => void;
}) {
  const { mount, target, task } = telemetry;
  const night = useQuery({ queryKey: ["night"], queryFn: api.night, staleTime: 10 * 60_000 });
  const guiding = useQuery({
    queryKey: ["guiding"],
    queryFn: api.guiding.status,
    refetchInterval: 5_000,
    retry: false,
  });

  const running = task?.state === "running";
  const hourAngle = mount?.hour_angle_deg ?? null;
  const showMeridian = hourAngle != null && meridianIsMeaningful(mount?.dec_deg);

  return (
    <>
      <Section title="Session">
        {running && task ? (
          <>
            <div className="spread">
              <span className="name">{task.name}</span>
              <span className="small dim">{task.step}</span>
            </div>
            {task.fraction != null && (
              <div className="bar">
                <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
              </div>
            )}
          </>
        ) : (
          <div className="spread">
            <span className="small faint">Nothing running.</span>
            <button className="ghost" style={{ flex: "0 0 auto" }} onClick={() => onOpen("session")}>
              Plan a session
            </button>
          </div>
        )}
      </Section>

      <Section title="Target">
        {target ? (
          <>
            <div className="spread">
              <button className="linkish" onClick={() => onOpen("target")}>
                {target.display_name}
              </button>
              <span className="small faint">{target.object_type}</span>
            </div>
            <div className="spread">
              <Field label="Altitude" value={degrees(target.altitude_deg, 1)} />
              <Field label="Azimuth" value={bearing(target.azimuth_deg, 0)} />
              {showMeridian && (
                <Field
                  label={hoursToMeridian(hourAngle) < 0 ? "Past meridian" : "To meridian"}
                  value={durationShort(Math.abs(hoursToMeridian(hourAngle)))}
                />
              )}
            </div>
          </>
        ) : (
          <div className="spread">
            <span className="small faint">No target.</span>
            <button className="ghost" style={{ flex: "0 0 auto" }} onClick={() => onOpen("target")}>
              Choose one
            </button>
          </div>
        )}
      </Section>

      <Section title="Mount">
        <div className="spread">
          <button className="linkish" onClick={() => onOpen("mount")}>
            <span className={`dot ${dotClass(mountHealth(mount))}`} /> {mount?.state ?? "unknown"}
          </button>
          <span className="small faint mono">
            {mount ? `${formatHms(mount.ra_deg)} ${formatDms(mount.dec_deg)}` : "--"}
          </span>
        </div>
        <div className="spread">
          <Field label="Altitude" value={degrees(mount?.alt_deg, 1)} />
          <Field label="Azimuth" value={bearing(mount?.az_deg, 1)} />
          <Field label="Tracking" value={mount?.tracking ? "sidereal" : "off"} />
        </div>
      </Section>

      <Section title="Guiding">
        {guiding.isError ? (
          <span className="small faint">No guide camera.</span>
        ) : (
          <>
            <div className="spread">
              <button className="linkish" onClick={() => onOpen("guiding")}>
                <span className={`dot ${dotClass(guidingHealth(telemetry.guideState))}`} />{" "}
                {telemetry.guideState}
              </button>
              <span className="small faint">
                {guiding.data?.calibrated ? "calibrated" : "not calibrated"}
              </span>
            </div>
            <div className="spread">
              <Field label="RMS total" value={arcsec(guiding.data?.rms_total_arcsec)} />
              <Field label="RA" value={arcsec(guiding.data?.rms_ra_arcsec)} />
              <Field label="Dec" value={arcsec(guiding.data?.rms_dec_arcsec)} />
            </div>
          </>
        )}
      </Section>

      {night.data && (
        <Section title="Tonight">
          <div className="spread">
            <Field label="Dark from" value={clockTime(night.data.astronomical_dusk)} />
            <Field label="Until" value={clockTime(night.data.astronomical_dawn)} />
            <Field
              label="Darkness"
              value={duration(night.data.dark_hours)}
              tone={night.data.dark_hours > 5 ? "good" : "fair"}
            />
          </div>
          <div className="small dim">
            Moon {moonPhaseName(night.data.moon_illumination)} &middot;{" "}
            {Math.round(night.data.moon_illumination * 100)}% lit &middot;{" "}
            {night.data.moon_altitude_deg > 0
              ? `up at ${night.data.moon_altitude_deg.toFixed(0)}°`
              : "below the horizon"}
            {target && ` · ${degrees(target.moon_separation_deg, 0)} from target`}
          </div>
        </Section>
      )}
    </>
  );
}
