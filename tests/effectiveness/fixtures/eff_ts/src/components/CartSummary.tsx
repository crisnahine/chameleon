import { formatMoney } from "../utils/format_money";

type CartSummaryProps = {
  itemCents: number[];
  taxCents: number;
};

export function CartSummary({ itemCents, taxCents }: CartSummaryProps) {
  const subtotal = itemCents.reduce((sum, c) => sum + c, 0);
  return (
    <div className="cart-summary">
      <div>Subtotal: {formatMoney(subtotal)}</div>
      <div>Tax: {formatMoney(taxCents)}</div>
      <div>Total: {formatMoney(subtotal + taxCents)}</div>
    </div>
  );
}
