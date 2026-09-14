# astropi

Raspberry Pi controller for an astrophotography rig (Star Adventurer GTI + Nikon Zf).

## Architecture

The Pi runs two processes: `backend/` (FastAPI) owns mount state and does the actual GoTo work; `frontend/` (React) is the UI, reachable from any device on the LAN. The frontend polls the backend for mount status and issues GoTo/park commands over HTTP; it never talks to hardware directly. The object catalog and search are entirely client-side (no backend round-trip needed to browse objects).

## GoTo mechanics

The GTI has no way to know it's precisely pointed where commanded - polar alignment and mount backlash mean a raw GoTo lands close but not exact. Instead of a manual star-alignment step, the backend closes the loop itself:

1. GoTo the target's RA/Dec (mount's best guess, given whatever alignment it currently has).
2. Capture a frame with the Zf.
3. Plate-solve the frame to get the *actual* RA/Dec the camera is centered on.
4. If that's off from the target by more than a small tolerance, sync the mount to the solved position and repeat from step 1 (the mount's error model improves each pass, so this converges in a couple of iterations).

This also replaces manual per-session star alignment: the first successful solve *is* the sync.

## Hardware status

Nothing is connected yet. Mount and camera are both mocked behind interfaces (`Mount`, `Camera`, `PlateSolver` in `backend/app/`) so the rest of the system can be built and tested now:
- Mount: intended to run over the GTI's SynScan protocol (USB serial once the cable arrives, or WiFi) - see `backend/README.md` for the driver options under consideration.
- Camera: intended to run over `gphoto2` (tethered USB) for capture and live view.
- Plate solving: intended to run via ASTAP (offline, ARM-compatible), no internet dependency in the field.

## Backend

See [backend/README.md](backend/README.md).

## Frontend

See [frontend/README.md](frontend/README.md).
