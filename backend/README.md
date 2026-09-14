# backend

FastAPI server owning mount state and the GoTo/alignment loop. See the root [README](../README.md) for the overall GoTo mechanics.

## API

- `GET /health` - liveness check
- `GET /status` - current mount state: `idle | slewing | tracking | parked`, current RA/Dec, current target (if any)
- `POST /goto {ra_deg, dec_deg}` - runs the full goto-capture-solve-correct loop synchronously, returns the final status plus a log of each iteration
- `POST /park` - parks the mount, clears the target

The frontend polls `/status` (every 2s) rather than the backend pushing updates - simple, and fine for now since nothing here needs sub-second latency.

## Structure

- `mount.py`, `camera.py`, `platesolve.py` - small `Protocol` interfaces (`Mount`, `Camera`, `PlateSolver`) plus `Mock*` implementations. Nothing else in the codebase depends on the mock vs. real distinction, so swapping in real hardware is a matter of writing one new class per interface, not touching `align.py` or `main.py`.
- `align.py` - the actual goto-capture-solve-correct loop, hardware-agnostic.

## Real hardware, when it's connected

- **Mount**: the GTI speaks Sky-Watcher's SynScan motor-controller protocol (ASCII commands over serial or UDP). Two options once the USB cable is in:
  - [`pysynscan`](https://github.com/nachoplus/pysynscan) - thin, direct, but only understands raw motor-axis angles; RA/Dec conversion (sidereal time, mount geometry) would need to be written here.
  - INDI's `skywatcherAPIMount` driver via `pyindi-client` - does that RA/Dec conversion for us, at the cost of running a separate `indiserver` process and talking its XML property protocol.
  Leaning toward INDI: the goto loop above already treats RA/Dec as the interface, so it's a natural fit, at the cost of the extra process/protocol.
- **Camera**: `gphoto2`/`python-gphoto2` for tethered capture and live view.
- **Plate solving**: ASTAP, run as a subprocess, offline star-index files downloaded ahead of time.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
