# Architecture

Notes on why the code is shaped the way it is. The [README](../README.md)
covers what it does.

## Layers

```
core/        coordinates, mount geometry, sidereal time, event bus, errors
devices/     device contracts, a registry, and backends behind them
services/    catalogue, ephemeris, plate solving, star detection,
             guiding, polar alignment
sequencing/  long operations as cancellable, observable tasks
api/         FastAPI routes and one multiplexed WebSocket
storage/     captured frames and preview rendering
```

Dependencies point downward only. `services/` knows about `devices/`
contracts but never about a driver; `api/` knows about services but does no
domain work of its own.

## Devices are contracts, not classes

`devices/` defines a `Protocol` per role - `Mount`, `Camera`, `Focuser`,
`Guider`. A backend satisfies one by having the right methods; there is no
base class to inherit and no registration decorator.

Two consequences worth stating:

**Capabilities are advertised, not assumed.** Mounts differ enormously in
what they will admit to. A Star Adventurer GTi has no pier side to report
and no absolute encoder to query. So a device carries a `DeviceDescriptor`
listing what it supports, callers check before acting, and the UI hides
controls the connected hardware cannot honour. A device asked to do
something outside its capabilities raises `CapabilityError` rather than
silently doing nothing - an autofocus that appears to succeed on a rig with
no focuser is worse than one that fails.

**Roles are separate from devices.** One object can fill several. The
ASI2600MC Duo carries an imaging sensor and a guide sensor in one body on
one USB connection, so a single backend registers under both `CAMERA` and
`GUIDE_CAMERA`. The registry maps role to device, which is why
`DeviceRegistry.register` takes the role explicitly instead of reading it
off the descriptor.

## Guiding is a protocol too

The built-in guide loop lives in `services/guiding.py`, but `Guider` is a
protocol, because guiding can legitimately come from elsewhere - an external
PHD2 process reached over its JSON-RPC socket is the obvious case. A
sequence asks for settled guiding without caring which is running.

## Long operations are tasks

Centring a target takes a minute; an imaging run takes hours. Neither fits
in an HTTP request. `sequencing/` gives them one shape: a `Task` reports
progress, is cancellable, and serialises to something the UI can render
without knowing its type.

The engine runs **one task at a time**, deliberately. Every task drives the
same mount and the same camera; two at once would fight over the hardware.
Anything that genuinely needs to overlap - guiding during an imaging run -
is a service with its own loop rather than a second task.

## One event bus, one socket

Everything that changes state publishes to an in-process `EventBus`. The
WebSocket is just another subscriber, which keeps domain code free of any
knowledge of transports: the guide loop emits `guiding.sample` without
caring whether a browser, a log file or nothing at all is listening.

Subscriber queues are bounded and drop their oldest entry when full. A phone
on a weak connection must never be able to stall the guide loop by failing
to drain its socket, and for telemetry the newest sample is the one that
matters.

The socket replays buffered recent events on connect, so a dashboard opened
mid-session renders immediately instead of waiting for the next event - on
an idle rig that could be a while.

State that cannot be reconstructed from events - the active target, the
guiding state - is then sent explicitly, *after* the replay so a stale
buffered event cannot overwrite it. The history is a ring buffer of 200
entries and guide samples fill it within a minute, so a replay alone leaves
a freshly opened dashboard claiming guiding is stopped while the loop runs.

## Where astropy is, and is not

`services/ephemeris.py` is the only module that imports astropy. Rise and
set times, twilight, moon geometry and planet positions all go through it,
because that is precisely the problem it exists to solve, and a hand-rolled
version silently drops precession, nutation, refraction and parallax.

`core/geometry.py` and `core/timekeeping.py` deliberately duplicate the
low-precision formulae without it. The simulator and the guide loop evaluate
those thousands of times per session, and an astropy `Time` round-trip costs
milliseconds each. The duplication is paid for by
`test_separation_agrees_with_astropy`, which pins the fast path to the
accurate one.

Altitude curves are computed by sampling the night on a five-minute grid and
reading rise, set and transit off it, rather than root-finding each event.
One vectorised transform for a whole night costs about as much as a single
scalar one.

## The simulator

`devices/backends/simulator/` models a rig that is wrong in the ways real
rigs are wrong, because a simulator that returned perfect answers would
exercise none of the code that matters.

`core/pointing.py` holds the mount geometry, and it is in `core/` rather
than in the simulator on purpose. A mount has two mechanical axes and
rotates about **its own** polar axis, which is never quite the celestial
pole. Almost everything this software exists to correct follows from that
one fact: GoTos miss, tracking drifts, the field rotates.

The simulator uses that model forwards, to produce those errors. Polar
alignment uses it in reverse, to recover the rotation axis from three plate
solves. Having both read from one model is what makes the simulated
alignment a genuine test rather than a constant planted for it to find.

The simulated mount *reports* where it believes it is pointing, which is not
where it actually is. Only the simulated camera may ask for the truth, and
frames carry it in `sim_`-prefixed metadata that the API strips before it
can reach a client. Everything else has to discover the pointing the way it
would with real hardware: by solving a frame.

