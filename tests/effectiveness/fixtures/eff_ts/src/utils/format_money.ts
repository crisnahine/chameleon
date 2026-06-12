export function formatMoney(cents: number, currency: string = "USD"): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const dollars = Math.floor(abs / 100);
  const remainder = String(abs % 100).padStart(2, "0");
  return `${sign}${currency} ${dollars}.${remainder}`;
}
