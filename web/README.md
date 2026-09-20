# web

React dashboard. See the root [README](../README.md) for the whole project.

```bash
npm install
npm run dev
```

Vite proxies `/api` and `/ws` to the backend on port 8000, so the client
calls same-origin paths and has no base URL to configure. In production the
backend serves the built bundle, and the same paths hold.

## How state arrives

Two channels, on purpose:

- **WebSocket** (`lib/useTelemetry.ts`) for anything that changes while you
  watch: mount position, camera state, guide samples, solve results, task
  progress. One socket carries every topic and a reducer folds them into a
  single state object. One multiplexed connection rather than a socket per
  concern, because the panels need them correlated in time and a phone on a
  weak link should open one connection, not six.
- **TanStack Query** for the rest - catalogue search, site, capabilities.
  `refetchOnWindowFocus` is off, since anything that changes is pushed.

Where both could serve, prefer the socket. The mount's coordinate readout
takes its value from telemetry and formats it client-side, because the
socket pushes a position every second and the REST payload would leave the
primary readout visibly stale right after a slew.

## Layout

Image-first. The frame fills the screen, a slim icon rail switches panels,
and one drawer opens over the image at a time. Eight panels competing for
attention was wrong for this: at the telescope you are looking at one thing,
and the thing you most want to see is the frame.

```
strip    always visible: mount state and position, the active target
         with its altitude, time to the meridian, darkness left,
         guiding RMS, exposure countdown, running task
rail     one button per panel; clicking the open one closes it, so the
         image is always one tap away. Number keys 1-6 do the same.
viewer   the frame, with zoom and pan
drawer   one panel, overlaid, non-modal - the telemetry behind it stays
         live and interactive
```

The cost of showing one panel at a time is that everything else becomes
invisible, so two things push back: the status strip carries what must never
be hidden, and the rail carries a dot per panel, so guiding losing its star
while the target list is open still shows.

The strip narrows in two stages, and never by dropping a readout. Words go
first - "connected", "meridian", "dark left" - and only much further down do
the values themselves shorten, losing the seconds from the coordinates and
falling back from "Andromeda Galaxy" to "M31". A phone shows the same
numbers as a laptop, just tersely. The brand and the night toggle are
pinned outside the scrolling middle, so the toggle can never scroll out of
reach.

The active target is not read back from the mount. A mount reports a
coordinate and has no idea it is called M31, so the name is session state
the backend holds and pushes on `target.active`.

Zoom and pan in the viewer are not decoration. Judging focus and star
roundness means seeing stars at their real pixel size, and at 26 megapixels
scaled into a viewport a star is a fraction of a screen pixel. Above 2x the
image switches to nearest-neighbour, because smoothing invents star shapes
and star shape is the thing being inspected.

On a phone the rail moves to the bottom within thumb reach and the drawer
becomes a sheet; a side rail plus a side drawer would leave the frame a
sliver. Controls are at least 40px tall throughout, for cold fingers in the
dark.

## Night mode

`[data-night="on"]` on the root element turns the interface red on black,
including a filter over the frame so even the image emits no blue or green.
Dark adaptation takes twenty minutes to build and a few seconds of white
screen to destroy. It is remembered per browser in localStorage, which is
the right place for it - a per-device preference, unlike the observing site,
which lives on the backend so every device agrees.

The rail icons and the galaxy mark are inline SVG using `currentColor`, so
they turn red with everything else. The favicon is a separate file that
picks its fill from `prefers-color-scheme`, since it has no page to inherit
from and the source artwork is solid black.

## Types

`lib/types.ts` is hand-written rather than generated from the OpenAPI
schema. The surface is small, and generated output would need reviewing
anyway. `npm run build` type-checks, so drift surfaces the moment a
component reads a field that no longer exists.
