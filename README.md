# astropi

Control software for an astrophotography rig: choose a target, get the
telescope pointed at it, check the polar alignment, guide, and run the
camera. Backend in Python, dashboard in React, reachable from any device on
the network.

The rig it is being built for is a Sky-Watcher Star Adventurer GTi, a ZWO
ASI2600MC Duo and a Raspberry Pi. None of that is baked in: everything is
written against device contracts, and the hardware sits behind them.

## Running it

Nothing needs to be plugged in. The simulator is the default backend and
models a complete rig.

```bash
uv venv && uv pip install -e ".[dev]"
uv run astropi
```

```bash
npm install --prefix web && npm run dev --prefix web
```

The dashboard is then on <http://localhost:5173>, the API on
<http://localhost:8000>, and the interactive API docs on
<http://localhost:8000/docs>.

## What it does

- **Targets.** Messier, OpenNGC and the IAU star names, about 1,100 objects,
  plus the planets. Search is ranked, and every result carries its current
  altitude so the list answers "is this up right now".
- **GoTo that actually lands on target.** A mount's GoTo is close but not
  exact. This slews, takes a frame, plate-solves it to find where the camera
  really is, syncs the mount to that truth and goes again - converging in
  two or three passes. The first successful solve replaces the session's
  star alignment.
- **Polar alignment without a polar scope.** Three plate solves at different
  hour angles. The circle they trace gives the mount's real rotation axis,
  which compared against the celestial pole gives the error at each
  adjustment knob. It then keeps solving while the knobs turn, so the
  correction converges instead of being guesswork.
- **Guiding.** Calibrates the mount's pulse response against the guide
  sensor, then closes the loop, with dithering between sub-exposures.
- **Camera.** Exposure, gain, cooling, and a stretched preview - a raw
  astronomical frame shown linearly is a black rectangle.
- **Imaging runs.** Long sequences with dithering, reporting progress and
  cancellable at any point.
- **Night mode.** The whole interface goes red on black. Dark adaptation
  takes twenty minutes to build and seconds of white screen to destroy.

## How it is put together

```
src/astropi/
  core/        coordinates, mount geometry, sidereal time, event bus
  devices/     device contracts, a registry, and backends behind them
  services/    catalogue, ephemeris, plate solving, star detection,
               guiding, polar alignment - none of it hardware-aware
  sequencing/  long operations as cancellable, observable tasks
  api/         FastAPI routes and one multiplexed WebSocket
  storage/     captured frames and preview rendering
web/           React dashboard
```

Nothing above `devices/` imports a vendor SDK. A device advertises what it
can do rather than being assumed to do it, so a mount that cannot report
pier side, or a camera with no cooler, fails honestly instead of pretending.
One object can fill several roles, which is how the ASI2600MC Duo's two
sensors register as both the imaging camera and the guide camera.

See [docs/architecture.md](docs/architecture.md) for the reasoning, and
[docs/hardware.md](docs/hardware.md) for what connecting the real rig needs.

## The simulator

It is a model, not a stub, because a stub would prove nothing. Its polar
axis is not on the pole, so GoTos miss and tracking drifts. It has worm
periodic error and declination backlash for the guide loop to fight. It
renders real star fields from the catalogue, so plate solving, star
detection and autofocus all run on actual pixels.

The point is that the simulator applies mount geometry forwards while polar
alignment inverts it. Recovering a hidden rotation axis from three solved
positions is real geometry either way - not a constant planted for the test
to find.

```bash
uv run pytest
```

98 tests: the spherical maths checked against astropy, the closed loops end
to end, and the API through HTTP.