## Nothing runs until it is asked to

A freshly started rig is parked, not tracking, not guiding, not cooling,
with no target. Opening the dashboard must never find something running
because of a previous session, and `test_a_freshly_started_rig_is_doing_nothing`
pins that.

The rig's state is the rig's state, though - not the browser's. Reloading
the page while the mount is tracking shows it tracking, because it is.

That covers the two live views as well. Both sensors can loop - the main
one from `services/preview.py`, the guide one from the guider's idle
preview - and neither does until it is switched on, from the button under
the display it feeds. The sensor is not free: it is the one a capture, a
plate solve and an autofocus all need, and a rig that starts exposing
because a browser was opened is a rig doing something nobody asked for.
While they run, both stand down for anything with a real claim on their
sensor: a task, a capture, calibration, or guiding itself.

## Two sensors, two loops

The Duo carries an imaging chip and a guide chip, and they are read at
once - the main one for framing, the guide one for whether there is still
a star to hold on to. So each has its own loop, its own cadence, its own
state, and its own pill in the status bar; neither waits for the other.

While the live view is running the main display follows it, and a
deliberate capture opens in an overlay of its own rather than replacing
it - a capture is a thing you stop and look at, not a reason to stop
watching the sky. With the loop off, the display shows whatever the camera
produced last, from the refresh button or a capture. That is one decision,
made in `_view_source` in `api/routes/camera.py`, rather than a client
comparing timestamps across two sources.

Each sensor owns its camera through one lock. The guide camera has an idle
preview loop *and* a guide loop *and* calibration, all wanting frames; a
single `asyncio.Lock` in `GuidingService` means they queue instead of
colliding with "an exposure is already in progress".

## Session plans

A plan is an ordered list of blocks - a target, and what to shoot on it.
The interesting work happens while it is being *edited*, not while it runs:
`services/planning.py` lays the blocks out on the clock and checks each
against the ephemeris, because the two questions worth answering are how
long the night takes and whether it will work at all.

Altitude is sampled nine times across a block rather than at its ends. A
long block can clear the horizon at both ends and still clip it in the
middle, and that is exactly the case worth catching.

A block that fails does not end the run. An unattended session that
abandons four hours of good targets because one solve failed on the first
is worse than one that records the failure and moves on.

Plans are one JSON file each, in a directory. No database: a plan is a few
kilobytes, there are a handful of them, and being able to read one in a
text editor - or copy last week's and change the targets - is worth more
than anything a schema would buy. They are written to a temporary file and
moved into place, so a crash midway leaves the previous plan intact.

The plan runs as one task. Centring, focus and capture are ordinary tasks
underneath, but they report *into* the session rather than publishing under
ids of their own - the operator is watching one job, not four.

## Guiding calibration

Worth being explicit here, because the word usually means something else
for a camera: this is not darks, flats or bias. It measures how the *mount*
moves the star on the *sensor*. The explanation lives here rather than in
the panel - it is read once, not every night.

Each axis is pulsed several times in one direction and the displacement
measured, which yields three numbers: how fast the mount pushes the star in
right ascension, how fast in declination, and the angle between the
sensor's axes and the mount's - because the camera is never square to the
mount. Without them, "the star drifted three pixels up and left" says
nothing about which way to correct or for how long.

Several small pulses rather than one long one, so that backlash and a
single bad frame are both averaged down. It is invalidated by anything that
changes the geometry: rotating the camera, flipping the mount, or moving a
long way in declination.

Declination guiding can be limited to one direction. Declination has
backlash - a reversing correction is partly swallowed by the gear teeth
before the axis moves - so when polar misalignment drives a consistent
drift one way, guiding only against that way never reverses and never pays
it.

Settings are read by the loop on each cycle rather than captured at start,
so a change to exposure or aggressiveness takes effect on the next frame.
That is the point: what you are usually trying to fix is the guiding
happening in front of you.

## Plate solving

`SolveHint` exists because blind solving searches the whole sky and takes
minutes, while a solver told roughly where to look and at what image scale
usually answers in under a second. The mount always has some idea where it
is pointing, so there is rarely a reason to solve blind.

Solvers are tried in preference order - ASTAP first, because it is local and
works offline in the field; astrometry.net's web service next if an API key
is configured; the simulated solver last, so a real ASTAP install is
preferred even in simulation.

## Frames stay raw

A `Frame` holds a 2-D array of raw ADU, undebayered and unstretched. Plate
solving, star detection and the preview renderer each want something
different from it, and transforming once up front would throw away
information one of them needs.

The frame store is bounded. A full ASI2600MC frame is about 52 MB, so an
unbounded cache would exhaust a Raspberry Pi within minutes of an imaging
run. Anything that must survive belongs on disk as FITS, which is the next
thing to build here.

## Site lives on the backend

The previous version kept the observing location per browser in
localStorage. It now lives on the backend, because every device looking at
the dashboard has to agree about the horizon, and because the Pi serves over
plain HTTP on the LAN, where the browser geolocation API refuses to work at
all. City search is proxied through the backend for the same reason.
