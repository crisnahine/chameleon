import { cx } from "../utils/cx";

type BadgeProps = {
  label: string;
  tone?: "green" | "red" | "gray";
  muted?: boolean;
};

export function Badge({ label, tone = "gray", muted = false }: BadgeProps) {
  return <span className={cx("badge", `badge-${tone}`, muted && "badge-muted")}>{label}</span>;
}
