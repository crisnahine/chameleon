import { truncateText } from "../utils/truncate_text";
import { cx } from "../utils/cx";

type ProductCardProps = {
  name: string;
  description: string;
  featured?: boolean;
};

export function ProductCard({ name, description, featured = false }: ProductCardProps) {
  return (
    <div className={cx("product-card", featured && "product-card-featured")}>
      <h3>{name}</h3>
      <p>{truncateText(description, 120)}</p>
    </div>
  );
}
