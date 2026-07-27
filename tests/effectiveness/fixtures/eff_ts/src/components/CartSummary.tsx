import { formatMoney } from "../utils/format_money";
import { cx } from "../utils/cx";

type CartSummaryProps = {
  itemCents: number[];
  taxCents: number;
  compact?: boolean;
};

export function CartSummary({ itemCents, taxCents, compact = false }: CartSummaryProps) {
  const subtotal = itemCents.reduce((sum, c) => sum + c, 0);
  return (
    <div className={cx("cart-summary", compact && "cart-summary-compact")}>
      <div>Subtotal: {formatMoney(subtotal)}</div>
      <div>Tax: {formatMoney(taxCents)}</div>
      <div>Total: {formatMoney(subtotal + taxCents)}</div>
    </div>
  );
}
