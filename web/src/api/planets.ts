import type { CelestialObject } from "./types";

// Low-precision Keplerian elements (JPL/Standish 1992), valid ~3000 BC-3000 AD,
// arcminute-level accuracy - not telescope-grade for planetary imaging, but
// plenty for framing a wide-field shot. Unlike stars/DSOs, planets move
// relative to the sky, so their RA/Dec are computed at catalog-load time
// rather than stored as static values.
//
// Elements: [a (AU), e, I (deg), L (deg), long.peri (deg), long.node (deg)]
// and their rates per Julian century.
interface Elements {
  a: [number, number];
  e: [number, number];
  i: [number, number];
  l: [number, number];
  peri: [number, number];
  node: [number, number];
}

const EARTH: Elements = {
  a: [1.00000261, 0.00000562],
  e: [0.01671123, -0.00004392],
  i: [-0.00001531, -0.01294668],
  l: [100.46457166, 35999.37244981],
  peri: [102.93768193, 0.32327364],
  node: [0.0, 0.0],
};

const PLANETS: Record<string, { elements: Elements; magnitude: number }> = {
  mercury: {
    elements: {
      a: [0.38709927, 0.00000037], e: [0.20563593, 0.00001906], i: [7.00497902, -0.00594749],
      l: [252.2503235, 149472.67411175], peri: [77.45779628, 0.16047689], node: [48.33076593, -0.12534081],
    },
    magnitude: -0.4,
  },
  venus: {
    elements: {
      a: [0.72333566, 0.0000039], e: [0.00677672, -0.00004107], i: [3.39467605, -0.0007889],
      l: [181.9790995, 58517.81538729], peri: [131.60246718, 0.00268329], node: [76.67984255, -0.27769418],
    },
    magnitude: -4.4,
  },
  mars: {
    elements: {
      a: [1.52371034, 0.00001847], e: [0.0933941, 0.00007882], i: [1.84969142, -0.00813131],
      l: [-4.55343205, 19140.30268499], peri: [-23.94362959, 0.44441088], node: [49.55953891, -0.29257343],
    },
    magnitude: 0.71,
  },
  jupiter: {
    elements: {
      a: [5.202887, -0.00011607], e: [0.04838624, -0.00013253], i: [1.30439695, -0.00183714],
      l: [34.39644051, 3034.74612775], peri: [14.72847983, 0.21252668], node: [100.47390909, 0.20469106],
    },
    magnitude: -2.2,
  },
  saturn: {
    elements: {
      a: [9.53667594, -0.0012506], e: [0.05386179, -0.00050991], i: [2.48599187, 0.00193609],
      l: [49.95424423, 1222.49362201], peri: [92.59887831, -0.41897216], node: [113.66242448, -0.28867794],
    },
    magnitude: 0.46,
  },
  uranus: {
    elements: {
      a: [19.18916464, -0.00196176], e: [0.04725744, -0.00004397], i: [0.77263783, -0.00242939],
      l: [313.23810451, 428.48202785], peri: [170.9542763, 0.40805281], node: [74.01692503, 0.04240589],
    },
    magnitude: 5.68,
  },
  neptune: {
    elements: {
      a: [30.06992276, 0.00026291], e: [0.00859048, 0.00005105], i: [1.77004347, 0.00035372],
      l: [-55.12002969, 218.45945325], peri: [44.96476227, -0.32241464], node: [131.78422574, -0.00508664],
    },
    magnitude: 7.78,
  },
};

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

function heliocentricEcliptic(elements: Elements, centuriesSinceJ2000: number): [number, number, number] {
  const T = centuriesSinceJ2000;
  const a = elements.a[0] + elements.a[1] * T;
  const e = elements.e[0] + elements.e[1] * T;
  const i = elements.i[0] + elements.i[1] * T;
  const L = elements.l[0] + elements.l[1] * T;
  const peri = elements.peri[0] + elements.peri[1] * T;
  const node = elements.node[0] + elements.node[1] * T;

  const w = peri - node; // argument of perihelion
  let M = (L - peri) % 360; // mean anomaly
  if (M > 180) M -= 360;
  if (M < -180) M += 360;

  // Solve Kepler's equation (Newton's method) for eccentric anomaly E, degrees.
  const eStar = e * RAD2DEG;
  let E = M + eStar * Math.sin(M * DEG2RAD);
  for (let iter = 0; iter < 10; iter++) {
    const dM = M - (E - eStar * Math.sin(E * DEG2RAD));
    const dE = dM / (1 - e * Math.cos(E * DEG2RAD));
    E += dE;
    if (Math.abs(dE) < 1e-7) break;
  }

  const xOrb = a * (Math.cos(E * DEG2RAD) - e);
  const yOrb = a * Math.sqrt(1 - e * e) * Math.sin(E * DEG2RAD);

  const cosW = Math.cos(w * DEG2RAD), sinW = Math.sin(w * DEG2RAD);
  const cosNode = Math.cos(node * DEG2RAD), sinNode = Math.sin(node * DEG2RAD);
  const cosI = Math.cos(i * DEG2RAD), sinI = Math.sin(i * DEG2RAD);

  const x = (cosNode * cosW - sinNode * sinW * cosI) * xOrb + (-cosNode * sinW - sinNode * cosW * cosI) * yOrb;
  const y = (sinNode * cosW + cosNode * sinW * cosI) * xOrb + (-sinNode * sinW + cosNode * cosW * cosI) * yOrb;
  const z = sinW * sinI * xOrb + cosW * sinI * yOrb;

  return [x, y, z];
}

function eclipticToEquatorial(x: number, y: number, z: number, obliquityDeg: number): [number, number] {
  const eps = obliquityDeg * DEG2RAD;
  const yEq = y * Math.cos(eps) - z * Math.sin(eps);
  const zEq = y * Math.sin(eps) + z * Math.cos(eps);
  let ra = Math.atan2(yEq, x) * RAD2DEG;
  if (ra < 0) ra += 360;
  const dec = Math.atan2(zEq, Math.sqrt(x * x + yEq * yEq)) * RAD2DEG;
  return [ra, dec];
}

function currentPosition(elements: Elements): { ra: number; dec: number } {
  const jd = Date.now() / 86400000 + 2440587.5;
  const T = (jd - 2451545.0) / 36525;

  const [ex, ey, ez] = heliocentricEcliptic(EARTH, T);
  const [px, py, pz] = heliocentricEcliptic(elements, T);

  // Geocentric ecliptic = planet - Earth
  const gx = px - ex, gy = py - ey, gz = pz - ez;
  const obliquity = 23.43928 - 0.0130042 * T;
  const [ra, dec] = eclipticToEquatorial(gx, gy, gz, obliquity);
  return { ra, dec };
}

const NAMES: Record<string, string> = {
  mercury: "Mercury", venus: "Venus", mars: "Mars",
  jupiter: "Jupiter", saturn: "Saturn", uranus: "Uranus", neptune: "Neptune",
};

export const PLANET_OBJECTS: CelestialObject[] = Object.entries(PLANETS).map(([id, { elements, magnitude }]) => {
  const { ra, dec } = currentPosition(elements);
  return {
    id,
    name: NAMES[id],
    commonNames: [],
    type: "Planet",
    ra: Math.round(ra * 10000) / 10000,
    dec: Math.round(dec * 10000) / 10000,
    magnitude,
  };
});
