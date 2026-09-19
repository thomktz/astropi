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

## Night mode

`[data-night="on"]` on the root element turns the interface red on black,
including a filter over the frame preview so even the image emits no blue or
green. Dark adaptation takes twenty minutes to build and a few seconds of
white screen to destroy. It is remembered per browser in localStorage, which
is the right place for it - it is a per-device preference, unlike the
observing site, which lives on the backend so every device agrees.

## Layout

One responsive grid, panels at a 330px minimum. It has to work on a laptop
indoors and on a phone propped against a tripod leg, so controls are at
least 40px tall for cold fingers in the dark, and the body respects
`env(safe-area-inset-*)`.

## Types

`lib/types.ts` is hand-written rather than generated from the OpenAPI
schema. The surface is small, and generated output would need reviewing
anyway. `npm run build` type-checks, so drift surfaces the moment a
component reads a field that no longer exists.
