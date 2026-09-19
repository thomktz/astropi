# Connecting the real rig

Everything currently runs against the simulator. This is what each piece
needs in order to talk to hardware.

Nothing above `devices/` should change. Adding a backend means writing a
class that satisfies the relevant protocol and registering it in
`Observatory._build_devices`.

## Mount - Star Adventurer GTi

The GTi speaks Sky-Watcher's motor-controller protocol: ASCII commands over
serial or UDP. Two routes:

- **[`pysynscan`](https://github.com/nachoplus/pysynscan)** - thin and
  direct, but it only understands raw motor-axis angles. The conversion to
  right ascension and declination, including sidereal time and mount
  geometry, would have to live in the backend. `core/pointing.py` already
  has that maths, so this is less work than it sounds.
- **INDI's `skywatcherAPIMount` via `pyindi-client`** - does the conversion
  for us, at the cost of running a separate `indiserver` process and
  speaking its XML property protocol.

INDI is the better fit. The `Mount` protocol is written in equatorial
coordinates, which is exactly INDI's interface, and it brings parking,
tracking rates and pulse guiding without reimplementing them.

The one thing to verify early is **pulse guiding latency**. The guide loop
assumes a pulse of N milliseconds moves the axis for about N milliseconds;
if the transport adds tens of milliseconds of jitter, calibration will be
noisy and the aggressiveness needs lowering.

## Cameras - ASI2600MC Duo

Two sensors, one body, one USB connection, so one backend object registers
under both `CAMERA` and `GUIDE_CAMERA`.

- **ZWO's ASI SDK** via `zwoasi` - direct, lowest latency, and the only way
  to get at the Duo's guide sensor cleanly if INDI's driver treats it
  awkwardly.
- **INDI's `indi_asi_ccd`** - consistent with the mount if INDI is already
  running.

Either way the `Camera` protocol needs: exposure with gain and offset,
region of interest and binning for guide frames, cooling with a setpoint,
and a raw 16-bit array out. All of that maps directly onto both APIs.

Frames come back Bayered (RGGB). Keep them that way - plate solving and star
detection both work better on the raw mosaic than on a debayered image, and
the preview renderer does its own thing.

## Plate solving - ASTAP

The intended field solver: offline, no upload, ARM builds available, and
fast enough on a Pi to sit inside a centring loop.

`AstapSolver` in `services/platesolve.py` is a stub with the shape already
in place. Finishing it needs:

1. the frame written to a temporary FITS file;
2. `astap -f <file> -ra <hours> -spd <dec+90> -r <radius> -fov <degrees>`
   run as a subprocess, using the `SolveHint` so the search stays local;
3. the resulting `.wcs` file parsed back into a `SolveResult`.

Install a star index ahead of time - H18 is enough for a 3-degree field.
`available()` already checks for the binary, so the solver is only offered
if it is actually installed.

## Focuser

Nothing is connected yet. Any ASCOM/Alpaca or INDI focuser with absolute
positioning will satisfy the `Focuser` protocol directly. The autofocus
routine assumes absolute positioning; a relative-only focuser can emulate it
by tracking its own step count.

## Running on the Pi

The backend serves the built dashboard, so there is one process and one
port. `ASTROPI_BACKEND=indi` selects the backend once the adapter exists.

Two things to watch:

- **Memory.** A full ASI2600MC frame is 52 MB. `ASTROPI_FRAME_CACHE_SIZE`
  bounds how many are held; the default of 12 is already 600 MB, which is
  too many for a 2 GB Pi.
- **CORS.** It is currently wide open, which is fine on a private LAN behind
  a router and not fine anywhere else. Lock it down before exposing the Pi
  to the internet.
