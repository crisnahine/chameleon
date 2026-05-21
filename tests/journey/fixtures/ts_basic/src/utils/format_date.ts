export function formatDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function parseDate(s: string): Date {
  return new Date(s);
}
