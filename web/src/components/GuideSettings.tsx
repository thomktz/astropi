import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { DecGuideMode, GuidingSettings } from "../lib/types";
import { ErrorNote, Section } from "./Field";
import { NumberField } from "./NumberField";

const DEC_MODES: { id: DecGuideMode; label: string; hint: string }[] = [
  { id: "auto", label: "Both", hint: "Correct declination in either direction" },
  { id: "north", label: "North", hint: "Only push north - never pays backlash on a reversal" },
  { id: "south", label: "South", hint: "Only push south - never pays backlash on a reversal" },
  { id: "off", label: "Off", hint: "Guide in right ascension only" },
];

/**
 * Guiding settings, split by how often they are touched.
 *
 * Exposure and gain change most nights, because they follow from how
 * bright a guide star the field happened to offer. Everything else is
 * behind a disclosure: useful when guiding misbehaves, noise the rest of
 * the time.
 */
export function GuideSettings() {
  const queryClient = useQueryClient();
  const [advanced, setAdvanced] = useState(false);

  const settings = useQuery({
    queryKey: ["guiding-settings"],
    queryFn: api.guiding.settings,
    retry: false,
  });

  const update = useMutation({
    mutationFn: (changes: Partial<GuidingSettings>) => api.guiding.updateSettings(changes),
    onSuccess: (next) => queryClient.setQueryData(["guiding-settings"], next),
  });

  const current = settings.data;
  if (!current) return null;

  return (
    <Section title="Settings">
      <div className="row">
        <NumberField
          label="Exposure (s)"
          value={current.exposure_s}
          min={0.1}
          step={0.5}
          onCommit={(exposure_s) => exposure_s != null && update.mutate({ exposure_s })}
        />
        <NumberField
          label="Gain"
          value={current.gain}
          min={0}
          step={10}
          onCommit={(gain) => gain != null && update.mutate({ gain })}
        />
      </div>
      <p className="small faint" style={{ margin: 0 }}>
        Longer exposures average out seeing but correct less often. Takes effect on the next frame,
        even mid-run.
      </p>

      <div>
        <div className="label">Declination</div>
        <div className="row quick">
          {DEC_MODES.map((mode) => (
            <button
              key={mode.id}
              className="ghost"
              aria-pressed={current.dec_mode === mode.id}
              title={mode.hint}
              onClick={() => update.mutate({ dec_mode: mode.id })}
            >
              {mode.label}
            </button>
          ))}
        </div>
        <p className="small faint" style={{ margin: "6px 0 0" }}>
          Declination has backlash: a reversing correction is partly swallowed by the gear teeth.
          If drift runs one way, guiding only that way never pays it.
        </p>
      </div>

      <button className="ghost" aria-expanded={advanced} onClick={() => setAdvanced((on) => !on)}>
        {advanced ? "Hide advanced" : "Advanced…"}
      </button>

      {advanced && (
        <div className="stack">
          <div className="row">
            <NumberField
              label="RA aggressiveness"
              value={current.ra_aggressiveness}
              min={0.1}
              max={2}
              step={0.05}
              onCommit={(ra_aggressiveness) => ra_aggressiveness != null && update.mutate({ ra_aggressiveness })}
            />
            <NumberField
              label="Dec aggressiveness"
              value={current.dec_aggressiveness}
              min={0}
              max={2}
              step={0.05}
              onCommit={(dec_aggressiveness) => dec_aggressiveness != null && update.mutate({ dec_aggressiveness })}
            />
          </div>
          <p className="small faint" style={{ margin: 0 }}>
            Fraction of each measured error corrected per cycle. Below one on purpose: you are
            fighting seeing as much as tracking error, and chasing every wobble adds motion.
          </p>

          <div className="row">
            <NumberField
              label={'Min move (")'}
              value={current.min_move_arcsec}
              min={0}
              step={0.05}
              onCommit={(min_move_arcsec) => min_move_arcsec != null && update.mutate({ min_move_arcsec })}
            />
            <NumberField
              label="Max pulse (ms)"
              value={current.max_pulse_ms}
              min={1}
              step={50}
              onCommit={(max_pulse_ms) => max_pulse_ms != null && update.mutate({ max_pulse_ms })}
            />
          </div>

          <div className="row">
            <NumberField
              label="Search radius (px)"
              value={current.search_radius_px}
              min={1}
              step={5}
              onCommit={(search_radius_px) => search_radius_px != null && update.mutate({ search_radius_px })}
            />
            <NumberField
              label="Edge margin"
              value={current.edge_margin}
              min={0}
              max={0.45}
              step={0.01}
              onCommit={(edge_margin) => edge_margin != null && update.mutate({ edge_margin })}
            />
          </div>

          <div className="row">
            <NumberField
              label="Cal pulse (ms)"
              value={current.calibration_pulse_ms}
              min={1}
              step={50}
              onCommit={(calibration_pulse_ms) => calibration_pulse_ms != null && update.mutate({ calibration_pulse_ms })}
            />
            <NumberField
              label="Cal steps"
              value={current.calibration_steps}
              min={2}
              step={1}
              onCommit={(calibration_steps) => calibration_steps != null && update.mutate({ calibration_steps })}
            />
          </div>
          <p className="small faint" style={{ margin: 0 }}>
            Calibration settings apply to the next calibration, not the one already measured.
          </p>

          <div className="row">
            <NumberField
              label={'Settle under (")'}
              value={current.settle_arcsec}
              min={0.1}
              step={0.1}
              onCommit={(settle_arcsec) => settle_arcsec != null && update.mutate({ settle_arcsec })}
            />
            <NumberField
              label="Settle for (s)"
              value={current.settle_time_s}
              min={0}
              step={1}
              onCommit={(settle_time_s) => settle_time_s != null && update.mutate({ settle_time_s })}
            />
          </div>
        </div>
      )}

      <ErrorNote error={update.error} />
    </Section>
  );
}
