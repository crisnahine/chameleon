import { truncateText } from "../utils/truncate_text";

type ProductCardProps = {
  name: string;
  description: string;
};

export function ProductCard({ name, description }: ProductCardProps) {
  return (
    <div className="product-card">
      <h3>{name}</h3>
      <p>{truncateText(description, 120)}</p>
    </div>
  );
}
