import { apiGet, apiPost } from "../api/client";
import { clamp } from "../utils/clamp";
import { formatMoney } from "../utils/format_money";

export type Quote = { cents: number; expiresAt: string };

export async function fetchQuote(productId: string, quantity: number): Promise<Quote> {
  const qty = clamp(quantity, 1, 99);
  return apiGet<Quote>(`/api/quotes/${productId}?qty=${qty}`);
}

export async function placeOrder(productId: string, quote: Quote): Promise<string> {
  await apiPost("/api/orders", { productId, cents: quote.cents });
  return `ordered at ${formatMoney(quote.cents)}`;
}
