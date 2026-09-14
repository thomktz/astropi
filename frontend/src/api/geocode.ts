export interface CityResult {
  name: string;
  admin1?: string;
  country?: string;
  lat: number;
  lon: number;
}

export async function searchCities(query: string): Promise<CityResult[]> {
  const q = query.trim();
  if (q.length < 2) return [];

  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(q)}&count=5`;
  const res = await fetch(url);
  if (!res.ok) return [];
  const data = await res.json();
  const results = (data.results ?? []) as Array<{
    name: string;
    admin1?: string;
    country?: string;
    latitude: number;
    longitude: number;
  }>;
  return results.map((r) => ({
    name: r.name,
    admin1: r.admin1,
    country: r.country,
    lat: r.latitude,
    lon: r.longitude,
  }));
}
