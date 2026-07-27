import { formatMoney } from "../utils/format_money";
import { cx } from "../utils/cx";

type PriceTagProps = {
  cents: number;
  currency?: string;
  discounted?: boolean;
};

export function PriceTag({ cents, currency = "USD", discounted = false }: PriceTagProps) {
  return (
    <span className={cx("price-tag", discounted && "price-tag-discounted")}>
      {formatMoney(cents, currency)}
    </span>
  );
}
