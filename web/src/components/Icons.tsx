/**
 * Inline SVG icons for the rail.
 *
 * Drawn rather than pulled from an icon font or emoji: they inherit
 * `currentColor`, so night mode turns them red along with everything else.
 * Emoji would stay full-colour and punch a hole in dark adaptation.
 */

interface IconProps {
  size?: number;
}

function Svg({ size = 21, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** Crosshair: choosing and centring a target. */
export function TargetIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="7" />
      <circle cx="12" cy="12" r="2.2" />
      <path d="M12 1.5v4M12 18.5v4M1.5 12h4M18.5 12h4" />
    </Svg>
  );
}

/** Aperture blades: the camera. */
export function CameraIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 3v9l7.8 4.5M12 12 4.2 16.5M12 12l7.8-4.5M12 12H2.6M12 12l-7.8-4.5M12 12v9" />
    </Svg>
  );
}

/** An error trace: guiding. */
export function GuidingIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2 12h3l2.5-6 3 12 3-9 2.5 5 2-2h4" />
    </Svg>
  );
}

/** A tilted axis against a circle: polar alignment. */
export function AlignIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M7 19 17 5" />
      <circle cx="12" cy="12" r="1.6" fill="currentColor" />
      <path d="M12 3.5v2" />
    </Svg>
  );
}

/** Stacked frames: the imaging run. */
export function SessionIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="7" width="14" height="10" rx="1.5" />
      <path d="M7 4.5h11.5A1.5 1.5 0 0 1 20 6v11" />
    </Svg>
  );
}

/** Sliders: site, devices and display. */
export function SetupIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2.1" />
      <circle cx="10" cy="17" r="2.1" />
    </Svg>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Svg>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />
    </Svg>
  );
}

/** A body crossing the meridian: time to the flip. */
export function MeridianIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 2.5v19" strokeDasharray="2.5 2.5" />
      <path d="M3 17c3.5-7.5 14.5-7.5 18 0" />
      <circle cx="12" cy="9.6" r="2.2" fill="currentColor" stroke="none" />
    </Svg>
  );
}

/** The sun below the horizon: darkness remaining. */
export function DarknessIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.5 15h19" />
      <path d="M12 19.5v2M5.6 18.1l-1 1M18.4 18.1l1 1" />
      <path d="M7.4 15a4.6 4.6 0 0 1 9.2 0" />
    </Svg>
  );
}

/** A telescope on an equatorial head: the mount. */
export function MountIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m7.5 12.2 8-5.2" />
      <path d="M5.4 10.6 16 3.8a1.4 1.4 0 0 1 2 .5l1 1.7a1.4 1.4 0 0 1-.5 1.9L7.9 14.6a1.4 1.4 0 0 1-2-.5l-1-1.7a1.4 1.4 0 0 1 .5-1.8Z" />
      <path d="m10.5 13.6 2 3.4M12.5 17H9m3.5 0 3 4m-3-4-3 4" />
    </Svg>
  );
}

/** Everything at once: the overview. */
export function OverviewIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="3" width="7.5" height="7.5" rx="1.4" />
      <rect x="13.5" y="3" width="7.5" height="7.5" rx="1.4" />
      <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.4" />
      <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.4" />
    </Svg>
  );
}
