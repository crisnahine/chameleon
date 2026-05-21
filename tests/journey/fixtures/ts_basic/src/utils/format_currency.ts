export function formatCurrency(amount: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
}

export function parseCents(amount: number): number {
  return Math.round(amount * 100);
}
