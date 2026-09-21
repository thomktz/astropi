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

### The control set

Everything else the camera can do is advertised rather than hard-coded.
`controls()` returns a list of `ControlSpec` - name, label, current value,
range, default, units, and whether it can be written - and `set_control()`
writes one by name. That is deliberately the shape the drivers already
use: `ASIGetControlCaps` hands back exactly this record per control
(including `IsWritable` and `IsAutoSupported`), and INDI publishes the
same as number and switch vectors.

| ours | ASI SDK | INDI |
| --- | --- | --- |
| `gain` | `ASI_GAIN` | `CCD_CONTROLS.Gain` |
| `offset` | `ASI_OFFSET` | `CCD_CONTROLS.Offset` |
| `usb_bandwidth` | `ASI_BANDWIDTHOVERLOAD` | `CCD_CONTROLS.BandwidthOverload` |
| `high_speed_mode` | `ASI_HIGH_SPEED_MODE` | `CCD_VIDEO_FORMAT` |
| `cooler_on` | `ASI_COOLER_ON` | `CCD_COOLER` |
| `target_temp` | `ASI_TARGET_TEMP` | `CCD_TEMPERATURE` |
| `sensor_temp` *(read-only)* | `ASI_TEMPERATURE` | `CCD_TEMPERATURE` |
| `cooler_power` *(read-only)* | `ASI_COOLER_POWER_PERC` | `CCD_COOLER_POWER` |
| `dew_heater` | `ASI_ANTI_DEW_HEATER` | `CCD_DEW_HEATER` |

Read-only entries are in the list rather than hidden from it. A sensor
temperature and a cooler duty cycle are as much part of the control set as
gain is; the only difference is which way they travel, and `writable` says
so. The client renders whatever comes back, so the guide sensor - no
cooler, no heater - simply shows a shorter list without either side
knowing anything about which camera is attached.

### Cooling

The ASI SDK exposes cooling as ordinary control values, not a dedicated
API: `ASI_COOLER_ON` to switch it, `ASI_TARGET_TEMP` for the setpoint in
whole degrees, and two read-only ones - `ASI_TEMPERATURE` in tenths of a
degree and `ASI_COOLER_POWER_PERC` as a percentage. There is also
`ASI_ANTI_DEW_HEATER` for the window heater, which matters at low
setpoints. Through INDI the same things appear as `CCD_COOLER`,
`CCD_TEMPERATURE` and `CCD_COOLER_POWER`.

`set_cooling(enabled, target_c)` and the `CoolingStatus` readback already
match that shape, so the adapter is a direct mapping. Three things it
should add:

* **Ramp the setpoint.** Driving straight to a target makes the cooler run
  at full power and risks condensation. A few degrees per minute is the
  usual compromise.
* **Warm up before disconnecting.** The same ramp in reverse; pulling power
  from a cold sensor invites moisture.
* **Expose the dew heater** as a capability, since frost on the window at
  -20 is a real failure mode and the camera has a heater for exactly that.

On the setpoint itself: this is a CMOS sensor with very low dark current,
so deep cooling buys far less than it did on a CCD, and *consistency*
matters more than depth - a dark library only subtracts correctly at the
temperature it was shot at. Pick something holdable in August as well as
January. The cooler can pull roughly 35 degrees below ambient, so a target
that leaves power sitting near 100% has no headroom for a warm night and
will drift off setpoint; easing it up a few degrees is better than losing
the match with the darks.

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
