import { apiPost } from "../api/client";
import { formatMoney } from "../utils/format_money";

export type InvoiceLine = { description: string; cents: number };

export async function submitInvoice(lines: InvoiceLine[]): Promise<string> {
  const total = lines.reduce((sum, line) => sum + line.cents, 0);
  const summary = lines
    .map((line) => `${line.description}: ${formatMoney(line.cents)}`)
    .join("\n");
  await apiPost("/api/invoices", { lines, total });
  return summary;
}
