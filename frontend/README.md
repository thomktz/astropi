# frontend

React + Vite (TypeScript). See the root [README](../README.md) for the overall architecture.

## Mount control

`src/api/mountApi.ts` talks to the real backend (`GET /status` polled every 2s, `POST /goto`, `POST /park`) - this part is fully wired, not mocked. The backend only knows RA/Dec; the frontend remembers which catalog object was last commanded so the UI can still show its name.

## Camera control

Still fully mocked in `src/api/mockApi.ts` (fake capture, fake live-view frame) - not wired to the backend's `gphoto2` plans yet.

## Object catalog

Entirely client-side, no backend involved:
- `api/planets.ts` - Mercury-Neptune, computed live from Keplerian orbital elements (positions actually move day to day, unlike everything else here)
- `api/stars.ts` - IAU-approved star names (434 stars)
- `api/messier.ts` - full Messier catalog (110 objects)
- `api/ngc.ts` - notable NGC/IC objects (OpenNGC, filtered to named or mag <= 10) not already covered by Messier

`api/catalog.ts` merges all of these and ranks search results by relevance (exact name/common-name match beats a constellation-only match), with magnitude as the tiebreaker.

## Location & visibility

`hooks/useLocation.ts` tries the browser Geolocation API first, with a manual lat/lon fallback (plus city search via Open-Meteo's geocoding API) - browser geolocation needs HTTPS or `localhost`, which the Pi's plain-HTTP LAN address won't satisfy. Location is per-browser (localStorage), entered fresh on each device; not synced through the backend.

`api/astro.ts` computes real rise/set times (local sidereal time, hour-angle at horizon) per object, shown as a color-coded badge: green (up, >1h to set), orange (setting within the hour), red (currently below the horizon or never rises from this latitude).

## Run

```bash
npm install
npm run dev
```
