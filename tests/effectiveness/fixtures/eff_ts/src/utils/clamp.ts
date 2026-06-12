export function clamp(value: number, min: number, max: number): number {
  if (min > max) throw new RangeError("min must be <= max");
  return Math.min(Math.max(value, min), max);
}
