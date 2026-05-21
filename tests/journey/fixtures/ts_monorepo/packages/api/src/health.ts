export type HealthStatus = { ok: boolean; uptime: number };

export function getHealth(): HealthStatus {
  return { ok: true, uptime: process.uptime() };
}
