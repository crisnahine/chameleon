import { formatMoney } from "../utils/format_money";

type PriceTagProps = {
  cents: number;
  currency?: string;
};

export function PriceTag({ cents, currency = "USD" }: PriceTagProps) {
  return <span className="price-tag">{formatMoney(cents, currency)}</span>;
}
