const BASE_URL = process.env["API_URL"] ?? "http://localhost:3001";

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  return res.json() as Promise<T>;
}
