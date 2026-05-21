type BadgeProps = {
  label: string;
  color?: "green" | "red" | "blue" | "gray";
};

export function Badge({ label, color = "gray" }: BadgeProps) {
  return <span className={`badge badge-${color}`}>{label}</span>;
}
